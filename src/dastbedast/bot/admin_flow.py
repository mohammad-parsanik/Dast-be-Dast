"""Admin side of the bot (PLAN §5.2): the three entry buttons, the loan-request queue and the digest.

Every money movement ends in an explicit admin tap on «ثبت و تأیید» (or «فقط پیش‌نویس»), and goes
through ``fund.services`` exactly as in the web panel.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from django.contrib.auth.models import AbstractBaseUser
from django.db import transaction as db_transaction
from django.utils import timezone

from dastbedast.core import ledger
from dastbedast.core.bot_flow import PURPOSES, REPAYMENTS, Reply, parse_tomans
from dastbedast.core.identity import mask_iban
from dastbedast.core.jalali import JMonth, format_jalali
from dastbedast.core.money import format_tomans
from dastbedast.core.sms import Bank, Direction, ParsedSms, SmsParseError, dedupe_key, parse_sms
from dastbedast.fund import services
from dastbedast.fund.models import Agreement, FundAccount, LoanRequest, Person, Transaction

from . import digest, ui
from .bale import BotApi
from .models import BotSession

K = ledger.Kind
S = LoanRequest.Status
FLOW = "admin"

IN_INSTALLMENT = "دریافت قسط"
IN_NEW = "وام / اهدای جدید"
OUT = "پرداخت انجام شد"
REQUESTS = "درخواست‌ها"
DIGEST = "خلاصه روز"
MENU = ((IN_INSTALLMENT, IN_NEW), (OUT,), (REQUESTS, DIGEST))
MODES = {IN_INSTALLMENT: "installment", IN_NEW: "in_new", OUT: "out"}

KINDS = {
    "in_new": [
        ("deposit", "قرض‌دهی"),
        ("subdeposit", "قرض‌دهی (آخر از همه)"),
        ("donation", "اهدا"),
        ("pledge", "کمک ماهانه حامی"),
        ("other_in", "سایر واریز"),
    ],
    "out": [
        ("disbursement", "پرداخت وام"),
        ("payout", "بازپرداخت به وام‌دهنده"),
        ("fee", "کارمزد بانک"),
        ("other_out", "سایر برداشت"),
    ],
}
KIND_LABELS = {k: v for pairs in KINDS.values() for k, v in pairs} | {"installment": "دریافت قسط"}
NEEDS_PERSON = {"installment", "deposit", "subdeposit", "pledge", "payout"}
OPTIONAL_PERSON = {"donation"}
CANCEL_ROW = [("✘ انصراف", "x")]


def menu(text: str) -> Reply:
    return Reply(text, MENU)


@dataclass
class AdminChat:
    api: BotApi
    session: BotSession
    user: AbstractBaseUser
    chat_id: int

    # --- plumbing ---------------------------------------------------------------------------------

    def say(self, text: str, buttons: list[list[tuple[str, str]]] | None = None) -> None:
        if buttons:
            ui.send(self.api, self.chat_id, Reply(text), ui.inline(buttons))
        else:
            ui.send(self.api, self.chat_id, menu(text))

    def edit(self, message_id: int | None, text: str, buttons: list[list[tuple[str, str]]] | None = None) -> None:
        if message_id is None:
            self.say(text, buttons)
            return
        self.api.call(
            "editMessageText",
            chat_id=self.chat_id,
            message_id=message_id,
            text=text[:4096],
            reply_markup=ui.inline(buttons) if buttons else None,
        )

    @property
    def data(self) -> dict[str, Any]:
        return self.session.data

    def step(self, step: str, **updates: Any) -> None:
        self.session.go(FLOW, step, {**self.data, **updates})

    def done(self, text: str) -> None:
        self.session.reset()
        self.say(text)

    def can(self, perm: str) -> bool:
        return self.user.has_perm(perm)  # type: ignore[attr-defined]

    # --- entry points -----------------------------------------------------------------------------

    def on_start(self) -> None:
        self.session.reset()
        self.say("منوی مدیریت صندوق. یکی از گزینه‌ها را انتخاب کنید.")

    def on_text(self, text: str) -> None:
        text = text.strip()
        if text in MODES:
            if not self.can("fund.add_transaction"):
                self.done("اجازه ثبت تراکنش ندارید.")
                return
            self.session.go(FLOW, "sms", {"mode": MODES[text]})
            self.say("متن پیامک بانک را اینجا بچسبانید.", [CANCEL_ROW])
        elif text == REQUESTS:
            self.session.reset()
            self.list_requests()
        elif text == DIGEST:
            self.session.reset()
            self.say(digest.build())
        elif self.session.flow == FLOW and self.session.step == "sms":
            self.on_sms(text)
        elif self.session.flow == FLOW and self.session.step == "person":
            self.search_people(text)
        elif self.session.flow == FLOW and self.session.step == "newname":
            self.on_new_name(text)
        elif self.session.flow == FLOW and self.session.step == "req_amount":
            self.on_request_amount(text)
        else:
            self.say("یکی از گزینه‌های منو را انتخاب کنید.")

    def on_callback(self, data: str, message_id: int | None) -> None:
        head, _, rest = data.partition(":")
        if head == "x":
            self.session.reset()
            self.edit(message_id, "لغو شد.")
        elif head == "rq":
            self.on_request_action(rest, message_id)
        elif self.session.flow != FLOW:
            self.edit(message_id, "این گفتگو تمام شده است؛ از منو دوباره شروع کنید.")
        elif head == "fa" and self.session.step == "account":
            self.step("account", account=int(rest))
            self.after_account(message_id)
        elif head == "k" and self.session.step == "kind":
            self.step("kind", kind=rest)
            self.after_kind(message_id)
        elif head == "pp" and self.session.step == "person":
            self.step("person", person=int(rest), new_name=None)
            self.review(message_id)
        elif head == "np" and self.session.step == "person":
            self.step("newname")
            self.edit(message_id, "نام کامل شخص جدید را بنویسید.", [CANCEL_ROW])
        elif head == "nobody" and self.session.step == "person":
            self.step("person", person=None, new_name=None)
            self.review(message_id)
        elif head == "ag" and self.session.step == "agreement":
            self.step("agreement", agreement=int(rest))
            self.review(message_id)
        elif head in ("ok", "dr") and self.session.step == "review":
            self.record(confirm=head == "ok", message_id=message_id)
        else:
            self.edit(message_id, "این دکمه دیگر معتبر نیست.")

    # --- recording a transaction from a bank SMS -------------------------------------------------

    def on_sms(self, text: str) -> None:
        try:
            parsed = parse_sms(text, services.today())
        except SmsParseError:
            self.say("پیامک شناخته نشد. متن کامل پیامک بانک را بچسبانید یا تراکنش را در پنل ثبت کنید.", [CANCEL_ROW])
            return
        mode = self.data["mode"]
        expected = Direction.OUT if mode == "out" else Direction.IN
        if parsed.direction is not None and parsed.direction != expected:
            word = "برداشت" if parsed.direction == Direction.OUT else "واریز"
            self.say(f"این پیامک {word} است و با این گزینه نمی‌خواند.", [CANCEL_ROW])
            return
        key = dedupe_key(parsed.bank, parsed.amount, parsed.occurred_at, parsed.balance)
        if Transaction.objects.filter(dedupe_hash=key).exists():
            self.done("این پیامک قبلاً ثبت شده است.")
            return
        sms = {
            "raw": text,
            "bank": parsed.bank.value,
            "direction": expected.value,
            "amount": parsed.amount,
            "balance": parsed.balance,
            "occurred_at": parsed.occurred_at.isoformat() if parsed.occurred_at else None,
            "account": parsed.account,
            "channel": parsed.channel,
        }
        account = services.match_fund_account(parsed)
        self.step("account", sms=sms, account=account.pk if account else None)
        if account is None:
            choices = list(FundAccount.objects.filter(is_active=True))
            if not choices:
                self.done("هیچ حساب صندوقی تعریف نشده است؛ مدیر کل باید در پنل اضافه کند.")
                return
            buttons = [[(a.label, f"fa:{a.pk}")] for a in choices]
            self.say("این پیامک مربوط به کدام حساب صندوق است؟", [*buttons, CANCEL_ROW])
            return
        self.after_account(None)

    def after_account(self, message_id: int | None) -> None:
        mode = self.data["mode"]
        if mode == "installment":
            self.step("kind", kind="installment")
            self.after_kind(message_id)
            return
        self.step("kind")
        buttons = [[(label, f"k:{key}")] for key, label in KINDS[mode]]
        self.edit(
            message_id, f"مبلغ {format_tomans(self.data['sms']['amount'])} تومان. نوع تراکنش؟", [*buttons, CANCEL_ROW]
        )

    def after_kind(self, message_id: int | None) -> None:
        kind = self.data["kind"]
        amount = self.data["sms"]["amount"]
        if kind == "disbursement":
            loans = sorted(
                Agreement.objects.filter(kind=Agreement.Kind.LOAN, status=Agreement.Status.PENDING).select_related(
                    "person"
                ),
                key=lambda a: a.principal != amount,
            )[:8]
            if not loans:
                self.session.reset()
                self.edit(message_id, "وام تأییدشده‌ای منتظر پرداخت نیست؛ ابتدا درخواست را تأیید کنید.")
                return
            buttons = [[(f"{a.person} — {format_tomans(a.principal)}", f"ag:{a.pk}")] for a in loans]
            self.step("agreement")
            self.edit(message_id, "پرداخت کدام وام است؟", [*buttons, CANCEL_ROW])
        elif kind in NEEDS_PERSON or kind in OPTIONAL_PERSON:
            self.step("person")
            self.edit(message_id, *self.person_prompt(kind, amount))
        else:
            self.step("person", person=None)
            self.review(message_id)

    def person_prompt(self, kind: str, amount: int) -> tuple[str, list[list[tuple[str, str]]]]:
        found: list[Person] = []
        if kind == "installment":
            found = [p for p, _, _ in services.suggest_people(amount)]
            text = "پیشنهاد بر اساس اقساط سررسیدشده با همین مبلغ. اگر نیست، بخشی از نام را بنویسید."
        elif kind == "payout":
            due = digest.due_by(Agreement.Kind.DEPOSIT, JMonth.of(services.today()).last_day())
            found = [a.person for a, due_amount in sorted(due, key=lambda r: r[1] != amount)]
            text = "وام‌دهندگان با بازپرداخت سررسیدشده. اگر نیست، بخشی از نام را بنویسید."
        elif kind == "pledge":
            found = [
                a.person for a in Agreement.objects.filter(kind=Agreement.Kind.PLEDGE, status=Agreement.Status.ACTIVE)
            ]
            text = "حامیان ماهانه. اگر نیست، بخشی از نام را بنویسید."
        else:
            text = "بخشی از نام شخص را بنویسید تا جستجو شود."
        return text, self.person_buttons(kind, found)

    def person_buttons(self, kind: str, people: list[Person]) -> list[list[tuple[str, str]]]:
        unique = list(dict.fromkeys(people))[:8]
        buttons = [[(str(p), f"pp:{p.pk}")] for p in unique]
        if kind in ("deposit", "subdeposit", "donation", "pledge"):
            buttons.append([("➕ شخص جدید", "np")])
        if kind in OPTIONAL_PERSON:
            buttons.append([("بدون نام", "nobody")])
        return [*buttons, CANCEL_ROW]

    def search_people(self, text: str) -> None:
        kind = self.data["kind"]
        people = list(Person.objects.filter(name__icontains=text)[:8])
        message = "نتیجه جستجو:" if people else "کسی با این نام پیدا نشد. دوباره بنویسید."
        self.say(message, self.person_buttons(kind, people))

    def on_new_name(self, text: str) -> None:
        if len(text) < 3 or text.startswith("/"):
            self.say("نام کامل را بنویسید.", [CANCEL_ROW])
            return
        self.step("person", person=None, new_name=text[:200])
        self.review(None)

    def review(self, message_id: int | None) -> None:
        d = self.data
        sms = d["sms"]
        lines = [
            f"نوع: {KIND_LABELS[d['kind']]}",
            f"مبلغ: {format_tomans(sms['amount'])} تومان",
            f"حساب: {FundAccount.objects.get(pk=d['account']).label}",
        ]
        if sms["occurred_at"]:
            when = datetime.fromisoformat(sms["occurred_at"])
            lines.append(f"زمان: {format_jalali(when.date())} {when:%H:%M}")
        else:
            lines.append("زمان: از پیامک خوانده نشد؛ زمان فعلی ثبت می‌شود.")
        if sms["balance"] is not None:
            lines.append(f"مانده طبق بانک: {format_tomans(sms['balance'])}")
        if d.get("agreement"):
            agreement = Agreement.objects.select_related("person").get(pk=d["agreement"])
            lines.append(f"وام: {agreement.person} — {format_tomans(agreement.principal)}")
        elif d.get("person"):
            lines.append(f"شخص: {Person.objects.get(pk=d['person'])}")
        elif d.get("new_name"):
            lines.append(f"شخص جدید: {d['new_name']}")
        if d["kind"] in ("installment", "payout"):
            lines.append("تخصیص: به قدیمی‌ترین قسط‌های باز این شخص (FIFO).")
        if d["kind"] == "subdeposit":
            lines.append("بدون تاریخ بازپرداخت؛ «آخر از همه» پرداخت می‌شود.")
        self.step("review")
        buttons = [[("✔ ثبت و تأیید", "ok")], [("📝 فقط پیش‌نویس", "dr")], CANCEL_ROW]
        self.edit(message_id, "\n".join(lines), buttons)

    def record(self, confirm: bool, message_id: int | None) -> None:
        if confirm and not self.can("fund.change_transaction"):
            self.edit(message_id, "اجازه تأیید تراکنش ندارید.")
            return
        d = self.data
        sms = d["sms"]
        parsed = ParsedSms(
            bank=Bank(sms["bank"]),
            direction=Direction(sms["direction"]),
            amount=sms["amount"],
            balance=sms["balance"],
            occurred_at=datetime.fromisoformat(sms["occurred_at"]) if sms["occurred_at"] else None,
            account=sms["account"],
            channel=sms["channel"],
        )
        kind = "deposit" if d["kind"] == "subdeposit" else d["kind"]
        try:
            with db_transaction.atomic():
                person = Person.objects.get(pk=d["person"]) if d.get("person") else None
                if person is None and d.get("new_name"):
                    person = Person.objects.create(name=d["new_name"])
                agreement = None
                if kind == K.DEPOSIT:
                    agreement = Agreement.objects.create(
                        person=person,
                        kind=Agreement.Kind.DEPOSIT,
                        principal=parsed.amount,
                        subordinated=d["kind"] == "subdeposit",
                        created_by=self.user,
                    )
                elif kind == K.DISBURSEMENT:
                    agreement = Agreement.objects.get(pk=d["agreement"])
                    person = agreement.person
                fund_account = FundAccount.objects.get(pk=d["account"])
                tx = services.create_from_sms(
                    parsed, sms["raw"], fund_account, kind, self.user, person=person, agreement=agreement
                )
                result = services.confirm(tx, self.user) if confirm else None
        except services.ServiceError as exc:
            self.session.reset()
            self.edit(message_id, f"ثبت نشد: {exc}")
            self.say("از منو دوباره شروع کنید.")
            return
        self.session.reset()
        if result is None:
            text = f"پیش‌نویس #{tx.pk} ثبت شد و منتظر تأیید است."
        else:
            text = "\n".join([f"تراکنش #{tx.pk} ثبت و تأیید شد.", *result.warnings])
        self.edit(message_id, text)

    # --- loan requests ------------------------------------------------------------------------------

    def list_requests(self) -> None:
        if not self.can("fund.view_loanrequest"):
            self.say("اجازه دیدن درخواست‌ها را ندارید.")
            return
        open_statuses = [S.NEW, S.CONTACTING, S.REFERRER_APPROVED, S.APPROVED]
        requests = list(
            LoanRequest.objects.filter(status__in=open_statuses)
            .select_related("person", "bank_account")
            .order_by("created_at")[:15]
        )
        if not requests:
            self.say("درخواست بازی نیست.")
            return
        for r in requests:
            self.say(request_card(r), request_buttons(r) or None)

    def on_request_action(self, rest: str, message_id: int | None) -> None:
        action, _, pk = rest.partition(":")
        request = LoanRequest.objects.select_related("person", "bank_account").filter(pk=int(pk or 0)).first()
        if request is None:
            self.edit(message_id, "درخواست پیدا نشد.")
            return
        if not self.can("fund.change_loanrequest"):
            self.edit(message_id, "اجازه تغییر درخواست را ندارید.")
            return
        card = request_card(request)
        try:
            if action == "c":
                services.mark_contacting(request)
            elif action == "r":
                services.approve_referrer(request, self.user)
            elif action == "a":
                shown = format_tomans(request.requested_amount)
                yes = [("✔ بله، تأیید", f"rq:A:{pk}"), ("بازگشت", f"rq:v:{pk}")]
                self.edit(message_id, f"{card}\n\nتأیید وام با {shown} تومان؟", [yes])
                return
            elif action in ("A", "B"):
                amount = self.data.get("amount") if action == "B" else None
                if action == "B" and (self.data.get("request") != request.pk or not amount):
                    raise services.ServiceError("مبلغ را دوباره وارد کنید.")
                services.approve_loan(request, amount, self.user)
                self.session.reset()
            elif action == "m":
                self.session.go(FLOW, "req_amount", {"request": request.pk})
                self.say(f"مبلغ تأییدشده برای درخواست #{request.pk} را به تومان بنویسید.", [CANCEL_ROW])
                return
            elif action == "x":
                yes = [("✘ بله، رد شود", f"rq:X:{pk}"), ("بازگشت", f"rq:v:{pk}")]
                self.edit(message_id, f"{card}\n\nاین درخواست رد شود؟", [yes])
                return
            elif action == "X":
                services.reject_request(request)
        except services.ServiceError as exc:
            self.edit(message_id, f"{card}\n\n⚠ {exc}", request_buttons(request) or None)
            return
        request.refresh_from_db()
        self.edit(message_id, request_card(request), request_buttons(request) or None)

    def on_request_amount(self, text: str) -> None:
        try:
            amount = parse_tomans(text)
        except ValueError:
            self.say("مبلغ را با عدد و به تومان بنویسید.", [CANCEL_ROW])
            return
        pk = self.data["request"]
        self.step("req_amount", amount=amount)
        yes = [("✔ بله، تأیید", f"rq:B:{pk}"), ("✘ انصراف", "x")]
        self.say(f"تأیید درخواست #{pk} با {format_tomans(amount)} تومان؟", [yes])


def request_card(r: LoanRequest) -> str:
    person = r.person
    referrer = person.referred_by or person.referrer_text or "—"
    lines = [
        f"درخواست #{r.pk} — {r.get_status_display()}",
        f"متقاضی: {person}",
        f"موبایل: {person.phone or '—'}",
        f"معرف: {referrer}" + (" ✔" if person.referrer_approved else ""),
        f"مبلغ درخواستی: {format_tomans(r.requested_amount)} تومان",
    ]
    if r.approved_amount:
        lines.append(f"مبلغ تأییدشده: {format_tomans(r.approved_amount)} تومان")
    lines.append(f"مصرف: {PURPOSES.get(r.purpose, r.purpose)} — {r.description}")
    lines.append(f"بازپرداخت: {REPAYMENTS.get(r.repayment_choice, r.repayment_choice)}")
    if r.bank_account:
        lines.append(f"حساب: {r.bank_account.holder_name} ({mask_iban(r.bank_account.iban)})")
    if r.notes:
        lines.append(f"توضیحات: {r.notes}")
    lines.append(f"تاریخ: {format_jalali(timezone.localtime(r.created_at).date())}")
    if r.status == S.APPROVED:
        lines.append("پس از واریز، «پرداخت انجام شد» را بزنید و پیامک را بچسبانید.")
    return "\n".join(lines)


def request_buttons(r: LoanRequest) -> list[list[tuple[str, str]]]:
    pk = r.pk
    reject = ("✘ رد", f"rq:x:{pk}")
    if r.status == S.NEW:
        return [[("📞 در حال تماس", f"rq:c:{pk}"), ("✔ تأیید معرف", f"rq:r:{pk}")], [reject]]
    if r.status == S.CONTACTING:
        return [[("✔ تأیید معرف", f"rq:r:{pk}")], [reject]]
    if r.status == S.REFERRER_APPROVED:
        return [[("✔ تأیید وام", f"rq:a:{pk}"), ("مبلغ دیگر", f"rq:m:{pk}")], [reject]]
    return []


def notify_new_request(api: BotApi, request: LoanRequest) -> None:
    from .models import BotAdmin

    for admin in BotAdmin.objects.filter(is_active=True, user__is_active=True):
        ui.send(
            api,
            admin.bale_user_id,
            Reply("درخواست وام جدید:\n" + request_card(request)),
            ui.inline(request_buttons(request)),
        )
