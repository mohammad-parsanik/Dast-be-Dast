"""Allocate incoming money across a person's agreements, oldest unpaid installment first (PLAN §3)."""

from __future__ import annotations

from collections.abc import Hashable, Sequence
from dataclasses import dataclass
from datetime import date

from .schedule import Planned, cover


@dataclass(frozen=True)
class OpenAgreement:
    key: Hashable
    opened_on: date
    plan: Sequence[Planned]
    paid_total: int


@dataclass(frozen=True)
class AllocationResult:
    parts: list[tuple[Hashable, int]]  # (agreement key, amount), in allocation order, one per agreement
    leftover: int  # not owed on any agreement: held as the person's credit


def allocate_fifo(amount: int, agreements: Sequence[OpenAgreement]) -> AllocationResult:
    """Spread ``amount`` over unpaid installments ordered by due date, then agreement age, then sequence."""
    if amount <= 0:
        raise ValueError("amount must be positive")
    pieces = []
    for a in agreements:
        for c in cover(a.plan, a.paid_total):
            if c.remaining > 0:
                pieces.append((c.due_date, a.opened_on, c.seq, a.key, c.remaining))
    pieces.sort(key=lambda p: (p[0], p[1], p[2]))

    totals: dict[Hashable, int] = {}
    left = amount
    for _due, _opened, _seq, key, remaining in pieces:
        if left == 0:
            break
        take = min(left, remaining)
        totals[key] = totals.get(key, 0) + take
        left -= take
    return AllocationResult(list(totals.items()), left)
