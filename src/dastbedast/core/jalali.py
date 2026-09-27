"""Jalali month arithmetic. Dates are stored as Gregorian ``datetime.date``; months are Jalali."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

import jdatetime

from .money import to_ascii_digits


@dataclass(frozen=True, order=True)
class JMonth:
    year: int
    month: int

    def __post_init__(self) -> None:
        if not 1 <= self.month <= 12:
            raise ValueError(f"month out of range: {self.month}")

    @classmethod
    def of(cls, d: date) -> JMonth:
        j = jdatetime.date.fromgregorian(date=d)
        return cls(j.year, j.month)

    def plus(self, months: int) -> JMonth:
        index = self.year * 12 + (self.month - 1) + months
        return JMonth(index // 12, index % 12 + 1)

    def minus(self, other: JMonth) -> int:
        """Number of months from ``other`` to ``self``."""
        return (self.year - other.year) * 12 + (self.month - other.month)

    def first_day(self) -> date:
        result: date = jdatetime.date(self.year, self.month, 1).togregorian()
        return result

    def last_day(self) -> date:
        """The due date for this month: its last Jalali day (PLAN §3)."""
        result: date = jdatetime.date(self.year, self.month, self.days()).togregorian()
        return result

    def days(self) -> int:
        if self.month <= 6:
            return 31
        if self.month <= 11:
            return 30
        return 30 if jdatetime.date(self.year, 1, 1).isleap() else 29

    def __str__(self) -> str:
        return f"{self.year:04d}/{self.month:02d}"


def due_date(origin: date, offset_months: int) -> date:
    """Last day of the Jalali month ``offset_months`` after the month of ``origin``.

    A loan disbursed on 1405/07/10 with offset 1 is due on 1405/08/30.
    """
    return JMonth.of(origin).plus(offset_months).last_day()


def format_jalali(d: date) -> str:
    j = jdatetime.date.fromgregorian(date=d)
    return f"{j.year:04d}/{j.month:02d}/{j.day:02d}"


_JALALI_RE = re.compile(r"^\s*(\d{2,4})\s*[/\-.]\s*(\d{1,2})\s*[/\-.]\s*(\d{1,2})\s*$")


def parse_jalali(text: str) -> date:
    """Parse ``1405/7/10`` (Persian digits allowed; a two-digit year means 14xx) to a Gregorian date."""
    m = _JALALI_RE.match(to_ascii_digits(text))
    if not m:
        raise ValueError(f"not a Jalali date: {text!r}")
    year, month, day = (int(g) for g in m.groups())
    if year < 100:
        year += 1400
    try:
        result: date = jdatetime.date(year, month, day).togregorian()
    except ValueError as exc:
        raise ValueError(f"invalid Jalali date: {text!r}") from exc
    return result
