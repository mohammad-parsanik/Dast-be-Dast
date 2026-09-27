"""Back-office screens. Every money-changing button calls ``services``; nothing writes money models directly."""

from __future__ import annotations

from typing import Any

from django import forms
from django.conf import settings
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.db.models import QuerySet
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404, render
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import format_html, format_html_join
from simple_history.admin import SimpleHistoryAdmin

from dastbedast.core import ledger
from dastbedast.core import schedule as sch
from dastbedast.core.identity import mask_iban, mask_phone
from dastbedast.core.jalali import JMonth, format_jalali
from dastbedast.core.money import format_tomans
from dastbedast.core.sms import SmsParseError, dedupe_key, parse_sms

from . import services
from .forms import JalaliDateFormField, TomanAmountField
from .models import (
    Adjustment,
    Agreement,
    BankAccount,
    FundAccount,
    LoanRequest,
    Person,
    ScheduleVersion,
    Transaction,
)

K = ledger.Kind


def tomans(value: int | None) -> str:
    return "—" if value is None else format_tomans(value)


def jdate(value: Any) -> str:
    if value is None:
        return "—"
    if hasattr(value, "date"):
        value = timezone.localtime(value).date() if timezone.is_aware(value) else value.date()
    return format_jalali(value)


def run_service(request: HttpRequest, fn: Any, *args: Any, **kwargs: Any) -> Any:
    """Call a service and turn rule violations into an admin error message."""
    try:
        result = fn(*args, **kwargs)
    except services.ServiceError as exc:
        messages.error(request, str(exc))
        return None
    for warning in getattr(result, "warnings", []):
        messages.warning(request, warning)
    return result


# --- people -------------------------------------------------------------------------------------------


class BankAccountInline(admin.TabularInline):
    model = BankAccount
    extra = 0
    fields = ("iban", "holder_name", "is_active")


@admin.register(Person)
class PersonAdmin(SimpleHistoryAdmin):
    list_display = ("name", "masked_phone", "referrer", "referrer_approved", "muted")
    list_filter = ("muted",)
    search_fields = ("name", "referrer_text")
    autocomplete_fields = ("referred_by",)
    readonly_fields = ("referrer_approved_at", "referrer_approved_by")
    inlines = [BankAccountInline]

    @admin.display(description="موبایل")
    def masked_phone(self, obj: Person) -> str:
        return mask_phone(obj.phone) if obj.phone else "—"

    @admin.display(description="معرف")
    def referrer(self, obj: Person) -> str:
        return str(obj.referred_by or obj.referrer_text or "—")

    @admin.display(description="معرف تأییدشده", boolean=True)
    def referrer_approved(self, obj: Person) -> bool:
        return obj.referrer_approved


@admin.register(FundAccount)
class FundAccountAdmin(SimpleHistoryAdmin):
    list_display = ("label", "bank", "account_hint", "is_active", "balance")

    @admin.display(description="مانده طبق دفتر (تومان)")
    def balance(self, obj: FundAccount) -> str:
        return tomans(services.account_balance(obj))


# --- agreements ---------------------------------------------------------------------------------------


class ScheduleForm(forms.Form):
    ACTIONS = [
        ("defer", "تعویق همه اقساط باقی‌مانده"),
        ("equal", "تقسیم مانده به اقساط مساوی"),
        ("balloon", "یکجا در یک ماه مشخص"),
    ]
    action = forms.ChoiceField(label="نوع تغییر", choices=ACTIONS)
    months = forms.IntegerField(label="تعداد ماه (تعویق) / تعداد قسط", min_value=1, initial=1)
    first_due = JalaliDateFormField(label="اولین سررسید (یا سررسید یکجا)", required=False)
    reason = forms.CharField(label="دلیل", max_length=300)


class AdjustForm(forms.Form):
    amount = TomanAmountField(label="مبلغ")
    reason = forms.CharField(label="دلیل", max_length=300)


