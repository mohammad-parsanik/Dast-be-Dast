from datetime import date

import pytest
from hypothesis import given
from hypothesis import strategies as st

from dastbedast.core.allocation import OpenAgreement, allocate_fifo
from dastbedast.core.jalali import JMonth
from dastbedast.core.schedule import equal

M = 1_000_000


def loan(key, month, principal=30 * M, paid=0, opened=date(2026, 1, 1)):
    return OpenAgreement(key, opened, equal(principal, 3, month), paid)


def test_oldest_installment_across_loans_first():
    a = loan("a", JMonth(1405, 8))
    b = loan("b", JMonth(1405, 7))
    result = allocate_fifo(15 * M, [a, b])
    assert result.parts == [("b", 10 * M), ("a", 5 * M)]
    assert result.leftover == 0


def test_already_paid_parts_are_skipped():
    a = loan("a", JMonth(1405, 8), paid=25 * M)
    result = allocate_fifo(10 * M, [a])
    assert result.parts == [("a", 5 * M)]
    assert result.leftover == 5 * M


def test_same_due_date_older_agreement_first():
    a = loan("a", JMonth(1405, 8), opened=date(2026, 2, 1))
    b = loan("b", JMonth(1405, 8), opened=date(2026, 1, 1))
    assert allocate_fifo(10 * M, [a, b]).parts == [("b", 10 * M)]


def test_rejects_non_positive():
    with pytest.raises(ValueError):
        allocate_fifo(0, [])


@given(st.integers(min_value=1, max_value=200 * M), st.integers(min_value=0, max_value=30 * M))
def test_conservation(amount, paid):
    agreements = [loan("a", JMonth(1405, 8), paid=paid), loan("b", JMonth(1405, 9))]
    result = allocate_fifo(amount, agreements)
    assert sum(v for _, v in result.parts) + result.leftover == amount
    owed = 60 * M - paid
    assert result.leftover == max(amount - owed, 0)
