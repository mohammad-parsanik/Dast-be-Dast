import pytest

from dastbedast.core.jalali import JMonth
from dastbedast.core.schedule import Planned, Standing, standing
from dastbedast.fund import services
from dastbedast.fund.models import Agreement, LoanRequest, Transaction

from .conftest import M, jat


def assert_ledger_holds():
    buckets = services.fund_buckets()
    assert buckets.balanced
    assert buckets.cash == services.cash_on_hand()
    return buckets


def deposit(account, person, amount, when, user, subordinated=False):
    agreement = Agreement.objects.create(person=person, kind="deposit", principal=amount, subordinated=subordinated)
    tx = Transaction.objects.create(
        fund_account=account, kind="deposit", amount=amount, occurred_at=when, person=person, agreement=agreement
    )
    services.confirm(tx, user)
    agreement.refresh_from_db()
    return agreement


def loan(account, person, amount, when, user):
    request = LoanRequest.objects.create(person=person, requested_amount=amount, purpose="medical")
    services.approve_referrer(request, user)
    agreement = services.approve_loan(request, None, user)
    tx = Transaction.objects.create(
        fund_account=account, kind="disbursement", amount=amount, occurred_at=when, person=person, agreement=agreement
    )
    services.confirm(tx, user)
    agreement.refresh_from_db()
    return agreement


def pay(account, person, amount, when, user, kind="installment", **kw):
    tx = Transaction.objects.create(
        fund_account=account, kind=kind, amount=amount, occurred_at=when, person=person, **kw
    )
    return services.confirm(tx, user)


def test_full_cycle_by_operator(opened, operator, lender, borrower):
    acct = opened
    dep = deposit(acct, lender, 60 * M, jat(1405, 7, 10), operator)
    assert dep.status == "active"
    assert dep.plan()[0] == Planned(JMonth(1405, 9).last_day(), 10 * M)  # deposit: +2 months

    ln = loan(acct, borrower, 30 * M, jat(1405, 7, 10), operator)
    assert ln.plan()[0].due_date == JMonth(1405, 8).last_day()  # PLAN §3 example
    assert LoanRequest.objects.get(agreement=ln).status == "disbursed"
    b = assert_ledger_holds()
    assert (b.cash, b.lender_liabilities, b.receivables, b.equity) == (40 * M, 60 * M, 30 * M, 10 * M)

    result = pay(acct, borrower, 12 * M, jat(1405, 8, 20), operator)
    assert services.paid_total(ln) == 12 * M
    assert not result.warnings
    b = assert_ledger_holds()
    assert b.receivables == 18 * M

    pay(acct, lender, 10 * M, jat(1405, 9, 29), operator, kind="payout")
    assert services.paid_total(dep) == 10 * M
    assert assert_ledger_holds().lender_liabilities == 50 * M


def test_overpayment_becomes_credit_and_can_be_refunded(opened, operator, borrower):
    ln = loan(opened, borrower, 10 * M, jat(1405, 7, 10), operator)
    result = pay(opened, borrower, 12 * M, jat(1405, 8, 1), operator)
    assert result.warnings  # overpayment notice
    ln.refresh_from_db()
    assert ln.status == "settled"
    assert services.person_credit(borrower) == 2 * M
    assert assert_ledger_holds().credit == 2 * M
    pay(opened, borrower, 2 * M, jat(1405, 8, 2), operator, kind="payout")
    assert services.person_credit(borrower) == 0
    with pytest.raises(services.ServiceError):
        pay(opened, borrower, 1 * M, jat(1405, 8, 3), operator, kind="payout")


def test_fifo_across_two_loans(opened, superuser, borrower):
    deposit(opened, borrower, 100 * M, jat(1405, 7, 1), superuser)  # make cash available
    first = loan(opened, borrower, 12 * M, jat(1405, 7, 5), superuser)
    second = loan(opened, borrower, 12 * M, jat(1405, 8, 5), superuser)
    # first loan: 08 and 09 installments; second loan: 09 onwards. Same due date → older loan first.
    pay(opened, borrower, 5 * M, jat(1405, 8, 20), superuser)
    assert services.paid_total(first) == 4 * M
    assert services.paid_total(second) == 1 * M
    assert_ledger_holds()


def test_reversal_restores_state(opened, operator, borrower):
    ln = loan(opened, borrower, 10 * M, jat(1405, 7, 10), operator)
    before = services.fund_buckets()
    pay(opened, borrower, 5 * M, jat(1405, 8, 1), operator)
    tx = Transaction.objects.get(kind="installment")
    mirror = services.reverse(tx, "wrong person", operator)
    assert mirror.direction == "out"
    assert services.paid_total(ln) == 0
    assert assert_ledger_holds() == before
    with pytest.raises(services.ServiceError):
        services.reverse(tx, "again", operator)


def test_disbursement_reversal_blocked_after_payments(opened, operator, borrower):
    ln = loan(opened, borrower, 10 * M, jat(1405, 7, 10), operator)
    pay(opened, borrower, 1 * M, jat(1405, 8, 1), operator)
    disb = Transaction.objects.get(kind="disbursement")
    with pytest.raises(services.ServiceError):
        services.reverse(disb, "oops", operator)
    services.reverse(Transaction.objects.get(kind="installment"), "oops", operator)
    services.reverse(disb, "oops", operator)
    ln.refresh_from_db()
    assert ln.status == "pending"
    assert ln.active_version is None
    assert_ledger_holds()


