"""The fund's four buckets and the invariant ``cash = equity + liabilities − receivables`` (PLAN §2).

Every confirmed transaction and adjustment is turned into a ``Movement``; the buckets are the sum of all
movements. Person credit (money received that nobody owes us) is a liability: we hold it for them.
"""

from __future__ import annotations

import enum
from collections.abc import Iterable
from dataclasses import dataclass, field


class Kind(enum.StrEnum):
    # money in
    OPENING = "opening"  # opening balance of a fund account at the start of the year
    DEPOSIT = "deposit"  # lender puts money in
    DONATION = "donation"
    PLEDGE = "pledge"  # monthly sponsor contribution (a donation)
    OWNER_COVER = "owner_cover"  # owner personally covers a loss
    INSTALLMENT = "installment"  # borrower repays
    OTHER_IN = "other_in"
    # money out
    DISBURSEMENT = "disbursement"  # loan paid out to a borrower
    PAYOUT = "payout"  # lender repaid, or a person's credit refunded
    FEE = "fee"  # bank fee
    OTHER_OUT = "other_out"


EQUITY_IN = frozenset({Kind.OPENING, Kind.DONATION, Kind.PLEDGE, Kind.OWNER_COVER, Kind.OTHER_IN})
INCOMING = EQUITY_IN | {Kind.DEPOSIT, Kind.INSTALLMENT}
EQUITY_OUT = frozenset({Kind.FEE, Kind.OTHER_OUT})


def is_incoming(kind: Kind) -> bool:
    return kind in INCOMING


class AdjustmentKind(enum.StrEnum):
    WRITE_OFF = "write_off"  # borrower balance lost: receivables → loss against equity
    TO_DONATION = "to_donation"  # lender forgives: liabilities → equity


@dataclass(frozen=True)
class Buckets:
    cash: int = 0
    equity: int = 0
    lender_liabilities: int = 0
    credit: int = 0
    receivables: int = 0

    @property
    def liabilities(self) -> int:
        return self.lender_liabilities + self.credit

    @property
    def balanced(self) -> bool:
        return self.cash == self.equity + self.liabilities - self.receivables

    def __add__(self, other: Buckets) -> Buckets:
        return Buckets(
            self.cash + other.cash,
            self.equity + other.equity,
            self.lender_liabilities + other.lender_liabilities,
            self.credit + other.credit,
            self.receivables + other.receivables,
        )

    def scaled(self, factor: int) -> Buckets:
        return Buckets(
            self.cash * factor,
            self.equity * factor,
            self.lender_liabilities * factor,
            self.credit * factor,
            self.receivables * factor,
        )


class LedgerError(ValueError):
    pass


@dataclass(frozen=True)
class TransactionMove:
    """A confirmed transaction. ``allocated`` is the part applied to agreements of the matching side:
    loans for installments and disbursements, deposits for deposits and payouts."""

    kind: Kind
    amount: int
    allocated: int = 0
    reversal: bool = False  # a reversal record cancels the effect of the original

    def buckets(self) -> Buckets:
        k, amt, alloc = self.kind, self.amount, self.allocated
        if amt <= 0:
            raise LedgerError("amount must be positive")
        if not 0 <= alloc <= amt:
            raise LedgerError("allocated amount must be between 0 and the amount")
        if k in EQUITY_IN:
            b = Buckets(cash=amt, equity=amt)
        elif k in EQUITY_OUT:
            b = Buckets(cash=-amt, equity=-amt)
        elif k is Kind.DEPOSIT:
            if alloc != amt:
                raise LedgerError("a deposit must be fully allocated to a deposit agreement")
            b = Buckets(cash=amt, lender_liabilities=amt)
        elif k is Kind.DISBURSEMENT:
            if alloc != amt:
                raise LedgerError("a disbursement must be fully allocated to a loan")
            b = Buckets(cash=-amt, receivables=amt)
        elif k is Kind.INSTALLMENT:
            b = Buckets(cash=amt, receivables=-alloc, credit=amt - alloc)
        elif k is Kind.PAYOUT:
            b = Buckets(cash=-amt, lender_liabilities=-alloc, credit=-(amt - alloc))
        else:  # pragma: no cover - exhaustive over Kind
            raise LedgerError(f"unknown kind {k}")
        return b.scaled(-1) if self.reversal else b


@dataclass(frozen=True)
class AdjustmentMove:
    kind: AdjustmentKind
    amount: int

    def buckets(self) -> Buckets:
        if self.amount <= 0:
            raise LedgerError("amount must be positive")
        if self.kind is AdjustmentKind.WRITE_OFF:
            return Buckets(receivables=-self.amount, equity=-self.amount)
        return Buckets(lender_liabilities=-self.amount, equity=self.amount)


@dataclass
class Ledger:
    buckets: Buckets = field(default_factory=Buckets)

    def apply(self, move: TransactionMove | AdjustmentMove) -> Buckets:
        self.buckets = self.buckets + move.buckets()
        return self.buckets


def totals(moves: Iterable[TransactionMove | AdjustmentMove]) -> Buckets:
    ledger = Ledger()
    for m in moves:
        ledger.apply(m)
    return ledger.buckets
