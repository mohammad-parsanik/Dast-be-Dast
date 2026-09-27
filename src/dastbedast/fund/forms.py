"""Form fields that let admins type tomans and Jalali dates while the database keeps rials and Gregorian."""

from __future__ import annotations

import re
from datetime import date, datetime, time
from typing import Any

from django import forms
from django.utils import timezone

from dastbedast.core.jalali import format_jalali, parse_jalali
from dastbedast.core.money import RIALS_PER_TOMAN, format_tomans, parse_amount, to_ascii_digits


class TomanAmountField(forms.Field):
    widget = forms.TextInput(attrs={"dir": "ltr", "inputmode": "numeric", "placeholder": "تومان"})

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        for unused in ("max_value", "min_value", "step_size"):
            kwargs.pop(unused, None)
        super().__init__(*args, **kwargs)
        if self.help_text:
            self.help_text = f"به تومان. {self.help_text}"
        else:
            self.help_text = "به تومان."

    def prepare_value(self, value: Any) -> Any:
        if isinstance(value, int):
            return format_tomans(value)
        return value

    def to_python(self, value: Any) -> int | None:
        if value in (None, ""):
            return None
        if isinstance(value, int):
            return value
        whole, _, tenth = to_ascii_digits(str(value)).strip().partition(".")
        try:
            tomans = parse_amount(whole)
            rials = int(tenth) if tenth else 0
        except ValueError as exc:
            raise forms.ValidationError("مبلغ را به عدد و به تومان وارد کنید.") from exc
        if not 0 <= rials < RIALS_PER_TOMAN or len(tenth) > 1:
            raise forms.ValidationError("مبلغ را به عدد و به تومان وارد کنید.")
        return tomans * RIALS_PER_TOMAN + (rials if tomans >= 0 else -rials)


class JalaliDateFormField(forms.Field):
    widget = forms.TextInput(attrs={"dir": "ltr", "placeholder": "1405/07/10"})

    def prepare_value(self, value: Any) -> Any:
        if isinstance(value, datetime):
            value = value.date()
        if isinstance(value, date):
            return format_jalali(value)
        return value

    def to_python(self, value: Any) -> date | None:
        if value in (None, ""):
            return None
        if isinstance(value, date):
            return value
        try:
            return parse_jalali(str(value))
        except ValueError as exc:
            raise forms.ValidationError("تاریخ را به شکل ۱۴۰۵/۰۷/۱۰ وارد کنید.") from exc


_TIME_RE = re.compile(r"(\d{1,2}):(\d{2})\s*$")


class JalaliDateTimeFormField(forms.Field):
    widget = forms.TextInput(attrs={"dir": "ltr", "placeholder": "1405/07/10 17:41"})

    def prepare_value(self, value: Any) -> Any:
        if isinstance(value, datetime):
            local = timezone.localtime(value) if timezone.is_aware(value) else value
            return f"{format_jalali(local.date())} {local:%H:%M}"
        return value

    def to_python(self, value: Any) -> datetime | None:
        if value in (None, ""):
            return None
        if isinstance(value, datetime):
            return value
        text = to_ascii_digits(str(value)).strip()
        m = _TIME_RE.search(text)
        at = time(int(m[1]), int(m[2])) if m else time(12, 0)
        date_part = text[: m.start()].strip() if m else text
        try:
            day = parse_jalali(date_part)
            naive = datetime.combine(day, at)
        except ValueError as exc:
            raise forms.ValidationError("زمان را به شکل ۱۴۰۵/۰۷/۱۰ ۱۷:۴۱ وارد کنید.") from exc
        return timezone.make_aware(naive)
