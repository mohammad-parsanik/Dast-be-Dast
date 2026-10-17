import pytest

from dastbedast.bot import admin_flow, digest
from dastbedast.bot.applicant import notify_status_changes
from dastbedast.bot.bale import BaleClient, BaleError
from dastbedast.bot.models import BotSession
from dastbedast.core import bot_flow as bf
from dastbedast.fund import services
from dastbedast.fund.models import BankAccount, LoanRequest, Person, Transaction
from tests.fund.conftest import M
from tests.fund.test_services import loan

from .conftest import ADMIN_ID, APPLICANT_ID, callback, jat, message

IBAN = "IR160120000000001234567890"  # made-up, valid checksum
PASARGAD_IN = "201.8000.2xxxxxx9.1\n+5,000,000\n07/20_10:00\nمانده: 5,000,000\n"
PASARGAD_OUT = "201.8000.2xxxxxx9.1\n-30,000,000\n07/21_11:00\nمانده: 1,000,000\n"


def apply(bot, user_id=APPLICANT_ID, contact_owner=None):
    bot.handle(message(user_id, "/start"))
    for answer in ["Test Applicant"]:
        bot.handle(message(user_id, answer))
    owner = user_id if contact_owner is None else contact_owner
    bot.handle(message(user_id, contact={"phone_number": "989121234567", "user_id": owner}))
    for answer in [
        "Friend C",
        "colleague",
        "20,000,000",
        "درمان",
        "surgery",
        IBAN,
        bf.SAME_NAME,
        bf.REPAYMENTS["default"],
        bf.SKIP,
        bf.SUBMIT,
    ]:
        bot.handle(message(user_id, answer))


def test_applicant_submits_and_admin_is_notified(bot, api, bot_admin):
    apply(bot)
    request = LoanRequest.objects.get()
    person = request.person
    assert (person.name, person.phone, person.bale_user_id) == ("Test Applicant", "+989121234567", APPLICANT_ID)
    assert person.referrer_text == "Friend C — colleague"
    assert request.requested_amount == 200 * M and request.purpose == "medical"
    assert request.bank_account == BankAccount.objects.get(iban=IBAN)
    assert request.notified_status == "new"
    assert "ثبت شد" in api.last_text(APPLICANT_ID)
    assert f"rq:r:{request.pk}" in api.buttons(ADMIN_ID)
    assert BotSession.objects.get(bale_user_id=APPLICANT_ID).flow == ""


def test_second_start_while_under_review_shows_status(bot, api):
    apply(bot)
    bot.handle(message(APPLICANT_ID, "/start"))
    assert "ثبت شد" in api.last_text(APPLICANT_ID)
    assert BotSession.objects.get(bale_user_id=APPLICANT_ID).flow == ""
    assert LoanRequest.objects.count() == 1


def test_someone_elses_contact_is_rejected(bot, api):
    apply(bot, contact_owner=APPLICANT_ID + 1)
    assert not LoanRequest.objects.exists()
    assert BotSession.objects.get(bale_user_id=APPLICANT_ID).step == "phone"


def test_existing_person_is_linked_not_duplicated(bot, borrower):
    borrower.phone = "+989121234567"
    borrower.save()
    apply(bot)
    borrower.refresh_from_db()
    assert Person.objects.count() == 1
    assert borrower.bale_user_id == APPLICANT_ID
    assert borrower.referrer_text == "Friend C"  # unchanged; the new one goes into the request notes
    assert "Friend C — colleague" in LoanRequest.objects.get().notes


def test_status_changes_are_sent_once(bot, api, operator):
    apply(bot)
    request = LoanRequest.objects.get()
    assert notify_status_changes(api) == 0
    services.mark_contacting(request)
    assert notify_status_changes(api) == 1
    assert "در حال بررسی" in api.last_text(APPLICANT_ID)
    assert notify_status_changes(api) == 0
    LoanRequest.objects.filter(pk=request.pk).update(status="rejected")  # paths without signals count too
    request.person.muted = True
    request.person.save()
    assert notify_status_changes(api) == 0
    assert LoanRequest.objects.get().notified_status == "rejected"


