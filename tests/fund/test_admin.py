import pytest
from django.urls import reverse

from dastbedast.fund import services
from dastbedast.fund.models import Agreement, BankAccount, LoanRequest, Person, Transaction

from .conftest import M, jat
from .test_services import deposit, loan

SMS = "201.8000.2xxxxxx9.1\n+12,000,000\n07/04_17:41\nمانده: 22,000,000"


@pytest.fixture
def client_op(client, operator):
    client.force_login(operator)
    return client


@pytest.mark.parametrize(
    "name",
    [
        "admin:index",
        "fund_dashboard",
        "admin:fund_transaction_changelist",
        "admin:fund_transaction_add",
        "admin:fund_agreement_changelist",
        "admin:fund_agreement_add",
        "admin:fund_person_add",
        "admin:fund_loanrequest_changelist",
        "admin:fund_scheduleversion_changelist",
    ],
)
def test_pages_render_for_operator(client_op, opened, name):
    assert client_op.get(reverse(name)).status_code == 200


def test_operator_cannot_touch_fund_accounts(client_op, opened):
    assert client_op.get(reverse("admin:fund_fundaccount_add")).status_code == 403


def test_paste_sms_and_confirm(client_op, opened, monkeypatch):
    monkeypatch.setattr(services, "today", lambda: jat(1405, 7, 20).date())
    response = client_op.post(
        reverse("admin:fund_transaction_add"),
        {
            "paste": SMS,
            "kind": "donation",
            "note": "test",
            "confirm_now": "on",
            "allocations-TOTAL_FORMS": "0",
            "allocations-INITIAL_FORMS": "0",
        },
    )
    assert response.status_code == 302, response.content.decode()[:2000]
    tx = Transaction.objects.get(kind="donation")
    assert (tx.amount, tx.balance_after, tx.fund_account, tx.status) == (12 * M, 22 * M, opened, "confirmed")
    assert tx.raw_sms == SMS
    # confirmed transactions are read-only and cannot be deleted, even by the owner
    page = client_op.get(reverse("admin:fund_transaction_change", args=[tx.pk])).content.decode()
    assert 'name="amount"' not in page


def test_amount_in_tomans(client_op, opened, operator):
    person = Person.objects.create(name="Donor")
    response = client_op.post(
        reverse("admin:fund_transaction_add"),
        {
            "kind": "donation",
            "fund_account": opened.pk,
            "amount": "۱,۲۰۰,۰۰۰",
            "occurred_at": "1405/07/05 10:30",
            "person": person.pk,
            "allocations-TOTAL_FORMS": "0",
            "allocations-INITIAL_FORMS": "0",
        },
    )
    assert response.status_code == 302, response.content.decode()[:2000]
    tx = Transaction.objects.get(kind="donation")
    assert tx.amount == 12 * M
    assert tx.status == "draft"


def test_reverse_action(client_op, opened, operator, lender):
    deposit(opened, lender, 5 * M, jat(1405, 7, 3), operator)
    tx = Transaction.objects.get(kind="deposit")
    url = reverse("admin:fund_transaction_changelist")
    data = {"action": "reverse_selected", "_selected_action": [tx.pk]}
    assert "دلیل برگشت" in client_op.post(url, data).content.decode()
    client_op.post(url, {**data, "reason": "entered twice"})
    assert Transaction.objects.filter(reverses=tx).exists()


def test_schedule_defer_view(client_op, opened, operator, borrower):
    ln = loan(opened, borrower, 12 * M, jat(1405, 7, 10), operator)
    url = reverse("admin:fund_agreement_schedule", args=[ln.pk])
    assert client_op.get(url).status_code == 200
    response = client_op.post(url, {"action": "defer", "months": 1, "reason": "asked"})
    assert response.status_code == 302
    assert ln.schedule_versions.count() == 2


def test_active_agreement_core_terms_locked_for_operator(client_op, opened, operator, lender):
    dep = deposit(opened, lender, 5 * M, jat(1405, 7, 3), operator)
    page = client_op.get(reverse("admin:fund_agreement_change", args=[dep.pk])).content.decode()
    assert 'name="principal"' not in page
    assert "تغییر جدول" in page


def test_loan_request_actions(client_op, borrower):
    req = LoanRequest.objects.create(person=borrower, requested_amount=5 * M, purpose="rent")
    url = reverse("admin:fund_loanrequest_changelist")
    client_op.post(url, {"action": "approve_referrer", "_selected_action": [req.pk]})
    client_op.post(url, {"action": "approve", "_selected_action": [req.pk]})
    req.refresh_from_db()
    assert req.status == "approved"
    assert req.agreement.kind == Agreement.Kind.LOAN


def test_payout_export(client_op, opened, operator, lender, monkeypatch):
    BankAccount.objects.create(person=lender, iban="IR160120000000001234567890", holder_name="Lender A")
    deposit(opened, lender, 6 * M, jat(1405, 5, 1), operator)
    # first payout due end of 1405/07; view at 1405/07/20
    monkeypatch.setattr(services, "today", lambda: jat(1405, 7, 20).date())
    page = client_op.get(reverse("fund_dashboard")).content.decode()
    assert "IR16…7890" in page
    csv = client_op.get(reverse("fund_payout_export")).content.decode()
    assert "IR160120000000001234567890,Lender A,1000000" in csv
