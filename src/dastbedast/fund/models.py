"""Data model (PLAN §6). Money fields are integer rials; forms take and show tomans."""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from simple_history.models import HistoricalRecords

from dastbedast.core import ledger, sms
from dastbedast.core.identity import is_valid_iban, normalize_iban, normalize_phone
from dastbedast.core.schedule import Planned

from . import forms as fund_forms


class MoneyField(models.BigIntegerField):
    """Integer rials in the database; entered and displayed as tomans in forms."""

    def formfield(self, **kwargs: Any) -> Any:
        kwargs["form_class"] = fund_forms.TomanAmountField
        kwargs.pop("widget", None)
        return super().formfield(**kwargs)


class JalaliDateField(models.DateField):
    """A Gregorian date in the database, entered and displayed as a Jalali date."""

    def formfield(self, **kwargs: Any) -> Any:
        kwargs["form_class"] = fund_forms.JalaliDateFormField
        kwargs.pop("widget", None)
        return super().formfield(**kwargs)


class JalaliDateTimeField(models.DateTimeField):
    def formfield(self, **kwargs: Any) -> Any:
        kwargs["form_class"] = fund_forms.JalaliDateTimeFormField
        kwargs.pop("widget", None)
        return super().formfield(**kwargs)


def validate_iban(value: str) -> None:
    if not is_valid_iban(value):
        raise ValidationError("شماره شبا معتبر نیست.")


User = settings.AUTH_USER_MODEL