@admin.register(Agreement)
class AgreementAdmin(SimpleHistoryAdmin):
    list_display = ("id", "person", "kind", "status", "principal_t", "outstanding_t", "standing", "opened")
    list_filter = ("kind", "status", "subordinated")
    search_fields = ("person__name",)
    autocomplete_fields = ("person",)
    fields = (
        "person",
        "kind",
        "principal",
        "subordinated",
        "opened_on",
        "pledge_months",
        "status",
        "notes",
        "schedule_table",
        "tools",
    )

    def get_fields(self, request: HttpRequest, obj: Agreement | None = None) -> Any:
        if obj is not None and obj.status != Agreement.Status.PENDING:
            return [("opened" if f == "opened_on" else f) for f in self.fields]
        return self.fields

    def get_readonly_fields(self, request: HttpRequest, obj: Agreement | None = None) -> Any:
        readonly = ["status", "schedule_table", "tools", "opened"]
        if obj is not None and obj.status != Agreement.Status.PENDING and not request.user.is_superuser:
            readonly.extend(Agreement.CORE_FIELDS)
        return readonly

    def save_model(self, request: HttpRequest, obj: Agreement, form: Any, change: bool) -> None:
        if not change:
            obj.created_by = request.user
            if obj.kind == Agreement.Kind.PLEDGE:
                obj.status = Agreement.Status.ACTIVE
        super().save_model(request, obj, form, change)

    @admin.display(description="مبلغ")
    def principal_t(self, obj: Agreement) -> str:
        return tomans(obj.principal)

    @admin.display(description="مانده")
    def outstanding_t(self, obj: Agreement) -> str:
        return tomans(services.outstanding(obj)) if obj.kind != Agreement.Kind.PLEDGE else "—"

    @admin.display(description="شروع")
    def opened(self, obj: Agreement) -> str:
        return jdate(obj.opened_on)

    @admin.display(description="وضعیت پرداخت")
    def standing(self, obj: Agreement) -> str:
        if obj.kind != Agreement.Kind.LOAN or obj.status != Agreement.Status.ACTIVE:
            return "—"
        labels = {"current": "مرتب", "follow_up": "پیگیری", "review": "بررسی"}
        return labels[sch.standing(obj.plan(), services.paid_total(obj), services.today())]

    @admin.display(description="جدول اقساط فعال")
    def schedule_table(self, obj: Agreement | None) -> str:
        if obj is None or obj.pk is None:
            return "—"
        version = obj.active_version
        if version is None:
            return "بدون جدول" + (" (آخر از همه)" if obj.subordinated else "")
        rows = sch.cover(version.plan(), services.paid_total(obj))
        body = format_html_join(
            "",
            "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>",
            ((c.seq, jdate(c.due_date), tomans(c.amount), tomans(c.paid)) for c in rows),
        )
        return format_html(
            "<p>نسخه {} — {}</p><table><tr><th>#</th><th>سررسید</th><th>مبلغ</th><th>پرداخت‌شده</th></tr>{}</table>",
            version.number,
            version.reason,
            body,
        )

    @admin.display(description="ابزارها")
    def tools(self, obj: Agreement | None) -> str:
        if obj is None or obj.pk is None or obj.status != Agreement.Status.ACTIVE or obj.kind == "pledge":
            return "—"
        links = [(reverse("admin:fund_agreement_schedule", args=[obj.pk]), "تغییر جدول / تعویق")]
        adjust_label = "سوخت" if obj.kind == Agreement.Kind.LOAN else "تبدیل به اهدا"
        links.append((reverse("admin:fund_agreement_adjust", args=[obj.pk]), adjust_label))
        return format_html_join(" | ", '<a href="{}">{}</a>', links)

    def get_urls(self) -> list[Any]:
        return [
            path(
                "<int:pk>/schedule/",
                self.admin_site.admin_view(self.schedule_view),
                name="fund_agreement_schedule",
            ),
            path("<int:pk>/adjust/", self.admin_site.admin_view(self.adjust_view), name="fund_agreement_adjust"),
            *super().get_urls(),
        ]

    def _form_page(self, request: HttpRequest, obj: Agreement, form: forms.Form, title: str) -> HttpResponse:
        context = {**self.admin_site.each_context(request), "title": title, "form": form, "object": obj}
        return render(request, "fund/form_page.html", context)

    def schedule_view(self, request: HttpRequest, pk: int) -> HttpResponse:
        obj = get_object_or_404(Agreement, pk=pk)
        if not self.has_change_permission(request, obj):
            return HttpResponseRedirect(reverse("admin:index"))
        form = ScheduleForm(request.POST or None)
        if request.method == "POST" and form.is_valid():
            data = form.cleaned_data
            try:
                if data["action"] == "defer":
                    services.defer(obj, data["months"], data["reason"], request.user)
                else:
                    unpaid = services.outstanding(obj)
                    if data["first_due"] is None or unpaid <= 0:
                        raise services.ServiceError("اولین سررسید لازم است و قرارداد باید مانده داشته باشد.")
                    month = JMonth.of(data["first_due"])
                    if data["action"] == "equal":
                        tail = sch.equal(unpaid, data["months"], month)
                    else:
                        tail = sch.balloon(unpaid, month, 0)
                    services.revise_schedule(obj, tail, data["reason"], request.user)
            except (services.ServiceError, sch.ScheduleError) as exc:
                messages.error(request, str(exc))
            else:
                messages.success(request, "جدول جدید ثبت شد.")
                return HttpResponseRedirect(reverse("admin:fund_agreement_change", args=[pk]))
        return self._form_page(request, obj, form, f"تغییر جدول — {obj}")

    def adjust_view(self, request: HttpRequest, pk: int) -> HttpResponse:
        obj = get_object_or_404(Agreement, pk=pk)
        if not self.has_change_permission(request, obj):
            return HttpResponseRedirect(reverse("admin:index"))
        kind = Adjustment.Kind.WRITE_OFF if obj.kind == Agreement.Kind.LOAN else Adjustment.Kind.TO_DONATION
        form = AdjustForm(request.POST or None, initial={"amount": services.outstanding(obj)})
        if request.method == "POST" and form.is_valid():
            data = form.cleaned_data
            if run_service(request, services.adjust, obj, kind, data["amount"], data["reason"], request.user):
                messages.success(request, "ثبت شد.")
                return HttpResponseRedirect(reverse("admin:fund_agreement_change", args=[pk]))
        return self._form_page(request, obj, form, f"{kind.label} — {obj}")


