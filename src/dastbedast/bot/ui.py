"""Turn replies into Bale ``sendMessage`` calls. Bale renders all text as Markdown and has no parse_mode."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from dastbedast.core.bot_flow import Reply

from .bale import BotApi

Button = tuple[str, str]  # (label, callback_data ≤ 64 bytes)


def reply_markup(reply: Reply) -> dict[str, Any] | None:
    if reply.ask_contact or reply.options:
        rows: list[list[dict[str, Any]]] = []
        if reply.ask_contact:
            rows.append([{"text": reply.ask_contact, "request_contact": True}])
        rows += [[{"text": label} for label in row] for row in reply.options]
        return {"keyboard": rows}
    if reply.remove_keyboard:
        return {"remove_keyboard": True}
    return None


def inline(rows: Sequence[Sequence[Button]]) -> dict[str, Any]:
    for row in rows:
        for _, data in row:
            if len(data.encode()) > 64:
                raise ValueError("callback_data longer than 64 bytes")
    return {"inline_keyboard": [[{"text": label, "callback_data": data} for label, data in row] for row in rows]}


def send(api: BotApi, chat_id: int, reply: Reply | str, markup: dict[str, Any] | None = None) -> Any:
    if isinstance(reply, str):
        reply = Reply(reply)
    return api.call("sendMessage", chat_id=chat_id, text=reply.text[:4096], reply_markup=markup or reply_markup(reply))
