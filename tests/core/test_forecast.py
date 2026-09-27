from datetime import date

from dastbedast.core.forecast import Pledge, ScheduledFlow, Subordinated, forecast, surplus_alert
from dastbedast.core.jalali import JMonth
from dastbedast.core.schedule import equal

M = 1_000_000
TODAY = JMonth(1405, 8).first_day()


def test_projection_and_free_to_lend():
    loan = ScheduledFlow(equal(30 * M, 3, JMonth(1405, 8)), 0, incoming=True)
    deposit = ScheduledFlow(equal(60 * M, 2, JMonth(1405, 9)), 0, incoming=False)
    f = forecast(TODAY, 20 * M, [loan, deposit], horizon=3)
    assert [r.net for r in f.rows] == [10 * M, -20 * M, -20 * M, 0]
    assert [r.projected_cash for r in f.rows] == [30 * M, 10 * M, -10 * M, -10 * M]
    assert f.shortfall_months == [JMonth(1405, 10), JMonth(1405, 11)]
    assert f.free_to_lend == 0


def test_overdue_goes_to_current_month_and_review_loans_excluded():
    late = ScheduledFlow(equal(20 * M, 2, JMonth(1405, 6)), 0, incoming=True)
    review = ScheduledFlow(equal(20 * M, 2, JMonth(1405, 6)), 0, incoming=True, collectible=False)
    f = forecast(TODAY, 0, [late, review], horizon=2)
    assert f.rows[0].expected_in == 20 * M
    assert f.overdue_in == 20 * M
    assert f.at_risk == 20 * M


def test_pledges_and_pending_disbursements():
    p = Pledge(2 * M, JMonth(1405, 7), JMonth(1405, 9), received_this_month=2 * M)
    f = forecast(TODAY, 10 * M, [], [p], pending_disbursements=5 * M, horizon=2, margin=1 * M)
    assert [r.expected_in for r in f.rows] == [0, 2 * M, 0]
    assert f.rows[0].expected_out == 5 * M
    assert f.free_to_lend == 4 * M


def test_surplus_ignores_borrower_balances():
    subs = [Subordinated("x", date(2026, 1, 1), 10 * M), Subordinated("y", date(2026, 2, 1), 5 * M)]
    # regular lenders still owed 40M; cash 45M → only 5M available: "x" (first in line) doesn't fit
    assert surplus_alert(45 * M, 0, 40 * M, subs) is None
    alert = surplus_alert(52 * M, 0, 40 * M, subs)
    assert alert is not None
    assert [s.key for s in alert.fitting] == ["x"]
    alert = surplus_alert(60 * M, 6 * M, 40 * M, subs)
    assert alert is not None
    assert [s.key for s in alert.fitting] == ["x"]  # pending disbursement leaves 4M, "y" needs 5M
    alert = surplus_alert(60 * M, 0, 40 * M, subs)
    assert alert is not None
    assert [s.key for s in alert.fitting] == ["x", "y"]
