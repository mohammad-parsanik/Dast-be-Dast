"""Thin client for Bale's bot API (https://docs.bale.ai). Pure Python, standard library only.

Follows the owner's Bale-Message-Sender pattern: throttle requests, retry network and server errors with
backoff, never retry a 4xx (except 429, which waits ``retry_after``). The token never appears in logs or
exceptions.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any, Protocol

log = logging.getLogger(__name__)

BASE_URL = "https://tapi.bale.ai"


class BaleError(Exception):
    def __init__(self, method: str, code: int | None, description: str) -> None:
        super().__init__(f"{method}: {code} {description}")
        self.method = method
        self.code = code
        self.description = description


class BotApi(Protocol):
    def call(self, method: str, **params: Any) -> Any: ...


class BaleClient:
    def __init__(
        self,
        token: str,
        base_url: str = BASE_URL,
        min_interval: float = 0.05,
        retries: int = 3,
        request_timeout: float = 15,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not token:
            raise ValueError("BALE_BOT_TOKEN is not set")
        self._url = f"{base_url}/bot{token}/"
        self._min_interval = min_interval
        self._retries = retries
        self._timeout = request_timeout
        self._sleep = sleep
        self._last = 0.0

    def _throttle(self) -> None:
        wait = self._last + self._min_interval - time.monotonic()
        if wait > 0:
            self._sleep(wait)
        self._last = time.monotonic()

    def _post(self, method: str, params: dict[str, Any], timeout: float) -> dict[str, Any]:
        body = json.dumps({k: v for k, v in params.items() if v is not None}).encode()
        req = urllib.request.Request(self._url + method, data=body, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read())  # type: ignore[no-any-return]
        except urllib.error.HTTPError as exc:
            try:
                return json.loads(exc.read())  # type: ignore[no-any-return]
            except ValueError:
                return {"ok": False, "error_code": exc.code, "description": exc.reason}

    def call(self, method: str, **params: Any) -> Any:
        # Long polling holds the request open for ``timeout`` seconds; allow for it.
        timeout = self._timeout + float(params.get("timeout") or 0)
        delay = 1.0
        for attempt in range(self._retries + 1):
            self._throttle()
            try:
                data = self._post(method, params, timeout)
            except (urllib.error.URLError, TimeoutError, ConnectionError, ValueError) as exc:
                data = {"ok": False, "error_code": None, "description": type(exc).__name__}
            if data.get("ok"):
                return data.get("result")
            code = data.get("error_code")
            desc = str(data.get("description", ""))
            retry_after = (data.get("parameters") or {}).get("retry_after")
            if code is not None and 400 <= code < 500 and code != 429:
                raise BaleError(method, code, desc)
            if attempt == self._retries:
                raise BaleError(method, code, desc)
            log.warning("bale %s failed (%s %s); retrying", method, code, desc)
            self._sleep(float(retry_after) if retry_after else delay)
            delay *= 2
        raise AssertionError("unreachable")
