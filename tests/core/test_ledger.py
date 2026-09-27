import pytest
from hypothesis import given
from hypothesis import strategies as st

from dastbedast.core.ledger import (
    AdjustmentKind,
    AdjustmentMove,
    Buckets,
    Kind,
    LedgerError,
    TransactionMove,
    totals,
)

M = 1_000_000


def test_typical_month():
    b = totals(
        [
            TransactionMove(Kind.OPENING, 5 * M),
            TransactionMove(Kind.DEPOSIT, 100 * M, allocated=100 * M),
            TransactionMove(Kind.DONATION, 10 * M),
            TransactionMove(Kind.DISBURSEMENT, 60 * M, allocated=60 * M),
            TransactionMove(Kind.INSTALLMENT, 12 * M, allocated=10 * M),  # 2M overpaid → credit
            TransactionMove(Kind.PAYOUT, 20 * M, allocated=20 * M),
            TransactionMove(Kind.FEE, 1 * M),
            AdjustmentMove(AdjustmentKind.WRITE_OFF, 5 * M),
            AdjustmentMove(AdjustmentKind.TO_DONATION, 10 * M),
        ]
    )
    assert b == Buckets(cash=46 * M, equity=19 * M, lender_liabilities=70 * M, credit=2 * M, receivables=45 * M)
    assert b.balanced


def test_reversal_cancels():
    t = TransactionMove(Kind.INSTALLMENT, 10 * M, allocated=7 * M)
    assert totals([t, TransactionMove(Kind.INSTALLMENT, 10 * M, allocated=7 * M, reversal=True)]) == Buckets()


def test_deposit_must_be_fully_allocated():
    with pytest.raises(LedgerError):
        TransactionMove(Kind.DEPOSIT, 10 * M, allocated=5 * M).buckets()


moves = st.one_of(
    st.builds(
        lambda k, a, f, r: TransactionMove(
            k, a, a if k in (Kind.DEPOSIT, Kind.DISBURSEMENT) else int(a * f), reversal=r
        ),
        st.sampled_from(list(Kind)),
        st.integers(min_value=1, max_value=10**12),
        st.floats(min_value=0, max_value=1),
        st.booleans(),
    ),
    st.builds(AdjustmentMove, st.sampled_from(list(AdjustmentKind)), st.integers(min_value=1, max_value=10**12)),
)


@given(st.lists(moves, max_size=50))
def test_invariant_always_holds(ms):
    assert totals(ms).balanced