def test_non_admin_cannot_use_admin_buttons(bot, api, operator):
    apply(bot)
    request = LoanRequest.objects.get()
    bot.handle(callback(APPLICANT_ID, f"rq:r:{request.pk}"))
    assert LoanRequest.objects.get().status == "new"
    assert api.calls[-1][0] == "answerCallbackQuery"
    bot.handle(message(APPLICANT_ID, admin_flow.DIGEST))
    assert "خلاصه روز" not in api.last_text(APPLICANT_ID)


def test_whoami(bot, api):
    bot.handle(message(APPLICANT_ID, "/whoami"))
    assert str(APPLICANT_ID) in api.last_text()


def test_admin_approves_then_records_disbursement(bot, api, bot_admin, opened):
    apply(bot)
    request = LoanRequest.objects.get()
    bot.handle(callback(ADMIN_ID, f"rq:r:{request.pk}"))
    assert LoanRequest.objects.get().status == "referrer_approved"
    bot.handle(message(ADMIN_ID, "x"))  # a stray text in the menu does nothing harmful
    bot.handle(callback(ADMIN_ID, f"rq:m:{request.pk}"))
    bot.handle(message(ADMIN_ID, "3,000,000"))  # approve a different amount: 3M tomans = 30M rials
    assert f"rq:B:{request.pk}" in api.buttons(ADMIN_ID)
    bot.handle(callback(ADMIN_ID, f"rq:B:{request.pk}"))
    request.refresh_from_db()
    assert request.status == "approved" and request.approved_amount == 30 * M

    bot.handle(message(ADMIN_ID, admin_flow.OUT))
    bot.handle(message(ADMIN_ID, PASARGAD_OUT))
    assert "k:disbursement" in api.buttons(ADMIN_ID)
    bot.handle(callback(ADMIN_ID, "k:disbursement"))
    bot.handle(callback(ADMIN_ID, f"ag:{request.agreement_id}"))
    assert "ok" in api.buttons(ADMIN_ID)
    bot.handle(callback(ADMIN_ID, "ok"))
    tx = Transaction.objects.get(kind="disbursement")
    assert tx.is_confirmed and tx.confirmed_by == bot_admin and tx.person == request.person
    assert LoanRequest.objects.get().status == "disbursed"
    assert notify_status_changes(api) >= 1
    assert "جدول اقساط" in api.last_text(APPLICANT_ID)


def test_admin_records_installment_by_name_search(bot, api, bot_admin, opened, borrower, superuser):
    ln = loan(opened, borrower, 30 * M, jat(1405, 7, 10), superuser)
    bot.handle(message(ADMIN_ID, admin_flow.IN_INSTALLMENT))
    bot.handle(message(ADMIN_ID, PASARGAD_IN))
    bot.handle(message(ADMIN_ID, "Borrow"))
    assert f"pp:{borrower.pk}" in api.buttons(ADMIN_ID)
    bot.handle(callback(ADMIN_ID, f"pp:{borrower.pk}"))
    bot.handle(callback(ADMIN_ID, "ok"))
    tx = Transaction.objects.get(kind="installment")
    assert tx.is_confirmed and tx.amount == 5 * M
    assert services.paid_total(ln) == 5 * M
    # The same SMS cannot be recorded twice.
    bot.handle(message(ADMIN_ID, admin_flow.IN_INSTALLMENT))
    bot.handle(message(ADMIN_ID, PASARGAD_IN))
    assert "قبلاً ثبت" in api.last_text(ADMIN_ID)


