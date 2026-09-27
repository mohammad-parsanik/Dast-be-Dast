"""Schedule templates, derived installment status, and schedule revisions (PLAN §3, §4.4).

A schedule is an ordered list of ``Planned`` installments whose amounts sum to the amount still to be
settled on the agreement. Payments are recorded per agreement, not per installment; the paid total
covers installments in due-date order, which is how each installment's status is derived.
"""

from __future__ import annotations

import enum
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from .jalali import JMonth
from .money import INSTALLMENT_UNIT

DEFAULT_INSTALLMENTS = 6
LOAN_FIRST_OFFSET = 1  # first borrower installment: end of the month after disbursement
DEPOSIT_FIRST_OFFSET = 2  # first lender payout: end of the second month after deposit


class ScheduleError(ValueError):
    pass


@dataclass(frozen=True)
class Planned:
    due_date: date
    amount: int


@dataclass(frozen=True)
class Covered:
    seq: int
    due_date: date
    amount: int
    paid: int

    @property
    def remaining(self) -> int:
        return self.amount - self.paid

    @property
    def is_paid(self) -> bool:
        return self.paid >= self.amount


# --- templates -----------------------------------------------------------------------------------


def equal(principal: int, count: int, first_month: JMonth, unit: int = INSTALLMENT_UNIT) -> list[Planned]:
    """``count`` monthly installments rounded down to ``unit``; the remainder goes on the last one.

    If the principal is too small for the unit, installments are split exactly instead.
    """
    if principal <= 0:
        raise ScheduleError("principal must be positive")
    if count <= 0:
        raise ScheduleError("count must be positive")
    base = principal // count // unit * unit
    if base == 0:
        base = principal // count
    amounts = [base] * (count - 1) + [principal - base * (count - 1)]
    return [Planned(first_month.plus(i).last_day(), amount) for i, amount in enumerate(amounts) if amount > 0]


def balloon(principal: int, first_month: JMonth, skip_months: int) -> list[Planned]:
    """Nothing for ``skip_months`` months, then everything."""
    if principal <= 0:
        raise ScheduleError("principal must be positive")
    if skip_months < 0:
        raise ScheduleError("skip_months must not be negative")
    return [Planned(first_month.plus(skip_months).last_day(), principal)]


def single(principal: int, due: date) -> list[Planned]:
    if principal <= 0:
        raise ScheduleError("principal must be positive")
    return [Planned(due, principal)]


def custom(principal: int, items: Sequence[tuple[date, int]]) -> list[Planned]:
    plan = sorted((Planned(d, a) for d, a in items), key=lambda p: p.due_date)
    if any(p.amount <= 0 for p in plan):
        raise ScheduleError("installment amounts must be positive")
    if sum(p.amount for p in plan) != principal:
        raise ScheduleError("installments must add up to the principal")
    return plan


def default_loan(principal: int, disbursed_on: date) -> list[Planned]:
    return equal(principal, DEFAULT_INSTALLMENTS, JMonth.of(disbursed_on).plus(LOAN_FIRST_OFFSET))


def default_deposit(principal: int, deposited_on: date) -> list[Planned]:
    return equal(principal, DEFAULT_INSTALLMENTS, JMonth.of(deposited_on).plus(DEPOSIT_FIRST_OFFSET))


# --- derived status ------------------------------------------------------------------------------


def _ordered(plan: Sequence[Planned]) -> list[Planned]:
    return sorted(plan, key=lambda p: p.due_date)


def cover(plan: Sequence[Planned], paid_total: int) -> list[Covered]:
    """Apply ``paid_total`` to installments in due-date order."""
    left = max(paid_total, 0)
    result = []
    for seq, p in enumerate(_ordered(plan), start=1):
        paid = min(left, p.amount)
        left -= paid
        result.append(Covered(seq, p.due_date, p.amount, paid))
    return result


def missed(plan: Sequence[Planned], paid_total: int, today: date) -> list[Covered]:
    """Installments whose due date has passed and which are not fully paid."""
    return [c for c in cover(plan, paid_total) if c.due_date < today and not c.is_paid]


class Standing(enum.StrEnum):
    CURRENT = "current"
    FOLLOW_UP = "follow_up"  # one missed installment: message or call
    REVIEW = "review"  # two or more: manual review, not collectible in forecasts


def standing(plan: Sequence[Planned], paid_total: int, today: date) -> Standing:
    n = len(missed(plan, paid_total, today))
    if n == 0:
        return Standing.CURRENT
    return Standing.FOLLOW_UP if n == 1 else Standing.REVIEW


# --- revisions -----------------------------------------------------------------------------------


def settled_part(plan: Sequence[Planned], paid_total: int) -> list[Planned]:
    """The part of the schedule already covered by payments; a revision must keep it unchanged."""
    return [Planned(c.due_date, c.paid) for c in cover(plan, paid_total) if c.paid > 0]


def unpaid_part(plan: Sequence[Planned], paid_total: int) -> list[Planned]:
    return [Planned(c.due_date, c.remaining) for c in cover(plan, paid_total) if c.remaining > 0]


def revise(plan: Sequence[Planned], paid_total: int, new_tail: Sequence[Planned]) -> list[Planned]:
    """Replace only the unpaid part of ``plan`` with ``new_tail`` (renegotiation, PLAN §3).

    ``new_tail`` must add up to what is still unpaid, and no new installment may be dated before the
    last settled one.
    """
    kept = settled_part(plan, paid_total)
    unpaid = sum(p.amount for p in plan) - sum(p.amount for p in kept)
    if sum(p.amount for p in new_tail) != unpaid:
        raise ScheduleError("the new installments must add up to the unpaid amount")
    if any(p.amount <= 0 for p in new_tail):
        raise ScheduleError("installment amounts must be positive")
    if kept and new_tail and min(p.due_date for p in new_tail) < kept[-1].due_date:
        raise ScheduleError("new installments cannot be dated before a paid one")
    return kept + _ordered(new_tail)


def shift_unpaid(plan: Sequence[Planned], paid_total: int, months: int) -> list[Planned]:
    """Push every unpaid installment ``months`` later (deferral tool, PLAN §4.1)."""
    if months <= 0:
        raise ScheduleError("months must be positive")
    tail = [Planned(JMonth.of(p.due_date).plus(months).last_day(), p.amount) for p in unpaid_part(plan, paid_total)]
    return revise(plan, paid_total, tail)


def reduce_from_end(plan: Sequence[Planned], paid_total: int, reduction: int) -> list[Planned]:
    """Remove ``reduction`` from the unpaid installments, latest first (write-off, lender→donation)."""
    unpaid = unpaid_part(plan, paid_total)
    if reduction <= 0:
        raise ScheduleError("reduction must be positive")
    if reduction > sum(p.amount for p in unpaid):
        raise ScheduleError("reduction is larger than the unpaid amount")
    tail = []
    left = reduction
    for p in reversed(unpaid):
        cut = min(left, p.amount)
        left -= cut
        if p.amount - cut > 0:
            tail.append(Planned(p.due_date, p.amount - cut))
    kept = settled_part(plan, paid_total)
    return kept + _ordered(tail)
