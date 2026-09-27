"""Validation and masking for IBANs (شبا) and phone numbers. These values are PII: never log them."""

from __future__ import annotations

import re

from .money import to_ascii_digits

_IBAN_RE = re.compile(r"^IR\d{24}$")


def normalize_iban(text: str) -> str:
    cleaned = to_ascii_digits(text).upper().replace(" ", "").replace("-", "")
    if cleaned.isdigit() and len(cleaned) == 24:
        cleaned = "IR" + cleaned
    return cleaned


def is_valid_iban(iban: str) -> bool:
    """Iranian IBAN: ``IR`` + 24 digits, ISO 13616 mod-97 checksum."""
    if not _IBAN_RE.match(iban):
        return False
    rearranged = iban[4:] + iban[:4]
    numeric = "".join(str(int(ch, 36)) for ch in rearranged)
    return int(numeric) % 97 == 1


def mask_iban(iban: str) -> str:
    return f"{iban[:4]}…{iban[-4:]}" if len(iban) > 8 else "…"


def normalize_phone(text: str) -> str:
    """Normalise an Iranian mobile number to ``+989XXXXXXXXX``. Raises ``ValueError`` if not a mobile."""
    digits = re.sub(r"\D", "", to_ascii_digits(text))
    if digits.startswith("0098"):
        digits = digits[4:]
    elif digits.startswith("98"):
        digits = digits[2:]
    elif digits.startswith("0"):
        digits = digits[1:]
    if not re.fullmatch(r"9\d{9}", digits):
        raise ValueError("not an Iranian mobile number")
    return "+98" + digits


def mask_phone(phone: str) -> str:
    return f"{phone[:6]}…{phone[-2:]}" if len(phone) > 8 else "…"
