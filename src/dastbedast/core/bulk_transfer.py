"""Bulk-transfer export for the month's payouts (PLAN §7, Stage A).

PLACEHOLDER: the payout bank and its upload template are not chosen yet. ``export_placeholder_csv``
writes a generic CSV that the admin can check by eye. When the bank is chosen, add an exporter for its
exact template here and select it through ``EXPORTERS``.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from .identity import is_valid_iban


@dataclass(frozen=True)
class PayoutRow:
    iban: str
    holder_name: str
    amount: int  # rials
    description: str = ""


class BulkTransferError(ValueError):
    pass


def validate(rows: Sequence[PayoutRow]) -> None:
    for i, row in enumerate(rows, start=1):
        if not is_valid_iban(row.iban):
            raise BulkTransferError(f"row {i}: invalid IBAN")
        if row.amount <= 0:
            raise BulkTransferError(f"row {i}: amount must be positive")
        if not row.holder_name.strip():
            raise BulkTransferError(f"row {i}: account holder name is required")


def export_placeholder_csv(rows: Sequence[PayoutRow]) -> str:
    validate(rows)
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["row", "iban", "holder_name", "amount_rials", "description"])
    for i, row in enumerate(rows, start=1):
        writer.writerow([i, row.iban, row.holder_name, row.amount, row.description])
    return out.getvalue()


EXPORTERS: dict[str, Callable[[Sequence[PayoutRow]], str]] = {
    "placeholder_csv": export_placeholder_csv,
}