class Person(models.Model):
    name = models.CharField("نام", max_length=200)
    phone = models.CharField("موبایل", max_length=16, unique=True, null=True, blank=True)
    bale_user_id = models.BigIntegerField("شناسه بله", unique=True, null=True, blank=True)
    referred_by = models.ForeignKey(
        "self", verbose_name="معرف", null=True, blank=True, on_delete=models.SET_NULL, related_name="referrals"
    )
    referrer_text = models.CharField("معرف (متن آزاد)", max_length=300, blank=True)
    referrer_approved_at = models.DateTimeField("تأیید معرف", null=True, blank=True, editable=False)
    referrer_approved_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.PROTECT, related_name="+", editable=False
    )
    muted = models.BooleanField("بی‌صدا", default=False, help_text="یادآوری خودکار برای این شخص ارسال نشود.")
    notes = models.TextField("یادداشت", blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    history = HistoricalRecords()

    class Meta:
        verbose_name = "شخص"
        verbose_name_plural = "اشخاص"
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name

    @property
    def referrer_approved(self) -> bool:
        return self.referrer_approved_at is not None

    def clean(self) -> None:
        if self.phone:
            try:
                self.phone = normalize_phone(self.phone)
            except ValueError as exc:
                raise ValidationError({"phone": "شماره موبایل معتبر نیست."}) from exc
        else:
            self.phone = None


class BankAccount(models.Model):
    person = models.ForeignKey(Person, verbose_name="شخص", on_delete=models.PROTECT, related_name="bank_accounts")
    iban = models.CharField("شبا", max_length=26, validators=[validate_iban])
    holder_name = models.CharField("نام صاحب حساب", max_length=200)
    is_active = models.BooleanField("فعال", default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    history = HistoricalRecords()

    class Meta:
        verbose_name = "حساب بانکی"
        verbose_name_plural = "حساب‌های بانکی"
        constraints = [models.UniqueConstraint(fields=["person", "iban"], name="unique_person_iban")]

    def __str__(self) -> str:
        return f"{self.holder_name} ({self.iban[:4]}…{self.iban[-4:]})"

    def clean(self) -> None:
        self.iban = normalize_iban(self.iban)


class FundAccount(models.Model):
    """The fund's own bank accounts. Superuser-only (PLAN §4.7)."""

    BANKS = [(b.value, label) for b, label in zip(sms.Bank, ["پاسارگاد", "ملی", "رسالت", "مهر ایران"], strict=True)]
    BANKS.append(("other", "سایر"))

    bank = models.CharField("بانک", max_length=20, choices=BANKS)
    label = models.CharField("عنوان", max_length=100)
    account_hint = models.CharField(
        "بخش آشکار شماره حساب",
        max_length=40,
        blank=True,
        help_text="همان بخشی از شماره حساب که در پیامک بانک دیده می‌شود؛ برای تشخیص خودکار حساب.",
    )
    is_active = models.BooleanField("فعال", default=True)
    history = HistoricalRecords()

    class Meta:
        verbose_name = "حساب صندوق"
        verbose_name_plural = "حساب‌های صندوق"

    def __str__(self) -> str:
        return self.label


class Agreement(models.Model):
    class Kind(models.TextChoices):
        LOAN = "loan", "وام (قرض‌گیرنده)"
        DEPOSIT = "deposit", "قرض‌دهی (وام‌دهنده)"
        PLEDGE = "pledge", "حامی ماهانه"

    class Status(models.TextChoices):
        PENDING = "pending", "در انتظار پرداخت/دریافت"
        ACTIVE = "active", "جاری"
        SETTLED = "settled", "تسویه‌شده"
        WRITTEN_OFF = "written_off", "سوخت‌شده"
        CANCELLED = "cancelled", "لغوشده"

    # Fields only a superuser may change once the agreement is no longer pending (PLAN §4.7).
    CORE_FIELDS = ("person", "kind", "principal", "subordinated")

    person = models.ForeignKey(Person, verbose_name="شخص", on_delete=models.PROTECT, related_name="agreements")
    kind = models.CharField("نوع", max_length=10, choices=Kind.choices)
    status = models.CharField("وضعیت", max_length=12, choices=Status.choices, default=Status.PENDING)
    principal = MoneyField("مبلغ", help_text="برای حامی ماهانه: مبلغ هر ماه.")
    subordinated = models.BooleanField(
        "آخر از همه پرداخت شود", default=False, help_text="وام‌دهنده‌ای که تاریخ بازپرداخت ندارد (§4.3)."
    )
    opened_on = JalaliDateField("تاریخ شروع", null=True, blank=True)
    pledge_months = models.PositiveIntegerField("تعداد ماه تعهد", null=True, blank=True, help_text="خالی: نامحدود")
    notes = models.TextField("یادداشت", blank=True)
    created_by = models.ForeignKey(User, null=True, on_delete=models.PROTECT, related_name="+", editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    history = HistoricalRecords()

    class Meta:
        verbose_name = "قرارداد"
        verbose_name_plural = "قراردادها"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.get_kind_display()} {self.person} #{self.pk}"

    def clean(self) -> None:
        if self.principal is not None and self.principal <= 0:
            raise ValidationError({"principal": "مبلغ باید مثبت باشد."})
        if self.subordinated and self.kind != self.Kind.DEPOSIT:
            raise ValidationError({"subordinated": "فقط برای وام‌دهنده‌ها معنا دارد."})
        if self.kind == self.Kind.PLEDGE and not self.opened_on:
            raise ValidationError({"opened_on": "ماه شروع تعهد را وارد کنید."})

    @property
    def active_version(self) -> ScheduleVersion | None:
        return self.schedule_versions.filter(is_active=True).first()

    def plan(self) -> list[Planned]:
        version = self.active_version
        return version.plan() if version else []


class ScheduleVersion(models.Model):
    """An immutable snapshot of an agreement's schedule. Changes create a new version (PLAN §3)."""

    agreement = models.ForeignKey(
        Agreement, verbose_name="قرارداد", on_delete=models.PROTECT, related_name="schedule_versions"
    )
    number = models.PositiveIntegerField("نسخه")
    is_active = models.BooleanField("فعال", default=True)
    reason = models.CharField("دلیل", max_length=300)
    created_by = models.ForeignKey(User, verbose_name="ثبت‌کننده", null=True, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField("زمان ثبت", auto_now_add=True)

    class Meta:
        verbose_name = "نسخه جدول اقساط"
        verbose_name_plural = "نسخه‌های جدول اقساط"
        ordering = ["agreement", "-number"]
        constraints = [
            models.UniqueConstraint(fields=["agreement", "number"], name="unique_schedule_version"),
            models.UniqueConstraint(
                fields=["agreement"], condition=models.Q(is_active=True), name="one_active_schedule_version"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.agreement} v{self.number}"

    def plan(self) -> list[Planned]:
        return [Planned(i.due_date, i.amount) for i in self.installments.all()]


class Installment(models.Model):
    version = models.ForeignKey(ScheduleVersion, on_delete=models.CASCADE, related_name="installments")
    due_date = JalaliDateField("سررسید")
    amount = MoneyField("مبلغ")

    class Meta:
        verbose_name = "قسط"
        verbose_name_plural = "اقساط"
        ordering = ["due_date", "pk"]

    def __str__(self) -> str:
        return f"{self.version} {self.due_date}"


class Transaction(models.Model):
    """A movement on one of the fund's bank accounts. Never edited once confirmed (PLAN §1)."""

    class Status(models.TextChoices):
        DRAFT = "draft", "پیش‌نویس"
        CONFIRMED = "confirmed", "تأییدشده"

    class Direction(models.TextChoices):
        IN = "in", "واریز"
        OUT = "out", "برداشت"

    KIND_LABELS = {
        ledger.Kind.OPENING: "مانده اول دوره",
        ledger.Kind.DEPOSIT: "قرض‌دهی (ورود پول وام‌دهنده)",
        ledger.Kind.DONATION: "اهدا",
        ledger.Kind.PLEDGE: "کمک ماهانه حامی",
        ledger.Kind.OWNER_COVER: "جبران زیان توسط مدیر",
        ledger.Kind.INSTALLMENT: "دریافت قسط",
        ledger.Kind.OTHER_IN: "سایر واریزها",
        ledger.Kind.DISBURSEMENT: "پرداخت وام",
        ledger.Kind.PAYOUT: "بازپرداخت به وام‌دهنده / استرداد",
        ledger.Kind.FEE: "کارمزد بانک",
        ledger.Kind.OTHER_OUT: "سایر برداشت‌ها",
    }

    fund_account = models.ForeignKey(FundAccount, verbose_name="حساب صندوق", on_delete=models.PROTECT)
    kind = models.CharField("نوع", max_length=16, choices=[(k.value, v) for k, v in KIND_LABELS.items()])
    direction = models.CharField("جهت", max_length=3, choices=Direction.choices, editable=False)
    amount = MoneyField("مبلغ")
    occurred_at = JalaliDateTimeField("زمان")
    balance_after = MoneyField("مانده پس از تراکنش (طبق بانک)", null=True, blank=True)
    raw_sms = models.TextField("متن پیامک", blank=True)
    person = models.ForeignKey(Person, verbose_name="شخص", null=True, blank=True, on_delete=models.PROTECT)
    agreement = models.ForeignKey(
        Agreement,
        verbose_name="قرارداد",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="funding_transactions",
        help_text="برای قرض‌دهی و پرداخت وام: قراردادی که با این تراکنش شروع می‌شود.",
    )
    status = models.CharField("وضعیت", max_length=10, choices=Status.choices, default=Status.DRAFT, editable=False)
    dedupe_hash = models.CharField(max_length=64, unique=True, null=True, blank=True, editable=False)
    reverses = models.OneToOneField(
        "self",
        verbose_name="برگشت تراکنش",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="reversal",
        editable=False,
    )
    note = models.CharField("توضیح", max_length=300, blank=True)
    created_by = models.ForeignKey(
        User, verbose_name="ثبت‌کننده", null=True, on_delete=models.PROTECT, related_name="+", editable=False
    )
    created_at = models.DateTimeField(auto_now_add=True)
    confirmed_by = models.ForeignKey(
        User,
        verbose_name="تأییدکننده",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
        editable=False,
    )
    confirmed_at = models.DateTimeField(null=True, blank=True, editable=False)
    history = HistoricalRecords()

    class Meta:
        verbose_name = "تراکنش"
        verbose_name_plural = "تراکنش‌ها"
        ordering = ["-occurred_at", "-pk"]
        permissions = [("record_opening_balance", "Can record opening balances")]

    def __str__(self) -> str:
        return f"{self.get_kind_display()} {self.amount:,} #{self.pk}"

    def save(self, *args: Any, **kwargs: Any) -> None:
        if self.kind:
            self.direction = self.natural_direction()
        super().save(*args, **kwargs)

    @property
    def ledger_kind(self) -> ledger.Kind:
        return ledger.Kind(self.kind)

    @property
    def is_confirmed(self) -> bool:
        return self.status == self.Status.CONFIRMED

    @property
    def is_reversal(self) -> bool:
        return self.reverses_id is not None

    def natural_direction(self) -> str:
        incoming = ledger.is_incoming(self.ledger_kind)
        if self.is_reversal:
            incoming = not incoming
        return self.Direction.IN if incoming else self.Direction.OUT

    def signed_amount(self) -> int:
        return self.amount if self.direction == self.Direction.IN else -self.amount


class Allocation(models.Model):
    """Part of an installment or payout transaction applied to an agreement's schedule.

    Reversal records carry negative amounts.
    """

    transaction = models.ForeignKey(Transaction, on_delete=models.PROTECT, related_name="allocations")
    agreement = models.ForeignKey(
        Agreement, verbose_name="قرارداد", on_delete=models.PROTECT, related_name="allocations"
    )
    amount = MoneyField("مبلغ")

    class Meta:
        verbose_name = "تخصیص"
        verbose_name_plural = "تخصیص‌ها"

    def __str__(self) -> str:
        return f"{self.transaction_id} → {self.agreement_id}: {self.amount:,}"


class Adjustment(models.Model):
    class Kind(models.TextChoices):
        WRITE_OFF = ledger.AdjustmentKind.WRITE_OFF.value, "سوخت (زیان)"
        TO_DONATION = ledger.AdjustmentKind.TO_DONATION.value, "تبدیل به اهدا"

    agreement = models.ForeignKey(
        Agreement, verbose_name="قرارداد", on_delete=models.PROTECT, related_name="adjustments"
    )
    kind = models.CharField("نوع", max_length=12, choices=Kind.choices)
    amount = MoneyField("مبلغ")
    reason = models.CharField("دلیل", max_length=300)
    created_by = models.ForeignKey(User, verbose_name="ثبت‌کننده", null=True, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField("زمان ثبت", auto_now_add=True)

    class Meta:
        verbose_name = "اصلاحیه"
        verbose_name_plural = "اصلاحیه‌ها"

    def __str__(self) -> str:
        return f"{self.get_kind_display()} {self.amount:,}"


class LoanRequest(models.Model):
    class Purpose(models.TextChoices):
        MEDICAL = "medical", "درمان"
        RENT = "rent", "اجاره/مسکن"
        EDUCATION = "education", "تحصیل"
        LIVING = "living", "هزینه زندگی"
        OTHER = "other", "سایر"

    class Repayment(models.TextChoices):
        DEFAULT = "default", "۶ قسط ماهانه"
        CONTACT = "contact", "تماس برای توافق"

    class Status(models.TextChoices):
        NEW = "new", "جدید"
        CONTACTING = "contacting", "در حال تماس"
        REFERRER_APPROVED = "referrer_approved", "معرف تأیید شد"
        APPROVED = "approved", "تأییدشده"
        DISBURSED = "disbursed", "پرداخت‌شده"
        REJECTED = "rejected", "ردشده"
        WITHDRAWN = "withdrawn", "انصراف"

    person = models.ForeignKey(Person, verbose_name="متقاضی", on_delete=models.PROTECT, related_name="loan_requests")
    requested_amount = MoneyField("مبلغ درخواستی")
    purpose = models.CharField("مصرف", max_length=10, choices=Purpose.choices)
    description = models.TextField("توضیح", blank=True)
    repayment_choice = models.CharField(
        "بازپرداخت", max_length=10, choices=Repayment.choices, default=Repayment.DEFAULT
    )
    bank_account = models.ForeignKey(
        BankAccount, verbose_name="حساب واریز", null=True, blank=True, on_delete=models.PROTECT
    )
    status = models.CharField("وضعیت", max_length=20, choices=Status.choices, default=Status.NEW)
    approved_amount = MoneyField("مبلغ تأییدشده", null=True, blank=True)
    agreement = models.OneToOneField(
        Agreement, null=True, blank=True, on_delete=models.PROTECT, related_name="loan_request", editable=False
    )
    notes = models.TextField("یادداشت", blank=True)
    # The last status the applicant was told about in the bot; the bot sends a message when it differs.
    notified_status = models.CharField(max_length=20, blank=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    history = HistoricalRecords()

    class Meta:
        verbose_name = "درخواست وام"
        verbose_name_plural = "درخواست‌های وام"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.person} — {self.requested_amount:,}"
