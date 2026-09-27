from datetime import date

import jdatetime
import pytest
from hypothesis import given
from hypothesis import strategies as st

from dastbedast.core import schedule as s
from dastbedast.core.jalali import JMonth
from dastbedast.core.money import INSTALLMENT_UNIT

M = 1_000_000  # 100,000 tomans in rials


def j(y: int, m: int, d: int) -> date:
    return jdatetime.date(y, m, d).togregorian()


def test_default_loan_six_equal_rounded():
    plan = s.default_loan(125 * M // 10 * 10, j(1405, 7, 10))  # 12.5M tomans
    assert [p.due_date for p in plan] == [JMonth(1405, 8).plus(i).last_day() for i in range(6)]
    assert [p.amount for p in plan[:5]] == [20 * M] * 5
    assert plan[-1].amount == 25 * M
    assert sum(p.amount for p in plan) == 125 * M


def test_default_deposit_starts_two_months_later():
    plan = s.default_deposit(60 * M, j(1405, 7, 10))
    assert plan[0].due_date == j(1405, 9, 30)
    assert [p.amount for p in plan] == [10 * M] * 6


def test_equal_small_principal_splits_exactly():
    plan = s.equal(3_000_000, 6, JMonth(1405, 8))
    assert sum(p.amount for p in plan) == 3_000_000
    assert len(plan) == 6


@given(st.integers(min_value=1, max_value=10**12), st.integers(min_value=1, max_value=36))
def test_equal_always_sums_and_rounds(principal, count):
    plan = s.equal(principal, count, JMonth(1405, 1))
    assert sum(p.amount for p in plan) == principal
    assert all(p.amount > 0 for p in plan)
    if principal // count >= INSTALLMENT_UNIT:
        assert all(p.amount % INSTALLMENT_UNIT == 0 for p in plan[:-1])
        assert plan[-1].amount >= plan[0].amount


def test_balloon_and_single_and_custom():
    assert s.balloon(30 * M, JMonth(1405, 8), 3) == [s.Planned(JMonth(1405, 11).last_day(), 30 * M)]
    assert s.single(5 * M, j(1406, 12, 29)) == [s.Planned(j(1406, 12, 29), 5 * M)]
    plan = s.custom(10 * M, [(j(1405, 9, 30), 6 * M), (j(1405, 8, 30), 4 * M)])
    assert [p.amount for p in plan] == [4 * M, 6 * M]
    with pytest.raises(s.ScheduleError):
        s.custom(10 * M, [(j(1405, 9, 30), 6 * M)])


def test_cover_partial_payment():
    plan = s.equal(60 * M, 6, JMonth(1405, 8))
    covered = s.cover(plan, 25 * M)
    assert [c.paid for c in covered] == [10 * M, 10 * M, 5 * M, 0, 0, 0]
    assert covered[2].remaining == 5 * M


def test_standing_follow_up_and_review():
    plan = s.equal(60 * M, 6, JMonth(1405, 8))
    after_two_dues = j(1405, 10, 1)
    assert s.standing(plan, 20 * M, after_two_dues) is s.Standing.CURRENT
    assert s.standing(plan, 15 * M, after_two_dues) is s.Standing.FOLLOW_UP
    assert s.standing(plan, 0, after_two_dues) is s.Standing.REVIEW
    # the due date itself is not yet missed
    assert s.standing(plan, 0, JMonth(1405, 8).last_day()) is s.Standing.CURRENT


def test_revise_keeps_paid_part_and_replaces_rest():
    plan = s.equal(60 * M, 6, JMonth(1405, 8))
    new = s.revise(plan, 15 * M, s.balloon(45 * M, JMonth(1406, 1), 0))
    assert new == [
        s.Planned(JMonth(1405, 8).last_day(), 10 * M),
        s.Planned(JMonth(1405, 9).last_day(), 5 * M),
        s.Planned(JMonth(1406, 1).last_day(), 45 * M),
    ]
    assert s.cover(new, 15 * M)[1].is_paid


def test_revise_rejects_wrong_total_or_backdating():
    plan = s.equal(60 * M, 6, JMonth(1405, 8))
    with pytest.raises(s.ScheduleError):
        s.revise(plan, 10 * M, s.balloon(40 * M, JMonth(1406, 1), 0))
    with pytest.raises(s.ScheduleError):
        s.revise(plan, 20 * M, s.balloon(40 * M, JMonth(1405, 7), 0))


def test_shift_unpaid():
    plan = s.equal(30 * M, 3, JMonth(1405, 8))
    new = s.shift_unpaid(plan, 10 * M, 1)
    assert [(JMonth.of(p.due_date), p.amount) for p in new] == [
        (JMonth(1405, 8), 10 * M),
        (JMonth(1405, 10), 10 * M),
        (JMonth(1405, 11), 10 * M),
    ]


def test_reduce_from_end():
    plan = s.equal(30 * M, 3, JMonth(1405, 8))
    new = s.reduce_from_end(plan, 5 * M, 15 * M)
    assert [p.amount for p in new] == [5 * M, 5 * M, 5 * M]
    assert sum(p.amount for p in new) == 15 * M
    assert s.reduce_from_end(plan, 5 * M, 25 * M) == [s.Planned(JMonth(1405, 8).last_day(), 5 * M)]
    with pytest.raises(s.ScheduleError):
        s.reduce_from_end(plan, 5 * M, 26 * M)


@given(
    st.integers(min_value=1, max_value=10**11),
    st.integers(min_value=1, max_value=12),
    st.floats(min_value=0, max_value=1),
    st.integers(min_value=1, max_value=6),
)
def test_revision_preserves_paid_status(principal, count, paid_fraction, months):
    plan = s.equal(principal, count, JMonth(1405, 1))
    paid = int(principal * paid_fraction)
    new = s.shift_unpaid(plan, paid, months) if paid < principal else plan
    assert sum(p.amount for p in new) == principal
    assert s.settled_part(new, paid) == s.settled_part(plan, paid)
    assert sum(c.remaining for c in s.cover(new, paid)) == principal - paid
