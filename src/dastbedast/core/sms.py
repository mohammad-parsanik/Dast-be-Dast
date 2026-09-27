"""Bank SMS parsers (PLAN §5.3). Amounts are rials. Formats: docs/bank-sms-samples.md.

Each parser returns ``None`` when the text is not in its format. Fields the parser cannot determine are
left empty and named in ``uncertain`` so the admin is asked instead of the parser guessing.
"""

from __future__ import annotations

import enum
import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime

import jdatetime

from .money import to_ascii_digits


class Bank(enum.StrEnum):
    PASARGAD = "pasargad"
    MELLI = "melli"
    RESALAT = "resalat"
    MEHR = "mehr"


class Direction(enum.StrEnum):
    IN = "in"
    OUT = "out"


class SmsParseError(ValueError):
    pass


@dataclass(frozen=True)
class ParsedSms:
    bank: Bank
    direction: Direction | None
    amount: int
    balance: int | None
    occurred_at: datetime | None  # naive, Tehran local time
    account: str | None
    channel: str | None = None
    uncertain: tuple[str, ...] = ()


_LETTERS = str.maketrans({"ي": "ی", "ك": "ک", "ى": "ی", "ة": "ه"})
_INVISIBLE = re.compile("[\u200e\u200f\u202a-\u202e\u2066-\u2069\ufeff]")


def normalize(text: str) -> str:
    text = _INVISIBLE.sub("", to_ascii_digits(text)).translate(_LETTERS)
    lines = (re.sub(r"[ \t]+", " ", line).strip() for line in text.replace("\r", "").split("\n"))
    return "\n".join(line for line in lines if line)


def _amount(text: str) -> int:
    return int(text.replace(",", ""))


def _jalali_datetime(year: int, month: int, day: int, hour: int, minute: int) -> datetime | None:
    try:
        result: datetime = jdatetime.datetime(year, month, day, hour, minute).togregorian()
    except ValueError:
        return None
    return result


def _infer_year(month: int, day: int, hour: int, minute: int, today: date) -> datetime | None:
    """SMS without a year: assume the current Jalali year, or last year if that would be in the future."""
    year = jdatetime.date.fromgregorian(date=today).year
    result = _jalali_datetime(year, month, day, hour, minute)
    if result is not None and result.date() > today:
        result = _jalali_datetime(year - 1, month, day, hour, minute)
    return result


_BALANCE_RE = re.compile(r"مانده\s*:\s*(-?[\d,]+)")


def _balance(text: str) -> int | None:
    m = _BALANCE_RE.search(text)
    return _amount(m.group(1)) if m else None


# Pasargad and Resalat share a layout; the account number tells them apart:
# Pasargad 201.8000.1234567.1 (four parts), Resalat 10.1234567.1 (three parts).
_SIGNED_RE = re.compile(
    r"^(?P<account>[\dx*]+(?:\.[\dx*]+){2,3})\n(?P<sign>[+-])(?P<amount>[\d,]+)\n"
    r"(?P<month>\d{2})/(?P<day>\d{2})_(?P<hour>\d{2}):(?P<minute>\d{2})",
    re.MULTILINE,
)


def parse_signed(text: str, today: date) -> ParsedSms | None:
    m = _SIGNED_RE.search(text)
    if not m:
        return None
    account = m["account"]
    bank = Bank.PASARGAD if account.count(".") == 3 else Bank.RESALAT
    when = _infer_year(int(m["month"]), int(m["day"]), int(m["hour"]), int(m["minute"]), today)
    balance = _balance(text)
    return ParsedSms(
        bank=bank,
        direction=Direction.IN if m["sign"] == "+" else Direction.OUT,
        amount=_amount(m["amount"]),
        balance=balance,
        occurred_at=when,
        account=account,
        uncertain=_missing(occurred_at=when, balance=balance),
    )


