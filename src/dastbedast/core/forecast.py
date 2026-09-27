"""Monthly cash forecast, free-to-lend, and the subordinated-lender surplus alert (PLAN §4.1, §4.3, §5.4)."""

from __future__ import annotations

from collections.abc import Hashable, Sequence
from dataclasses import dataclass, field
from datetime import date

from .jalali import JMonth
from .schedule import Planned, unpaid_part

DEFAULT_HORIZON = 12


@dataclass(frozen=True)
class ScheduledFlow:
    """An active agreement's schedule: a loan (money in) or a dated deposit (money out)."""

    plan: Sequence[Planned]
    paid_total: int
    incoming: bool
    collectible: bool = True  # False for loans under review: excluded from expected income


@dataclass(frozen=True)
class Pledge:
    monthly_amount: int
    first_month: JMonth
    last_month: JMonth | None = None  # None: open-ended
    received_this_month: int = 0

    def expected(self, month: JMonth, current: JMonth) -> int:
        if month < self.first_month or (self.last_month is not None and month > self.last_month):
            return 0
        if month == current:
            return max(self.monthly_amount - self.received_this_month, 0)
        return self.monthly_amount


@dataclass
class MonthRow:
    month: JMonth
    expected_in: int = 0
    expected_out: int = 0
    projected_cash: int = 0

    @property
    def net(self) -> int:
        return self.expected_in - self.expected_out


@dataclass
class Forecast:
    start_cash: int
    margin: int
    rows: list[MonthRow]
    overdue_in: int = 0  # collectible borrower installments already past due (counted in the first month)
    overdue_out: int = 0  # lender payouts already past due (counted in the first month)
    at_risk: int = 0  # unpaid installments of loans under review (not counted)
    shortfall_months: list[JMonth] = field(default_factory=list)

    @property
    def lowest_cash(self) -> int:
        return min([self.start_cash, *(r.projected_cash for r in self.rows)])

    @property
    def free_to_lend(self) -> int:
        """How much can be lent today without a shortfall anywhere in the horizon."""
        return max(self.lowest_cash - self.margin, 0)


def forecast(
    today: date,
    cash: int,
    flows: Sequence[ScheduledFlow],
    pledges: Sequence[Pledge] = (),
    pending_disbursements: int = 0,
    horizon: int = DEFAULT_HORIZON,
    margin: int = 0,
) -> Forecast:
    current = JMonth.of(today)
    rows = [MonthRow(current.plus(i)) for i in range(horizon + 1)]
    last = rows[-1].month
    result = Forecast(start_cash=cash, margin=margin, rows=rows)

    for flow in flows:
        for p in unpaid_part(flow.plan, flow.paid_total):
            month = JMonth.of(p.due_date)
            if month > last:
                continue
            if flow.incoming and not flow.collectible:
                result.at_risk += p.amount
                continue
            index = max(month.minus(current), 0)
            if flow.incoming:
                rows[index].expected_in += p.amount
                if month < current:
                    result.overdue_in += p.amount
            else:
                rows[index].expected_out += p.amount
                if month < current:
                    result.overdue_out += p.amount

    for pledge in pledges:
        for row in rows:
            row.expected_in += pledge.expected(row.month, current)

    rows[0].expected_out += pending_disbursements

    running = cash
    for row in rows:
        running += row.net
        row.projected_cash = running
        if running < 0:
            result.shortfall_months.append(row.month)
    return result


# --- subordinated ("pay me last") lenders ---------------------------------------------------------


@dataclass(frozen=True)
class Subordinated:
    key: Hashable
    opened_on: date
    balance: int


@dataclass(frozen=True)
class SurplusAlert:
    available: int
    fitting: list[Subordinated]


def surplus_alert(
    cash: int,
    pending_disbursements: int,
    regular_remaining: int,
    subordinated: Sequence[Subordinated],
) -> SurplusAlert | None:
    """Fire only when today's cash can repay the subordinated lenders (PLAN §4.3).

    ``available = cash − pending disbursements − every remaining regular lender payout``. Borrower
    balances are deliberately ignored: they will cover lenders eventually, not now. Lenders are taken in
    deposit order; the list stops at the first one who does not fit.
    """
    available = cash - pending_disbursements - regular_remaining
    fitting = []
    left = available
    for s in sorted(subordinated, key=lambda s: s.opened_on):
        if s.balance <= 0:
            continue
        if s.balance > left:
            break
        fitting.append(s)
        left -= s.balance
    return SurplusAlert(available, fitting) if fitting else None
