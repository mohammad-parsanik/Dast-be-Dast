"""Bot state. Bale user ids can exceed 2^31, so they are BigIntegerFields."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.conf import settings
from django.db import models
from django.utils import timezone

SESSION_TTL = timedelta(days=1)


class BotAdmin(models.Model):
    """A back-office user who may use the admin side of the bot. Managed by the superuser only."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, verbose_name="کاربر", on_delete=models.CASCADE)
    bale_user_id = models.BigIntegerField("شناسه بله", unique=True, help_text="با فرمان /whoami در بازو دیده می‌شود.")
    is_active = models.BooleanField("فعال", default=True)
    receives_digest = models.BooleanField("خلاصه روزانه", default=True)

    class Meta:
        verbose_name = "مدیر در بازو"
        verbose_name_plural = "مدیران در بازو"

    def __str__(self) -> str:
        return str(self.user)


class BotSession(models.Model):
    """Where each chat is in a conversation. ``flow`` is empty when idle."""

    bale_user_id = models.BigIntegerField(unique=True)
    chat_id = models.BigIntegerField()
    flow = models.CharField(max_length=20, blank=True)
    step = models.CharField(max_length=30, blank=True)
    data = models.JSONField(default=dict, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"{self.bale_user_id} {self.flow}:{self.step}"

    def expired(self) -> bool:
        return bool(self.flow) and self.updated_at is not None and timezone.now() - self.updated_at > SESSION_TTL

    def go(self, flow: str, step: str, data: dict[str, Any] | None = None) -> None:
        self.flow, self.step, self.data = flow, step, data or {}
        self.save()

    def reset(self) -> None:
        self.go("", "")
