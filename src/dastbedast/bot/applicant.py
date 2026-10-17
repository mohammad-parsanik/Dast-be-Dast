"""Borrower side of the bot: saving a finished application and telling applicants about status changes."""

from __future__ import annotations

import logging

from django.db import transaction as db_transaction
from django.db.models import F

from dastbedast.core.bot_flow import Application, Reply
from dastbedast.core.jalali import format_jalali
from dastbedast.core.money import format_tomans
from dastbedast.fund.models import BankAccount, LoanRequest, Person

from . import ui
from .bale import BaleError, BotApi

log = logging.getLogger(__name__)

S = LoanRequest.Status
UNDER_REVIEW = (S.NEW, S.CONTACTING, S.REFERRER_APPROVED)


def person_for(bale_user_id: int) -> Person | None:
    return Person.objects.filter(bale_user_id=bale_user_id).first()


def open_request(bale_user_id: int) -> LoanRequest | None:
    return LoanRequest.objects.filter(person__bale_user_id=bale_user_id, status__in=UNDER_REVIEW).first()


@db_transaction.atomic
def submit(app: Application, bale_user_id: int) -> LoanRequest:
    """Create or link the person, their bank account and a ``new`` loan request (PLAN §5.1)."""
    by_phone = Person.objects.select_for_update().filter(phone=app.phone).first()
    by_bale = Person.objects.select_for_update().filter(bale_user_id=bale_user_id).first()
    person = by_phone or by_bale
    notes = [app.notes] if app.notes else []
    if person is None:
        person = Person.objects.create(
            name=app.name, phone=app.phone, bale_user_id=bale_user_id, referrer_text=app.referrer_text
        )
    else:
        changed = []
        if person.bale_user_id is None and by_bale is None:
            person.bale_user_id = bale_user_id
            changed.append("bale_user_id")
        if not person.phone:
            person.phone = app.phone
            changed.append("phone")
        if not person.referrer_text:
            person.referrer_text = app.referrer_text
            changed.append("referrer_text")
        elif person.referrer_text != app.referrer_text:
            notes.append(f"معرف اعلام‌شده در این درخواست: {app.referrer_text}")
        if by_bale is not None and by_phone is not None and by_bale != by_phone:
            notes.append("هشدار: شماره تأییدشده متعلق به شخص دیگری با شناسه بله دیگر است؛ بررسی شود.")
        if app.name != person.name:
            notes.append(f"نام اعلام‌شده: {app.name}")
        if changed:
            person.save(update_fields=changed)
    account, _ = BankAccount.objects.get_or_create(
        person=person, iban=app.iban, defaults={"holder_name": app.holder_name}
    )
    return LoanRequest.objects.create(
        person=person,
        requested_amount=app.amount,
        purpose=app.purpose,
        description=app.description,
        repayment_choice=app.repayment,
        bank_account=account,
        notes="\n".join(notes),
        notified_status=S.NEW,
    )


def status_text(request: LoanRequest) -> str:
    ref = f"درخواست شماره {request.pk}"
    if request.status == S.NEW:
        return f"{ref} ثبت شد. پس از بررسی با شما تماس می‌گیریم."
    if request.status == S.CONTACTING:
        return f"{ref} در حال بررسی است و به‌زودی با شما تماس گرفته می‌شود."
    if request.status == S.REFERRER_APPROVED:
        return f"معرف {ref} تأیید شد. درخواست شما در صف بررسی نهایی است."
    if request.status == S.APPROVED:
        amount = format_tomans(request.approved_amount or request.requested_amount)
        return f"{ref} با مبلغ {amount} تومان تأیید شد. پس از واریز به شما خبر می‌دهیم."
    if request.status == S.DISBURSED:
        lines = [f"مبلغ وام {ref} به حساب شما واریز شد."]
        plan = request.agreement.plan() if request.agreement else []
        if plan:
            lines.append("جدول اقساط (تومان):")
            lines += [f"{format_jalali(p.due_date)}: {format_tomans(p.amount)}" for p in plan]
        return "\n".join(lines)
    if request.status == S.REJECTED:
        return f"متأسفانه {ref} در حال حاضر قابل پرداخت نیست."
    return f"{ref} بسته شد."


def notify_status_changes(api: BotApi) -> int:
    """Tell applicants about any status change they have not heard about yet, whatever path changed it."""
    sent = 0
    pending = LoanRequest.objects.exclude(status=F("notified_status")).select_related("person", "agreement")
    for request in pending:
        person = request.person
        if person.bale_user_id and not person.muted:
            try:
                ui.send(api, person.bale_user_id, Reply(status_text(request)))
                sent += 1
            except BaleError as exc:
                if exc.code is None or exc.code >= 500 or exc.code == 429:
                    log.warning("status message for request %s not sent; will retry", request.pk)
                    continue
                log.warning("status message for request %s rejected (%s)", request.pk, exc.code)
        LoanRequest.objects.filter(pk=request.pk).update(notified_status=request.status)
    return sent