class InstallmentInline(admin.TabularInline):
    from .models import Installment

    model = Installment
    extra = 0
    can_delete = False
    readonly_fields = ("due_date", "amount")

    def has_add_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


class ReadOnlyAdmin(admin.ModelAdmin):
    """History records created by services; never edited by hand."""

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


@admin.register(ScheduleVersion)
class ScheduleVersionAdmin(ReadOnlyAdmin):
    list_display = ("agreement", "number", "is_active", "reason", "created_by", "created_at")
    list_filter = ("is_active",)
    inlines = [InstallmentInline]


@admin.register(Adjustment)
class AdjustmentAdmin(ReadOnlyAdmin):
    list_display = ("agreement", "kind", "amount_t", "reason", "created_by", "created_at")

    @admin.display(description="مبلغ")
    def amount_t(self, obj: Adjustment) -> str:
        return tomans(obj.amount)


# --- transactions -------------------------------------------------------------------------------------


class TransactionForm(forms.ModelForm):
    paste = forms.CharField(
        label="متن پیامک بانک",
        required=False,
        widget=forms.Textarea(attrs={"rows": 5, "dir": "rtl"}),
        help_text="اگر پیامک را اینجا بچسبانید، مبلغ، زمان، مانده و حساب از آن خوانده می‌شود.",
    )
    subordinated = forms.BooleanField(label="آخر از همه پرداخت شود", required=False, help_text="فقط برای قرض‌دهی جدید.")
    confirm_now = forms.BooleanField(label="همین حالا تأیید شود", required=False)

    class Meta:
        model = Transaction
        fields = (
            "paste",
            "kind",
            "fund_account",
            "amount",
            "occurred_at",
            "balance_after",
            "person",
            "agreement",
            "note",
        )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        for name in ("amount", "occurred_at", "fund_account"):
            if name in self.fields:
                self.fields[name].required = False

    def clean(self) -> dict[str, Any]:
        data = super().clean() or {}
        text = data.get("paste", "").strip()
        if text:
            try:
                parsed = parse_sms(text, services.today())
            except SmsParseError as exc:
                raise forms.ValidationError("پیامک شناخته نشد؛ مقادیر را دستی وارد کنید.") from exc
            data["amount"] = data.get("amount") or parsed.amount
            if parsed.occurred_at and not data.get("occurred_at"):
                data["occurred_at"] = timezone.make_aware(parsed.occurred_at)
            if data.get("balance_after") is None:
                data["balance_after"] = parsed.balance
            if not data.get("fund_account"):
                data["fund_account"] = services.match_fund_account(parsed)
            kind = data.get("kind")
            if parsed.direction and kind and ledger.is_incoming(ledger.Kind(kind)) != (parsed.direction == "in"):
                self.add_error("kind", "جهت پیامک با نوع تراکنش نمی‌خواند.")
            key = dedupe_key(parsed.bank, parsed.amount, parsed.occurred_at, parsed.balance)
            if Transaction.objects.filter(dedupe_hash=key).exclude(pk=self.instance.pk).exists():
                raise forms.ValidationError("این پیامک قبلاً ثبت شده است.")
            self.instance.dedupe_hash = key
            self.instance.raw_sms = text
        for name, label in (("amount", "مبلغ"), ("occurred_at", "زمان"), ("fund_account", "حساب صندوق")):
            if not data.get(name):
                self.add_error(name if name in self.fields else None, f"{label} لازم است.")
        kind = data.get("kind")
        needs_person = kind in (K.DEPOSIT, K.DISBURSEMENT, K.INSTALLMENT, K.PAYOUT)
        if needs_person and not data.get("person") and data.get("amount"):
            hints = ""
            if kind == K.INSTALLMENT:
                found = services.suggest_people(data["amount"])
                hints = "، ".join(f"{p} (قرارداد #{a.pk})" for p, a, _ in found[:5])
            self.add_error("person", "شخص را انتخاب کنید." + (f" پیشنهاد: {hints}" if hints else ""))
        if kind == K.DISBURSEMENT and not data.get("agreement"):
            self.add_error("agreement", "قرارداد وام تأییدشده را انتخاب کنید.")
        return data