def test_admin_new_deposit_for_new_person_as_draft(bot, api, bot_admin, opened):
    bot.handle(message(ADMIN_ID, admin_flow.IN_NEW))
    bot.handle(message(ADMIN_ID, PASARGAD_IN))
    bot.handle(callback(ADMIN_ID, "k:subdeposit"))
    bot.handle(callback(ADMIN_ID, "np"))
    bot.handle(message(ADMIN_ID, "New Lender"))
    bot.handle(callback(ADMIN_ID, "dr"))
    tx = Transaction.objects.get(kind="deposit")
    assert not tx.is_confirmed
    assert tx.person.name == "New Lender"
    assert tx.agreement.subordinated and tx.agreement.status == "pending"


def test_direction_mismatch_and_bad_sms(bot, api, bot_admin, opened):
    bot.handle(message(ADMIN_ID, admin_flow.OUT))
    bot.handle(message(ADMIN_ID, PASARGAD_IN))
    assert "واریز" in api.last_text(ADMIN_ID)
    bot.handle(message(ADMIN_ID, "hello"))
    assert "شناخته نشد" in api.last_text(ADMIN_ID)
    assert not Transaction.objects.filter(kind__in=["disbursement", "payout"]).exists()


def test_failed_confirm_rolls_back(bot, api, bot_admin, opened, lender):
    # A payout to someone the fund owes nothing: confirm refuses and no draft is left behind.
    bot.handle(message(ADMIN_ID, admin_flow.OUT))
    bot.handle(message(ADMIN_ID, PASARGAD_OUT))
    bot.handle(callback(ADMIN_ID, "k:payout"))
    bot.handle(message(ADMIN_ID, "Lender"))
    bot.handle(callback(ADMIN_ID, f"pp:{lender.pk}"))
    bot.handle(callback(ADMIN_ID, "ok"))
    assert "ثبت نشد" in "\n".join(p["text"] for p in api.sent(ADMIN_ID))
    assert not Transaction.objects.filter(kind="payout").exists()


def test_reject_needs_second_tap(bot, api, bot_admin):
    apply(bot)
    request = LoanRequest.objects.get()
    bot.handle(callback(ADMIN_ID, f"rq:x:{request.pk}"))
    assert LoanRequest.objects.get().status == "new"
    bot.handle(callback(ADMIN_ID, f"rq:X:{request.pk}"))
    assert LoanRequest.objects.get().status == "rejected"


def test_digest(bot, api, bot_admin, opened, borrower, superuser):
    loan(opened, borrower, 30 * M, jat(1405, 7, 10), superuser)
    text = digest.build()
    assert "خلاصه روز" in text and "قابل وام‌دهی" in text
    bot.handle(message(ADMIN_ID, admin_flow.DIGEST))
    assert "خلاصه روز" in api.last_text(ADMIN_ID)


# --- HTTP client -----------------------------------------------------------------------------------


def client_with(responses, monkeypatch):
    client = BaleClient("123:abc", sleep=lambda s: None, min_interval=0)
    calls = []

    def fake_post(method, params, timeout):
        calls.append(method)
        return responses.pop(0)

    monkeypatch.setattr(client, "_post", fake_post)
    return client, calls


def test_client_retries_server_errors(monkeypatch):
    client, calls = client_with([{"ok": False, "error_code": 502}, {"ok": True, "result": 7}], monkeypatch)
    assert client.call("getMe") == 7
    assert len(calls) == 2


def test_client_does_not_retry_client_errors(monkeypatch):
    client, calls = client_with([{"ok": False, "error_code": 400, "description": "bad"}], monkeypatch)
    with pytest.raises(BaleError) as exc:
        client.call("sendMessage", chat_id=1, text="x")
    assert exc.value.code == 400 and len(calls) == 1
    assert "123:abc" not in str(exc.value)


def test_client_waits_on_rate_limit(monkeypatch):
    waits = []
    client, _ = client_with(
        [{"ok": False, "error_code": 429, "parameters": {"retry_after": 3}}, {"ok": True, "result": True}], monkeypatch
    )
    client._sleep = waits.append
    assert client.call("sendMessage", chat_id=1, text="x") is True
    assert 3.0 in waits
