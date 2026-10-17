"""Run the Bale bot by long polling ``getUpdates`` (PLAN §6). No public HTTPS endpoint is needed."""

import logging
import traceback
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand

from dastbedast.bot.applicant import notify_status_changes
from dastbedast.bot.bale import BaleClient, BaleError
from dastbedast.bot.dispatch import Bot

log = logging.getLogger("dastbedast.bot")

POLL_TIMEOUT = 25


class Command(BaseCommand):
    help = "Run the Bale bot (long polling)."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--once", action="store_true", help="Process one batch of updates and exit.")

    def handle(self, *args: Any, **options: Any) -> None:
        api = BaleClient(settings.BALE_BOT_TOKEN)
        me = api.call("getMe")
        api.call("deleteWebhook")
        log.info("bot @%s started", me.get("username"))
        bot = Bot(api)
        offset = None
        while True:
            try:
                updates = api.call("getUpdates", offset=offset, timeout=0 if options["once"] else POLL_TIMEOUT)
            except BaleError as exc:
                log.warning("getUpdates failed: %s", exc.code)
                updates = []
            for update in updates or []:
                offset = update["update_id"] + 1
                try:
                    bot.handle(update)
                except Exception as exc:
                    # Messages of some exceptions can carry PII (e.g. unique-key values); log type and frames only.
                    frames = "".join(traceback.format_tb(exc.__traceback__))
                    log.error("update %s failed: %s\n%s", update["update_id"], type(exc).__name__, frames)
            try:
                notify_status_changes(api)
            except Exception as exc:
                log.error("status notifications failed: %s", type(exc).__name__)
            if options["once"]:
                if offset is not None:
                    api.call("getUpdates", offset=offset, timeout=0)  # confirm the processed batch
                return