class AllocationInline(admin.TabularInline):
    from .models import Allocation

    model = Allocation
    extra = 0
    can_delete = False
    readonly_fields = ("agreement", "amount")

    def has_add_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


class ReverseForm(forms.Form):
    reason = forms.CharField(label="دلیل برگشت", max_length=300)


@admin.register(Transaction)
class TransactionAdmin(SimpleHistoryAdmin):
    form = TransactionForm
    list_display = ("id", "when", "kind", "amount_t", "person", "status", "reversal_mark", "fund_account")
    list_filter = ("status", "kind", "fund_account")
    search_fields = ("person__name", "note")
    autocomplete_fields = ("person", "agreement")
    actions = ["confirm_selected", "reverse_selected"]
    inlines = [AllocationInline]

    def get_fields(self, request: HttpRequest, obj: Transaction | None = None) -> Any:
        if obj is not None and obj.is_confirmed:
            return (
                "kind",
                "direction",
                "fund_account",
                "amount_t",
                "when",
                "balance_t",
                "person",
                "agreement",
                "note",
                "status",
                "reverses",
                "created_by",
                "confirmed_by",
                "confirmed",
                "raw_sms",
            )
        fields = list(TransactionForm.Meta.fields)
        fields.insert(fields.index("agreement") + 1, "subordinated")
        return [*fields, "confirm_now"]

    def get_readonly_fields(self, request: HttpRequest, obj: Transaction | None = None) -> Any:
        if obj is not None and obj.is_confirmed:
            return self.get_fields(request, obj)
        return ()

    def has_delete_permission(self, request: HttpRequest, obj: Transaction | None = None) -> bool:
        if obj is not None and obj.is_confirmed:
            return False  # use a reversal instead (CLAUDE.md)
        return super().has_delete_permission(request, obj)

    def save_model(self, request: HttpRequest, obj: Transaction, form: Any, change: bool) -> None:
        data = form.cleaned_data
        for name in ("amount", "occurred_at", "balance_after", "fund_account"):
            setattr(obj, name, data.get(name))
        if not change:
            obj.created_by = request.user
        if obj.kind == K.DEPOSIT and obj.agreement_id is None and obj.person_id is not None:
            obj.agreement = Agreement.objects.create(
                person=obj.person,
                kind=Agreement.Kind.DEPOSIT,
                principal=obj.amount,
                subordinated=data.get("subordinated", False),
                created_by=request.user,
            )
        super().save_model(request, obj, form, change)
        if data.get("confirm_now") and run_service(request, services.confirm, obj, request.user):
            messages.success(request, "تراکنش تأیید شد.")

    @admin.display(description="زمان", ordering="occurred_at")
    def when(self, obj: Transaction) -> str:
        local = timezone.localtime(obj.occurred_at)
        return f"{jdate(local.date())} {local:%H:%M}"

    @admin.display(description="مبلغ (تومان)", ordering="amount")
    def amount_t(self, obj: Transaction) -> str:
        return ("+" if obj.direction == "in" else "−") + tomans(obj.amount)

    @admin.display(description="زمان تأیید")
    def confirmed(self, obj: Transaction) -> str:
        if obj.confirmed_at is None:
            return "—"
        local = timezone.localtime(obj.confirmed_at)
        return f"{jdate(local.date())} {local:%H:%M}"

    @admin.display(description="مانده طبق بانک (تومان)")
    def balance_t(self, obj: Transaction) -> str:
        return tomans(obj.balance_after)

    @admin.display(description="برگشت")
    def reversal_mark(self, obj: Transaction) -> str:
        if obj.is_reversal:
            return f"برگشتِ #{obj.reverses_id}"
        return "برگشت خورده" if hasattr(obj, "reversal") else ""

    @admin.action(description="تأیید تراکنش‌های انتخاب‌شده", permissions=["change"])
    def confirm_selected(self, request: HttpRequest, queryset: QuerySet[Transaction]) -> None:
        count = 0
        for tx in queryset.filter(status=Transaction.Status.DRAFT).order_by("occurred_at"):
            if run_service(request, services.confirm, tx, request.user):
                count += 1
        if count:
            messages.success(request, f"{count} تراکنش تأیید شد.")

    @admin.action(description="برگشت تراکنش‌های انتخاب‌شده…", permissions=["change"])
    def reverse_selected(self, request: HttpRequest, queryset: QuerySet[Transaction]) -> HttpResponse | None:
        form = ReverseForm(request.POST if "reason" in request.POST else None)
        if form.is_valid():
            count = 0
            for tx in queryset:
                if run_service(request, services.reverse, tx, form.cleaned_data["reason"], request.user):
                    count += 1
            if count:
                messages.success(request, f"{count} تراکنش برگشت خورد.")
            return None
        context = {
            **self.admin_site.each_context(request),
            "title": "برگشت تراکنش",
            "form": form,
            "queryset": queryset,
            "action": "reverse_selected",
            "action_checkbox_name": admin.helpers.ACTION_CHECKBOX_NAME,
        }
        return render(request, "fund/reverse_confirm.html", context)


