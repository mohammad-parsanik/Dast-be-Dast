"""Every operation that changes money-related state goes through here, inside a DB transaction.

Admin views, the bot and management commands call these functions; they never write the money models
directly. Each function enforces the money-safety rules in CLAUDE.md and PLAN §4.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime

from django.contrib.auth.models import AbstractBaseUser, AnonymousUser
from django.db import transaction as db_transaction
from django.db.models import Q, Sum
from django.utils import timezone

from dastbedast.core import forecast as fc
from dastbedast.core import ledger
from dastbedast.core import schedule as sch
from dastbedast.core.allocation import OpenAgreement, allocate_fifo
from dastbedast.core.jalali import JMonth
from dastbedast.core.sms import ParsedSms, dedupe_key

from .models import (
    Adjustment,
    Agreement,
    Allocation,
    FundAccount,
    Installment,
    LoanRequest,
    Person,
    ScheduleVersion,
    Transaction,
)

UserLike = AbstractBaseUser | AnonymousUser


class ServiceError(Exception):
    """A rule was violated; the message is shown to the admin as is."""


K = ledger.Kind
CONFIRMED = Transaction.Status.CONFIRMED


def _user(user: UserLike | None) -> AbstractBaseUser | None:
    return user if user is not None and user.is_authenticated else None  # type: ignore[return-value]


def today() -> date:
    return timezone.localdate()


# --- reading agreement state ----------------------------------------------------------------------


def paid_total(agreement: Agreement) -> int:
    """Settled so far: installments received (loans) or payouts made (deposits). Reversals are negative."""
    return agreement.allocations.aggregate(s=Sum("amount"))["s"] or 0


def adjusted_total(agreement: Agreement) -> int:
    return agreement.adjustments.aggregate(s=Sum("amount"))["s"] or 0


def outstanding(agreement: Agreement) -> int:
    """What is still to be settled: principal − adjustments − paid."""
    if agreement.status in (Agreement.Status.PENDING, Agreement.Status.CANCELLED):
        return 0
    return agreement.principal - adjusted_total(agreement) - paid_total(agreement)


def person_credit(person: Person) -> int:
    """Money received from a person that no agreement owes: we hold it for them (a liability)."""
    total = 0
    for tx in Transaction.objects.filter(
        person=person, status=CONFIRMED, kind__in=[K.INSTALLMENT, K.PAYOUT]
    ).prefetch_related("allocations"):
        unallocated = abs(tx.amount) - sum(abs(a.amount) for a in tx.allocations.all())
        sign = 1 if tx.kind == K.INSTALLMENT else -1
        total += -sign * unallocated if tx.is_reversal else sign * unallocated
    return total


# --- schedules -------------------------------------------------------------------------------------


def _save_version(agreement: Agreement, plan: Sequence[sch.Planned], reason: str, user: UserLike | None) -> None:
    last = agreement.schedule_versions.order_by("-number").first()
    agreement.schedule_versions.filter(is_active=True).update(is_active=False)
    version = ScheduleVersion.objects.create(
        agreement=agreement,
        number=(last.number + 1) if last else 1,
        reason=reason,
        created_by=_user(user),
    )
    Installment.objects.bulk_create(Installment(version=version, due_date=p.due_date, amount=p.amount) for p in plan)


def _check_plan_total(agreement: Agreement, plan: Sequence[sch.Planned]) -> None:
    expected = agreement.principal - adjusted_total(agreement)
    if sum(p.amount for p in plan) != expected:
        raise ServiceError("جمع اقساط باید با مانده قرارداد برابر باشد.")


@db_transaction.atomic
def revise_schedule(agreement: Agreement, new_tail: Sequence[sch.Planned], reason: str, user: UserLike | None) -> None:
    """Renegotiate: replace only the unpaid part of the active schedule (PLAN §3)."""
    agreement = Agreement.objects.select_for_update().get(pk=agreement.pk)
    if agreement.status != Agreement.Status.ACTIVE:
        raise ServiceError("فقط جدول قرارداد جاری قابل تغییر است.")
    if not reason.strip():
        raise ServiceError("دلیل تغییر جدول لازم است.")
    paid = paid_total(agreement)
    current = agreement.plan()
    if current:
        try:
            plan = sch.revise(current, paid, new_tail)
        except sch.ScheduleError as exc:
            raise ServiceError(str(exc)) from exc
    else:  # a subordinated lender getting dates for the first time
        plan = list(new_tail)
        if paid:
            raise ServiceError("این قرارداد جدول ندارد اما پرداخت دارد؛ با مدیر کل هماهنگ کنید.")
    _check_plan_total(agreement, plan)
    _save_version(agreement, plan, reason, user)


def defer(agreement: Agreement, months: int, reason: str, user: UserLike | None) -> None:
    """Deferral tool (PLAN §4.1): push every unpaid installment ``months`` later."""
    tail = [
        sch.Planned(JMonth.of(p.due_date).plus(months).last_day(), p.amount)
        for p in sch.unpaid_part(agreement.plan(), paid_total(agreement))
    ]
    if months <= 0 or not tail:
        raise ServiceError("چیزی برای تعویق وجود ندارد.")
    revise_schedule(agreement, tail, reason, user)


@db_transaction.atomic
def adjust(agreement: Agreement, kind: str, amount: int, reason: str, user: UserLike | None) -> Adjustment:
    """Write off a borrower balance or convert part of a lender balance to a donation (PLAN §4.4, §4.6)."""
    agreement = Agreement.objects.select_for_update().get(pk=agreement.pk)
    expected_kind = {
        Adjustment.Kind.WRITE_OFF: Agreement.Kind.LOAN,
        Adjustment.Kind.TO_DONATION: Agreement.Kind.DEPOSIT,
    }[Adjustment.Kind(kind)]
    if agreement.kind != expected_kind or agreement.status != Agreement.Status.ACTIVE:
        raise ServiceError("این اصلاحیه برای این قرارداد مجاز نیست.")
    if not reason.strip():
        raise ServiceError("دلیل لازم است.")
    left = outstanding(agreement)
    if not 0 < amount <= left:
        raise ServiceError("مبلغ باید مثبت و حداکثر برابر مانده باشد.")
    paid = paid_total(agreement)
    plan = agreement.plan()
    adjustment = Adjustment.objects.create(
        agreement=agreement, kind=kind, amount=amount, reason=reason, created_by=_user(user)
    )
    if plan:
        _save_version(agreement, sch.reduce_from_end(plan, paid, amount), reason, user)
    if amount == left:
        agreement.status = (
            Agreement.Status.WRITTEN_OFF if kind == Adjustment.Kind.WRITE_OFF else Agreement.Status.SETTLED
        )
        agreement.save(update_fields=["status"])
    return adjustment


# --- loan requests -----------------------------------------------------------------------------------


@db_transaction.atomic
def approve_referrer(request: LoanRequest, user: UserLike | None) -> None:
    person = Person.objects.select_for_update().get(pk=request.person_id)
    if not (person.referred_by_id or person.referrer_text.strip()):
        raise ServiceError("معرف متقاضی مشخص نشده است.")
    if not person.referrer_approved:
        person.referrer_approved_at = timezone.now()
        person.referrer_approved_by = _user(user)
        person.save(update_fields=["referrer_approved_at", "referrer_approved_by"])
    if request.status in (LoanRequest.Status.NEW, LoanRequest.Status.CONTACTING):
        request.status = LoanRequest.Status.REFERRER_APPROVED
        request.save(update_fields=["status"])


@db_transaction.atomic
def approve_loan(request: LoanRequest, amount: int | None, user: UserLike | None) -> Agreement:
    request = LoanRequest.objects.select_for_update().select_related("person").get(pk=request.pk)
    if request.agreement_id or request.status in (
        LoanRequest.Status.APPROVED,
        LoanRequest.Status.DISBURSED,
        LoanRequest.Status.REJECTED,
        LoanRequest.Status.WITHDRAWN,
    ):
        raise ServiceError("این درخواست قابل تأیید نیست.")
    if not request.person.referrer_approved:
        raise ServiceError("ابتدا معرف باید تأیید شود.")
    amount = amount or request.approved_amount or request.requested_amount
    if amount <= 0:
        raise ServiceError("مبلغ باید مثبت باشد.")
    agreement = Agreement.objects.create(
        person=request.person, kind=Agreement.Kind.LOAN, principal=amount, created_by=_user(user)
    )
    request.approved_amount = amount
    request.agreement = agreement
    request.status = LoanRequest.Status.APPROVED
    request.save(update_fields=["approved_amount", "agreement", "status"])
    return agreement


# --- transactions ------------------------------------------------------------------------------------


@dataclass
class ConfirmResult:
    transaction: Transaction
    warnings: list[str] = field(default_factory=list)


def create_from_sms(
    parsed: ParsedSms, raw_sms: str, fund_account: FundAccount, kind: str, user: UserLike | None, **extra: object
) -> Transaction:
    """Create a draft from a parsed SMS; the same bank event can only be recorded once."""
    key = dedupe_key(parsed.bank, parsed.amount, parsed.occurred_at, parsed.balance)
    if Transaction.objects.filter(dedupe_hash=key).exists():
        raise ServiceError("این پیامک قبلاً ثبت شده است.")
    occurred = timezone.make_aware(parsed.occurred_at) if parsed.occurred_at else timezone.now()
    return Transaction.objects.create(
        fund_account=fund_account,
        kind=kind,
        amount=parsed.amount,
        occurred_at=occurred,
        balance_after=parsed.balance,
        raw_sms=raw_sms,
        dedupe_hash=key,
        created_by=_user(user),
        **extra,
    )


def match_fund_account(parsed: ParsedSms) -> FundAccount | None:
    accounts = FundAccount.objects.filter(is_active=True, bank=parsed.bank)
    if parsed.account:
        hinted = [a for a in accounts if a.account_hint and a.account_hint in parsed.account]
        if len(hinted) == 1:
            return hinted[0]
    return accounts.first() if accounts.count() == 1 else None


def suggest_people(amount: int, on: date | None = None) -> list[tuple[Person, Agreement, int]]:
    """Borrowers whose due or overdue unpaid installment matches ``amount`` (PLAN §5.2)."""
    on = on or today()
    horizon = JMonth.of(on).last_day()
    result = []
    for agreement in Agreement.objects.filter(kind=Agreement.Kind.LOAN, status=Agreement.Status.ACTIVE).select_related(
        "person"
    ):
        for c in sch.cover(agreement.plan(), paid_total(agreement)):
            if c.remaining > 0 and c.due_date <= horizon and amount in (c.remaining, c.amount):
                result.append((agreement.person, agreement, c.remaining))
                break
    return result


def _allocations_for(tx: Transaction, manual: Sequence[tuple[Agreement, int]] | None) -> list[tuple[Agreement, int]]:
    agreement_kind = Agreement.Kind.LOAN if tx.kind == K.INSTALLMENT else Agreement.Kind.DEPOSIT
    open_agreements = list(
        Agreement.objects.select_for_update().filter(
            person=tx.person, kind=agreement_kind, status=Agreement.Status.ACTIVE
        )
    )
    if manual is not None:
        by_pk = {a.pk: a for a in open_agreements}
        result = []
        for agreement, amount in manual:
            if agreement.pk not in by_pk:
                raise ServiceError(f"قرارداد {agreement} جاری نیست یا متعلق به این شخص نیست.")
            if amount <= 0 or amount > outstanding(by_pk[agreement.pk]):
                raise ServiceError(f"مبلغ تخصیص به {agreement} از مانده آن بیشتر است.")
            result.append((by_pk[agreement.pk], amount))
        if sum(a for _, a in result) > tx.amount:
            raise ServiceError("جمع تخصیص‌ها از مبلغ تراکنش بیشتر است.")
        return result
    views = [OpenAgreement(a.pk, a.opened_on or date.min, a.plan(), paid_total(a)) for a in open_agreements]
    fifo = allocate_fifo(tx.amount, views)
    by_pk = {a.pk: a for a in open_agreements}
    return [(by_pk[key], amount) for key, amount in fifo.parts]  # type: ignore[index]


def _activate(tx: Transaction) -> None:
    """A confirmed deposit or disbursement starts its agreement and its default schedule."""
    agreement = Agreement.objects.select_for_update().get(pk=tx.agreement_id)
    expected = Agreement.Kind.DEPOSIT if tx.kind == K.DEPOSIT else Agreement.Kind.LOAN
    if agreement.kind != expected or agreement.person_id != tx.person_id:
        raise ServiceError("نوع یا شخص قرارداد با تراکنش نمی‌خواند.")
    if agreement.status != Agreement.Status.PENDING:
        raise ServiceError("این قرارداد قبلاً شروع شده است.")
    if agreement.principal != tx.amount:
        raise ServiceError("مبلغ تراکنش با مبلغ قرارداد برابر نیست.")
    started = timezone.localtime(tx.occurred_at).date()
    agreement.opened_on = started
    agreement.status = Agreement.Status.ACTIVE
    agreement.save(update_fields=["opened_on", "status"])
    if agreement.kind == Agreement.Kind.LOAN:
        _save_version(agreement, sch.default_loan(agreement.principal, started), "جدول پیش‌فرض", tx.confirmed_by)
        LoanRequest.objects.filter(agreement=agreement).update(status=LoanRequest.Status.DISBURSED)
    elif not agreement.subordinated:
        _save_version(agreement, sch.default_deposit(agreement.principal, started), "جدول پیش‌فرض", tx.confirmed_by)


def _settle_if_done(agreements: Sequence[Agreement]) -> None:
    for agreement in agreements:
        if agreement.status == Agreement.Status.ACTIVE and outstanding(agreement) <= 0:
            agreement.status = Agreement.Status.SETTLED
            agreement.save(update_fields=["status"])


def account_balance(fund_account: FundAccount, until: datetime | None = None) -> int:
    qs = Transaction.objects.filter(fund_account=fund_account, status=CONFIRMED)
    if until is not None:
        qs = qs.filter(occurred_at__lte=until)
    totals = qs.aggregate(i=Sum("amount", filter=Q(direction="in")), o=Sum("amount", filter=Q(direction="out")))
    return (totals["i"] or 0) - (totals["o"] or 0)


@db_transaction.atomic
def confirm(
    tx: Transaction, user: UserLike | None, allocations: Sequence[tuple[Agreement, int]] | None = None
) -> ConfirmResult:
    """Confirm a draft (any admin may; PLAN §4.7). Allocations default to FIFO (PLAN §3)."""
    tx = Transaction.objects.select_for_update().get(pk=tx.pk)
    if tx.is_confirmed:
        raise ServiceError("این تراکنش قبلاً تأیید شده است.")
    if tx.amount <= 0:
        raise ServiceError("مبلغ باید مثبت باشد.")
    actor = _user(user)
    if tx.kind == K.OPENING and not (actor is not None and actor.has_perm("fund.record_opening_balance")):
        raise ServiceError("ثبت مانده اول دوره فقط با مدیر کل است.")
    tx.confirmed_by = actor
    tx.confirmed_at = timezone.now()
    result = ConfirmResult(tx)

    if tx.kind in (K.DEPOSIT, K.DISBURSEMENT):
        if tx.agreement_id is None or tx.person_id is None:
            raise ServiceError("شخص و قرارداد این تراکنش باید مشخص باشد.")
        _activate(tx)
    elif tx.kind in (K.INSTALLMENT, K.PAYOUT):
        if tx.person_id is None:
            raise ServiceError("شخص این تراکنش باید مشخص باشد.")
        if allocations is None and tx.agreement_id is not None:
            agreement = Agreement.objects.get(pk=tx.agreement_id)
            allocations = [(agreement, min(tx.amount, outstanding(agreement)))]
        parts = _allocations_for(tx, allocations)
        leftover = tx.amount - sum(a for _, a in parts)
        if tx.kind == K.PAYOUT and leftover > person_credit(tx.person):  # type: ignore[arg-type]
            raise ServiceError("این برداشت از بدهی صندوق به این شخص بیشتر است.")
        Allocation.objects.bulk_create(Allocation(transaction=tx, agreement=a, amount=amt) for a, amt in parts)
        _settle_if_done([a for a, _ in parts])
        if tx.kind == K.INSTALLMENT and leftover:
            result.warnings.append(f"{leftover:,} ریال اضافه پرداخت به‌عنوان اعتبار این شخص نگه داشته شد.")

    tx.status = CONFIRMED
    tx.save()
    if tx.balance_after is not None:
        expected = account_balance(tx.fund_account, until=tx.occurred_at)
        if expected != tx.balance_after:
            result.warnings.append(
                f"مغایرت با بانک: مانده طبق دفتر {expected:,} ریال، طبق پیامک {tx.balance_after:,} ریال."
            )
    return result


@db_transaction.atomic
def record_and_confirm(tx: Transaction, user: UserLike | None, **kwargs: object) -> ConfirmResult:
    tx.created_by = tx.created_by or _user(user)
    tx.save()
    return confirm(tx, user, **kwargs)  # type: ignore[arg-type]


@db_transaction.atomic
def reverse(tx: Transaction, reason: str, user: UserLike | None) -> Transaction:
    """Cancel a confirmed transaction with a mirror record; the original is never edited (CLAUDE.md)."""
    tx = Transaction.objects.select_for_update().get(pk=tx.pk)
    if not tx.is_confirmed:
        raise ServiceError("پیش‌نویس را می‌توان ویرایش کرد؛ برگشت فقط برای تراکنش تأییدشده است.")
    if tx.is_reversal or hasattr(tx, "reversal"):
        raise ServiceError("این تراکنش برگشت خورده یا خودش برگشت است.")
    if not reason.strip():
        raise ServiceError("دلیل برگشت لازم است.")
    if tx.kind == K.OPENING and not (user is not None and user.has_perm("fund.record_opening_balance")):
        raise ServiceError("برگشت مانده اول دوره فقط با مدیر کل است.")
    if tx.kind in (K.DEPOSIT, K.DISBURSEMENT) and tx.agreement is not None:
        agreement = Agreement.objects.select_for_update().get(pk=tx.agreement_id)
        if paid_total(agreement) != 0 or agreement.adjustments.exists():
            raise ServiceError("ابتدا تراکنش‌های قسط/بازپرداخت این قرارداد را برگشت بزنید.")
        agreement.status = Agreement.Status.PENDING
        agreement.opened_on = None
        agreement.save(update_fields=["status", "opened_on"])
        agreement.schedule_versions.filter(is_active=True).update(is_active=False)
        LoanRequest.objects.filter(agreement=agreement).update(status=LoanRequest.Status.APPROVED)
    now = timezone.now()
    mirror = Transaction.objects.create(
        fund_account=tx.fund_account,
        kind=tx.kind,
        amount=tx.amount,
        occurred_at=now,
        person=tx.person,
        agreement=tx.agreement,
        reverses=tx,
        note=reason,
        status=CONFIRMED,
        created_by=_user(user),
        confirmed_by=_user(user),
        confirmed_at=now,
    )
    Allocation.objects.bulk_create(
        Allocation(transaction=mirror, agreement=a.agreement, amount=-a.amount) for a in tx.allocations.all()
    )
    for a in {a.agreement for a in tx.allocations.select_related("agreement")}:
        if a.status == Agreement.Status.SETTLED and outstanding(a) > 0:
            a.status = Agreement.Status.ACTIVE
            a.save(update_fields=["status"])
    return mirror


# --- reports -----------------------------------------------------------------------------------------


def fund_buckets() -> ledger.Buckets:
    moves: list[ledger.TransactionMove | ledger.AdjustmentMove] = []
    for tx in Transaction.objects.filter(status=CONFIRMED).prefetch_related("allocations"):
        if tx.kind in (K.DEPOSIT, K.DISBURSEMENT):
            allocated = tx.amount
        else:
            allocated = sum(abs(a.amount) for a in tx.allocations.all())
        moves.append(ledger.TransactionMove(tx.ledger_kind, tx.amount, allocated, reversal=tx.is_reversal))
    for adj in Adjustment.objects.all():
        moves.append(ledger.AdjustmentMove(ledger.AdjustmentKind(adj.kind), adj.amount))
    return ledger.totals(moves)


def cash_on_hand() -> int:
    return sum(account_balance(a) for a in FundAccount.objects.all())


def pending_disbursements() -> int:
    return (
        Agreement.objects.filter(kind=Agreement.Kind.LOAN, status=Agreement.Status.PENDING).aggregate(
            s=Sum("principal")
        )["s"]
        or 0
    )


def _active(kind: str) -> list[Agreement]:
    return list(Agreement.objects.filter(kind=kind, status=Agreement.Status.ACTIVE).select_related("person"))


def surplus() -> fc.SurplusAlert | None:
    """PLAN §4.3: only today's cash counts, never what borrowers still owe."""
    regular_remaining = 0
    subordinated = []
    for a in _active(Agreement.Kind.DEPOSIT):
        if a.subordinated and a.active_version is None:
            subordinated.append(fc.Subordinated(a, a.opened_on or date.min, outstanding(a)))
        else:
            regular_remaining += outstanding(a)
    return fc.surplus_alert(cash_on_hand(), pending_disbursements(), regular_remaining, subordinated)


