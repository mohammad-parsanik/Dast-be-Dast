from datetime import date

import jdatetime
import pytest

from dastbedast.core.jalali import JMonth, due_date, format_jalali, parse_jalali


def j(y: int, m: int, d: int) -> date:
    return jdatetime.date(y, m, d).togregorian()


def test_plan_example_loan_first_installment():
    # PLAN §3: disbursed 1405/07/10 → first installment 1405/08/30
    assert due_date(j(1405, 7, 10), 1) == j(1405, 8, 30)


def test_plan_example_deposit_first_payout():
    assert due_date(j(1405, 7, 10), 2) == j(1405, 9, 30)


def test_day_of_month_does_not_matter():
    assert due_date(j(1405, 7, 1), 1) == due_date(j(1405, 7, 30), 1)


def test_month_lengths():
    assert JMonth(1405, 1).last_day() == j(1405, 1, 31)
    assert JMonth(1405, 6).last_day() == j(1405, 6, 31)
    assert JMonth(1405, 7).last_day() == j(1405, 7, 30)
    assert JMonth(1403, 12).last_day() == j(1403, 12, 30)  # leap year
    assert JMonth(1404, 12).last_day() == j(1404, 12, 29)


def test_year_rollover():
    assert JMonth(1405, 11).plus(3) == JMonth(1406, 2)
    assert JMonth(1406, 2).minus(JMonth(1405, 11)) == 3
    assert JMonth(1406, 1).plus(-1) == JMonth(1405, 12)


def test_parse_and_format():
    d = parse_jalali("۱۴۰۵/۷/۱۰")
    assert d == j(1405, 7, 10)
    assert format_jalali(d) == "1405/07/10"
    assert parse_jalali("05-07-10") == d


@pytest.mark.parametrize("bad", ["", "1405/13/01", "1405/07/31", "yesterday"])
def test_parse_rejects(bad):
    with pytest.raises(ValueError):
        parse_jalali(bad)
