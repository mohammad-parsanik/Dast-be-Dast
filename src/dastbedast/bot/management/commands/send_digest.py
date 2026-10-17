"""Send the daily digest to every bot admin who receives it (PLAN §5.5). Run it from cron."""

from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand

from dastbedast.bot import digest, ui
from dastbedast.bot.bale import BaleClient, BaleError
from dastbedast.bot.models import BotAdmin


class Command(BaseCommand):
    help = "Send the admins' daily digest through the bot."

    def handle(self, *args: Any, **options: Any) -> None:
        api = BaleClient(settings.BALE_BOT_TOKEN)
        text = digest.build()
        sent = 0
        for admin in BotAdmin.objects.filter(is_active=True, receives_digest=True, user__is_active=True):
            try:
                ui.send(api, admin.bale_user_id, text)
                sent += 1
            except BaleError as exc:
                self.stderr.write(f"digest not sent to admin {admin.pk}: {exc.code}")
        self.stdout.write(f"digest sent to {sent} admin(s)")