@dataclass
class OverdueRow:
    agreement: Agreement
    standing: sch.Standing
    missed: list[sch.Covered]

    @property
    def amount(self) -> int:
        return sum(c.remaining for c in self.missed)


def overdue(on: date | None = None) -> list[OverdueRow]:
    on = on or today()
    rows = []
    for a in _active(Agreement.Kind.LOAN):
        plan, paid = a.plan(), paid_total(a)
        missed = sch.missed(plan, paid, on)
        if missed:
            rows.append(OverdueRow(a, sch.standing(plan, paid, on), missed))
    return rows


def monthly_forecast(on: date | None = None, horizon: int = 12, margin: int = 0) -> fc.Forecast:
    on = on or today()
    flows = []
    for a in _active(Agreement.Kind.LOAN):
        plan, paid = a.plan(), paid_total(a)
        flows.append(
            fc.ScheduledFlow(plan, paid, incoming=True, collectible=sch.standing(plan, paid, on) != sch.Standing.REVIEW)
        )
    for a in _active(Agreement.Kind.DEPOSIT):
        if a.active_version is not None:
            flows.append(fc.ScheduledFlow(a.plan(), paid_total(a), incoming=False))
    month = JMonth.of(on)
    pledges = []
    for a in _active(Agreement.Kind.PLEDGE):
        first = JMonth.of(a.opened_on or on)
        last = first.plus(a.pledge_months - 1) if a.pledge_months else None
        received = (
            Transaction.objects.filter(
                person=a.person,
                kind=K.PLEDGE,
                status=CONFIRMED,
                reverses__isnull=True,
                reversal__isnull=True,
                occurred_at__date__gte=month.first_day(),
                occurred_at__date__lte=month.last_day(),
            ).aggregate(s=Sum("amount"))["s"]
            or 0
        )
        pledges.append(fc.Pledge(a.principal, first, last, received))
    return fc.forecast(on, cash_on_hand(), flows, pledges, pending_disbursements(), horizon=horizon, margin=margin)