def test_confirmed_twice_rejected(opened, superuser):
    tx = Transaction.objects.get(kind="opening")
    with pytest.raises(services.ServiceError):
        services.confirm(tx, superuser)


def test_operator_cannot_record_opening_balance(account, operator):
    tx = Transaction.objects.create(fund_account=account, kind="opening", amount=M, occurred_at=jat(1405, 7, 1))
    with pytest.raises(services.ServiceError):
        services.confirm(tx, operator)


def test_operator_permissions(operator):
    assert operator.has_perm("fund.change_transaction")
    assert operator.has_perm("fund.add_adjustment")
    assert not operator.has_perm("fund.delete_transaction")
    assert not operator.has_perm("fund.delete_person")
    assert not operator.has_perm("fund.change_fundaccount")
    assert not operator.has_perm("auth.change_user")


def test_loan_needs_approved_referrer(borrower, operator):
    request = LoanRequest.objects.create(person=borrower, requested_amount=5 * M, purpose="rent")
    with pytest.raises(services.ServiceError):
        services.approve_loan(request, None, operator)
    services.approve_referrer(request, operator)
    agreement = services.approve_loan(request, 4 * M, operator)
    assert agreement.principal == 4 * M
    assert services.pending_disbursements() == 4 * M


def test_defer_and_revise_keep_paid_part(opened, operator, borrower):
    ln = loan(opened, borrower, 30 * M, jat(1405, 7, 10), operator)
    pay(opened, borrower, 7 * M, jat(1405, 8, 1), operator)
    services.defer(ln, 2, "asked for time", operator)
    ln.refresh_from_db()
    plan = ln.plan()
    assert ln.active_version.number == 2
    assert plan[0] == Planned(JMonth(1405, 8).last_day(), 5 * M)
    assert plan[1] == Planned(JMonth(1405, 9).last_day(), 2 * M)
    assert plan[2] == Planned(JMonth(1405, 11).last_day(), 3 * M)  # unpaid rest of #2, moved 2 months
    assert sum(p.amount for p in plan) == 30 * M
    assert ln.schedule_versions.count() == 2
    with pytest.raises(services.ServiceError):
        services.revise_schedule(ln, [Planned(JMonth(1406, 1).last_day(), 1 * M)], "bad total", operator)


def test_write_off_and_donation(opened, operator, borrower, lender):
    dep = deposit(opened, lender, 20 * M, jat(1405, 7, 1), operator)
    ln = loan(opened, borrower, 10 * M, jat(1405, 7, 10), operator)
    pay(opened, borrower, 4 * M, jat(1405, 8, 1), operator)
    services.adjust(ln, "write_off", 6 * M, "moved abroad", operator)
    ln.refresh_from_db()
    assert ln.status == "written_off"
    assert services.outstanding(ln) == 0
    services.adjust(dep, "to_donation", 5 * M, "gift", operator)
    dep.refresh_from_db()
    assert sum(p.amount for p in dep.plan()) == 15 * M
    b = assert_ledger_holds()
    assert b.receivables == 0
    assert b.lender_liabilities == 15 * M
    assert b.equity == 10 * M - 6 * M + 5 * M


def test_overdue_standing(opened, operator, borrower):
    ln = loan(opened, borrower, 30 * M, jat(1405, 7, 10), operator)
    on = JMonth(1405, 10).first_day()
    assert standing(ln.plan(), 0, on) is Standing.REVIEW
    rows = services.overdue(on)
    assert rows[0].standing is Standing.REVIEW
    assert rows[0].amount == 10 * M
    f = services.monthly_forecast(on, horizon=3)
    assert f.at_risk == 30 * M  # review loans are not counted as income


def test_surplus_uses_cash_only(opened, operator, lender, borrower):
    other = type(lender).objects.create(name="Patient lender")
    deposit(opened, lender, 20 * M, jat(1405, 7, 1), operator)
    sub = deposit(opened, other, 5 * M, jat(1405, 7, 2), operator, subordinated=True)
    assert sub.active_version is None
    # cash 35M, regular payouts 20M → 15M free ≥ 5M
    alert = services.surplus()
    assert alert is not None and [s.key for s in alert.fitting] == [sub]
    # lend 12M: cash 23M − 20M regular = 3M < 5M, even though the borrower owes 12M
    loan(opened, borrower, 12 * M, jat(1405, 7, 10), operator)
    assert services.surplus() is None


def test_sms_duplicate_rejected(opened, operator):
    from dastbedast.core.sms import parse_sms

    text = "201.8000.2xxxxxx9.1\n+12,000,000\n07/04_17:41\nمانده: 22,000,000"
    parsed = parse_sms(text, jat(1405, 7, 20).date())
    tx = services.create_from_sms(parsed, text, opened, "donation", operator)
    result = services.confirm(tx, operator)
    assert not result.warnings  # 10M opening + 12M = 22M: matches the bank
    with pytest.raises(services.ServiceError):
        services.create_from_sms(parsed, text, opened, "donation", operator)


def test_balance_mismatch_warns(opened, operator):
    tx = Transaction.objects.create(
        fund_account=opened, kind="donation", amount=M, occurred_at=jat(1405, 7, 5), balance_after=999
    )
    result = services.confirm(tx, operator)
    assert any("مغایرت" in w for w in result.warnings)