_MELLI_RE = re.compile(r"(?P<channel>[^\s:\d]+(?: [^\s:\d]+)*)\s*:\s*(?P<amount>[\d,]+)(?P<sign>[+-])")
_MELLI_DATE_RE = re.compile(r"^(?P<month>\d{2})(?P<day>\d{2})-(?P<hour>\d{2}):(?P<minute>\d{2})$", re.MULTILINE)
_ACCOUNT_RE = re.compile(r"حساب\s*:\s*(\S+)")


def parse_melli(text: str, today: date) -> ParsedSms | None:
    if "ملی" not in text:
        return None
    m = _MELLI_RE.search(text)
    if not m:
        return None
    d = _MELLI_DATE_RE.search(text)
    when = _infer_year(int(d["month"]), int(d["day"]), int(d["hour"]), int(d["minute"]), today) if d else None
    account = _ACCOUNT_RE.search(text)
    balance = _balance(text)
    return ParsedSms(
        bank=Bank.MELLI,
        direction=Direction.IN if m["sign"] == "+" else Direction.OUT,
        amount=_amount(m["amount"]),
        balance=balance,
        occurred_at=when,
        account=account.group(1) if account else None,
        channel=m["channel"],
        uncertain=_missing(occurred_at=when, balance=balance),
    )


# Labels seen on Mehr Iran messages, which carry no sign.
MEHR_LABELS = {"پرداخت گروهی": Direction.OUT}
_MEHR_AMOUNT_RE = re.compile(r"مبلغ\s*:\s*([\d,]+)")
_MEHR_TIME_RE = re.compile(
    r"زمان\s*:\s*(?P<year>\d{4})/(?P<month>\d{1,2})/(?P<day>\d{1,2})-(?P<hour>\d{1,2}):(?P<minute>\d{2})"
)


def parse_mehr(text: str, today: date) -> ParsedSms | None:
    amount = _MEHR_AMOUNT_RE.search(text)
    t = _MEHR_TIME_RE.search(text)
    if not amount or not t:
        return None
    label = text.split("\n", 1)[0]
    direction = MEHR_LABELS.get(label)
    when = _jalali_datetime(*(int(t[k]) for k in ("year", "month", "day", "hour", "minute")))
    account = _ACCOUNT_RE.search(text)
    balance = _balance(text)
    return ParsedSms(
        bank=Bank.MEHR,
        direction=direction,
        amount=_amount(amount.group(1)),
        balance=balance,
        occurred_at=when,
        account=account.group(1) if account else None,
        channel=label,
        uncertain=_missing(direction=direction, occurred_at=when, balance=balance),
    )


def _missing(**fields: object) -> tuple[str, ...]:
    return tuple(name for name, value in fields.items() if value is None)


PARSERS: dict[Bank, Callable[[str, date], ParsedSms | None]] = {
    Bank.MEHR: parse_mehr,
    Bank.MELLI: parse_melli,
    Bank.PASARGAD: parse_signed,
    Bank.RESALAT: parse_signed,
}


def parse_sms(text: str, today: date, bank: Bank | None = None) -> ParsedSms:
    """Parse a pasted bank SMS. ``bank`` forces a parser when the admin already knows the bank."""
    clean = normalize(text)
    if bank is not None:
        result = PARSERS[bank](clean, today)
        if result is not None and bank in (Bank.PASARGAD, Bank.RESALAT) and result.bank != bank:
            result = ParsedSms(**{**result.__dict__, "bank": bank})
    else:
        result = next((r for p in dict.fromkeys(PARSERS.values()) if (r := p(clean, today))), None)
    if result is None:
        raise SmsParseError("unrecognised SMS format")
    if result.amount <= 0:
        raise SmsParseError("amount must be positive")
    return result


def dedupe_key(bank: str, amount: int, occurred_at: datetime | None, balance: int | None) -> str:
    """Hash identifying one bank event, so the same SMS pasted twice is rejected (PLAN §5.2)."""
    stamp = occurred_at.strftime("%Y-%m-%dT%H:%M") if occurred_at else "-"
    raw = f"{bank}|{amount}|{stamp}|{'-' if balance is None else balance}"
    return hashlib.sha256(raw.encode()).hexdigest()
