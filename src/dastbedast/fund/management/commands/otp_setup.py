"""Give a user a TOTP device for admin 2FA and print the QR code to scan with an authenticator app."""

from typing import Any

import qrcode
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django_otp.plugins.otp_totp.models import TOTPDevice


class Command(BaseCommand):
    help = "Create a TOTP device for USERNAME (replacing any existing one) and show its QR code."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("username")

    def handle(self, *args: Any, username: str, **options: Any) -> None:
        try:
            user = get_user_model().objects.get(username=username)
        except get_user_model().DoesNotExist as exc:
            raise CommandError(f"no user {username!r}") from exc
        TOTPDevice.objects.filter(user=user).delete()
        device = TOTPDevice.objects.create(user=user, name="authenticator", confirmed=True)
        qr = qrcode.QRCode(border=1)
        qr.add_data(device.config_url)
        qr.print_ascii(out=self.stdout, invert=True)
        self.stdout.write("Scan this code with an authenticator app, then clear the terminal.")
