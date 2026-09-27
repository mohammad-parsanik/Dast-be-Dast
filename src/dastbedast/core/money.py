"""Money helpers. Amounts are always integer rials; tomans exist only for display and input."""

from __future__ import annotations

RIALS_PER_TOMAN = 10
# Installments are rounded to 100,000 tomans (PLAN §3).
INSTALLMENT_UNIT = 100_000 * RIALS_PER_TOMAN

_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def to_ascii_digits(text: str) -> str:
    """Replace Persian and Arabic-Indic digits with ASCII digits."""
    return text.translate(_DIGITS)


def tomans_to_rials(tomans: int) -> int:
    return tomans * RIALS_PER_TOMAN


def parse_amount(text: str) -> int:
    """Parse a user-typed amount such as ``۱۲,۰۰۰,۰۰۰`` or ``12٬000٬000`` into an int."""
    cleaned = to_ascii_digits(text).strip()
    for sep in (",", "٬", "،", " ", "_", "‌"):
        cleaned = cleaned.replace(sep, "")
    if not cleaned or not cleaned.lstrip("-").isdigit():
        raise ValueError(f"not an amount: {text!r}")
    return int(cleaned)


def format_tomans(rials: int) -> str:
    """Display rials as tomans with thousands separators, e.g. ``12,000,000 → '1,200,000'``."""
    sign = "-" if rials < 0 else ""
    whole, rem = divmod(abs(rials), RIALS_PER_TOMAN)
    text = f"{sign}{whole:,}"
    return f"{text}.{rem}" if rem else text