# --- loan requests ------------------------------------------------------------------------------------


@admin.register(LoanRequest)
class LoanRequestAdmin(SimpleHistoryAdmin):
    list_display = ("id", "person", "requested_t", "approved_t", "purpose", "repayment_choice", "status", "created")
    list_filter = ("status", "purpose")
    search_fields = ("person__name",)
    autocomplete_fields = ("person",)
    readonly_fields = ("agreement",)
    actions = ["mark_contacting", "approve_referrer", "approve", "reject"]

    @admin.display(description="درخواستی")
    def requested_t(self, obj: LoanRequest) -> str:
        return tomans(obj.requested_amount)

    @admin.display(description="تأییدشده")
    def approved_t(self, obj: LoanRequest) -> str:
        return tomans(obj.approved_amount)

    @admin.display(description="تاریخ", ordering="created_at")
    def created(self, obj: LoanRequest) -> str:
        return jdate(obj.created_at)

    @admin.action(description="در حال تماس", permissions=["change"])
    def mark_contacting(self, request: HttpRequest, queryset: QuerySet[LoanRequest]) -> None:
        queryset.filter(status=LoanRequest.Status.NEW).update(status=LoanRequest.Status.CONTACTING)

    @admin.action(description="تأیید معرف", permissions=["change"])
    def approve_referrer(self, request: HttpRequest, queryset: QuerySet[LoanRequest]) -> None:
        for r in queryset:
            run_service(request, services.approve_referrer, r, request.user)

    @admin.action(description="تأیید وام (با مبلغ تأییدشده یا درخواستی)", permissions=["change"])
    def approve(self, request: HttpRequest, queryset: QuerySet[LoanRequest]) -> None:
        for r in queryset:
            if run_service(request, services.approve_loan, r, None, request.user):
                messages.success(request, f"وام {r.person} تأیید شد؛ پس از واریز، تراکنش «پرداخت وام» را ثبت کنید.")

    @admin.action(description="رد درخواست", permissions=["change"])
    def reject(self, request: HttpRequest, queryset: QuerySet[LoanRequest]) -> None:
        queryset.exclude(status__in=[LoanRequest.Status.APPROVED, LoanRequest.Status.DISBURSED]).update(
            status=LoanRequest.Status.REJECTED
        )


# --- dashboard ----------------------------------------------------------------------------------------


def payout_rows(month: JMonth) -> list[dict[str, Any]]:
    """Lender installments due in ``month`` (and earlier, unpaid), with the lender's active IBAN."""
    rows = []
    end = month.last_day()
    for a in Agreement.objects.filter(kind=Agreement.Kind.DEPOSIT, status=Agreement.Status.ACTIVE).select_related(
        "person"
    ):
        due = sum(c.remaining for c in sch.cover(a.plan(), services.paid_total(a)) if c.due_date <= end)
        if due > 0:
            account = a.person.bank_accounts.filter(is_active=True).first()
            rows.append(
                {"agreement": a, "amount": due, "account": account, "iban": mask_iban(account.iban) if account else "—"}
            )
    return rows


def dashboard_view(request: HttpRequest) -> HttpResponse:
    if not request.user.has_perm("fund.view_transaction"):
        raise PermissionDenied
    on = services.today()
    buckets = services.fund_buckets()
    cash = services.cash_on_hand()
    forecast = services.monthly_forecast(on, horizon=settings.FUND_FORECAST_HORIZON, margin=settings.FUND_SAFETY_MARGIN)
    month = JMonth.of(on)
    payouts = payout_rows(month)
    alert = services.surplus()
    actions = {sch.Standing.FOLLOW_UP: "پیام یا تماس", sch.Standing.REVIEW: "بررسی دستی"}
    context = {
        **admin.site.each_context(request),
        "title": "وضعیت صندوق",
        "today": jdate(on),
        "buckets": buckets,
        "fmt": {
            name: tomans(getattr(buckets, name))
            for name in ("cash", "equity", "lender_liabilities", "credit", "receivables")
        },
        "ledger_matches": cash == buckets.cash and buckets.balanced,
        "surplus": alert,
        "surplus_available": tomans(alert.available) if alert else "",
        "surplus_rows": [
            {"pk": s.key.pk, "label": str(s.key.person), "amount": tomans(s.balance)}  # type: ignore[attr-defined]
            for s in (alert.fitting if alert else [])
        ],
        "overdue_rows": [
            {
                "pk": r.agreement.pk,
                "person": r.agreement.person,
                "count": len(r.missed),
                "amount": tomans(r.amount),
                "action": actions[r.standing],
            }
            for r in services.overdue(on)
        ],
        "drafts": Transaction.objects.filter(status=Transaction.Status.DRAFT).count(),
        "payout_list": [
            {"pk": r["agreement"].pk, "person": r["agreement"].person, "amount": tomans(r["amount"]), "iban": r["iban"]}
            for r in payouts
        ],
        "payouts_total_fmt": tomans(sum(r["amount"] for r in payouts)),
        "month": str(month),
        "forecast": forecast,
        "free_to_lend": tomans(forecast.free_to_lend),
        "shortfall": "، ".join(str(m) for m in forecast.shortfall_months),
        "forecast_rows": [
            {
                "month": str(r.month),
                "in": tomans(r.expected_in),
                "out": tomans(r.expected_out),
                "cash": tomans(r.projected_cash),
            }
            for r in forecast.rows
        ],
        "overdue_in": tomans(forecast.overdue_in),
        "at_risk": tomans(forecast.at_risk),
    }
    return render(request, "fund/dashboard.html", context)


def payout_export_view(request: HttpRequest) -> HttpResponse:
    from dastbedast.core.bulk_transfer import EXPORTERS, BulkTransferError, PayoutRow

    if not request.user.has_perm("fund.view_bankaccount"):
        raise PermissionDenied
    month = JMonth.of(services.today())
    rows = []
    for r in payout_rows(month):
        if r["account"] is None:
            messages.error(request, f"{r['agreement'].person} حساب بانکی فعال ندارد.")
            return HttpResponseRedirect(reverse("fund_dashboard"))
        rows.append(PayoutRow(r["account"].iban, r["account"].holder_name, r["amount"], f"Dast-be-Dast {month}"))
    try:
        body = EXPORTERS[settings.FUND_BULK_TRANSFER_FORMAT](rows)
    except BulkTransferError as exc:
        messages.error(request, str(exc))
        return HttpResponseRedirect(reverse("fund_dashboard"))
    response = HttpResponse(body, content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="payouts-{month.year}-{month.month:02d}.csv"'
    return response
