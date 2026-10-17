"""Borrower application conversation for the Bale bot (PLAN §5.1). Pure Python, no Bale or Django.

The conversation is a state machine: ``handle(step, data, incoming)`` returns the next step, the updated
data (JSON-serialisable) and the replies to send. When the applicant submits, ``Outcome.submitted`` holds
the finished application. Amounts are entered in tomans and stored in rials.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .identity import is_valid_iban, normalize_iban, normalize_phone
from .money import RIALS_PER_TOMAN, format_tomans, parse_amount, to_ascii_digits

# Values match fund.LoanRequest.Purpose / Repayment.
PURPOSES = {"medical": "درمان", "rent": "اجاره/مسکن", "education": "تحصیل", "living": "هزینه زندگی", "other": "سایر"}
REPAYMENTS = {"default": "۶ قسط ماهانه", "contact": "با من تماس بگیرید تا توافق کنیم"}

CANCEL = "انصراف"
SKIP = "ندارم"
SAME_NAME = "به نام خودم است"
SUBMIT = "ارسال درخواست"
RESTART = "از اول وارد می‌کنم"
SHARE_PHONE = "ارسال شماره موبایل من"

MAX_TOMANS = 10_000_000_000  # sanity bound on a typed amount


@dataclass(frozen=True)
class Reply:
    text: str
    options: tuple[tuple[str, ...], ...] = ()  # reply-keyboard rows
    ask_contact: str | None = None  # label of a request_contact button
    remove_keyboard: bool = False


@dataclass(frozen=True)
class Incoming:
    text: str | None = None
    phone: str | None = None  # from a shared contact
    own_contact: bool = False  # the contact belongs to the sender (Bale-verified)


@dataclass(frozen=True)
class Application:
    name: str
    phone: str
    referrer: str
    referrer_relation: str
    amount: int  # rials
    purpose: str
    description: str
    iban: str
    holder_name: str
    repayment: str
    notes: str

    @property
    def referrer_text(self) -> str:
        return f"{self.referrer} — {self.referrer_relation}"


@dataclass
class Outcome:
    step: str
    data: dict[str, Any]
    replies: list[Reply] = field(default_factory=list)
    submitted: Application | None = None
    cancelled: bool = False


def _cancel_row() -> tuple[str, ...]:
    return (CANCEL,)


def _prompt(step: str, data: dict[str, Any]) -> Reply:
    if step == "name":
        return Reply("نام و نام خانوادگی خود را بنویسید.", (_cancel_row(),))
    if step == "phone":
        return Reply(
            "برای تأیید شماره موبایل، دکمه زیر را بزنید تا شماره‌تان از طریق بله فرستاده شود.",
            (_cancel_row(),),
            ask_contact=SHARE_PHONE,
        )
    if step == "referrer":
        return Reply("معرف شما کیست؟ نام کامل او را بنویسید.", (_cancel_row(),))
    if step == "relation":
        return Reply("معرف را از کجا می‌شناسید؟ (مثلاً همکار، همسایه، خویشاوند)", (_cancel_row(),))
    if step == "amount":
        return Reply("چه مبلغی لازم دارید؟ به تومان بنویسید (مثلاً ۲۰,۰۰۰,۰۰۰ یا ۲۰ میلیون).", (_cancel_row(),))
    if step == "purpose":
        labels = list(PURPOSES.values())
        return Reply("این وام برای چه مصرفی است؟", (tuple(labels[:3]), tuple(labels[3:]), _cancel_row()))
    if step == "description":
        return Reply("در یکی دو جمله توضیح دهید وام برای چه کاری است.", (_cancel_row(),))
    if step == "iban":
        return Reply("شماره شبای حسابی که وام به آن واریز شود را بنویسید (IR و ۲۴ رقم).", (_cancel_row(),))
    if step == "holder":
        return Reply("این حساب به نام چه کسی است؟", ((SAME_NAME,), _cancel_row()))
    if step == "repayment":
        return Reply("بازپرداخت را چطور می‌خواهید؟", (tuple(REPAYMENTS.values()), _cancel_row()))
    if step == "notes":
        return Reply("اگر نکته دیگری هست بنویسید؛ وگرنه «ندارم» را بزنید.", ((SKIP,), _cancel_row()))
    if step == "review":
        return Reply(summary(data) + "\n\nاگر درست است «ارسال درخواست» را بزنید.", ((SUBMIT,), (RESTART, CANCEL)))
    raise ValueError(f"unknown step: {step}")


def summary(data: dict[str, Any]) -> str:
    lines = [
        "خلاصه درخواست شما:",
        f"نام: {data['name']}",
        f"موبایل: {data['phone']}",
        f"معرف: {data['referrer']} ({data['relation']})",
        f"مبلغ: {format_tomans(data['amount'])} تومان",
        f"مصرف: {PURPOSES[data['purpose']]} — {data['description']}",
        f"شبا: {data['iban']}",
        f"صاحب حساب: {data['holder']}",
        f"بازپرداخت: {REPAYMENTS[data['repayment']]}",
    ]
    if data.get("notes"):
        lines.append(f"توضیحات: {data['notes']}")
    return "\n".join(lines)


ORDER = ["name", "phone", "referrer", "relation", "amount", "purpose", "description", "iban", "holder"]
ORDER += ["repayment", "notes", "review"]


def begin() -> Outcome:
    return Outcome(
        "name",
        {},
        [Reply("سلام. با چند سؤال کوتاه درخواست وام قرض‌الحسنه شما ثبت می‌شود."), _prompt("name", {})],
    )


def parse_tomans(text: str) -> int:
    """Typed toman amount → rials. Accepts Persian digits, separators, and «میلیون» / «هزار»."""
    clean = to_ascii_digits(text).replace("تومان", "").strip()
    scale = 1
    for word, factor in (("میلیون", 1_000_000), ("هزار", 1_000)):
        if word in clean:
            clean, scale = clean.replace(word, "").strip(), factor
    if re.fullmatch(r"\d+([.٫/]\d+)?", clean) and scale > 1:
        whole = float(clean.replace("٫", ".").replace("/", "."))
        tomans = round(whole * scale)
    else:
        tomans = parse_amount(clean) * scale
    if not 0 < tomans <= MAX_TOMANS:
        raise ValueError("amount out of range")
    return tomans * RIALS_PER_TOMAN


def _text(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if not value or value.startswith("/") or len(value) > limit:
        return None
    return value


def _accept(step: str, data: dict[str, Any], msg: Incoming) -> str | None:
    """Store the answer for ``step`` in ``data``; return an error message if it is not acceptable."""
    if step == "phone":
        if msg.phone is None:
            return "لطفاً با دکمه «ارسال شماره موبایل من» شماره را بفرستید؛ تایپ شماره پذیرفته نمی‌شود."
        if not msg.own_contact:
            return "فقط شماره موبایل خودتان را با دکمه زیر بفرستید."
        try:
            data["phone"] = normalize_phone(msg.phone)
        except ValueError:
            return "این شماره موبایل ایران نیست؛ با پشتیبانی تماس بگیرید."
        return None
    if msg.text is None:
        return "لطفاً پاسخ را به صورت متن بنویسید."
    text = msg.text
    if step == "name":
        value = _text(text, 100)
        if value is None or len(value) < 3:
            return "نام و نام خانوادگی را کامل بنویسید."
        data["name"] = value
    elif step in ("referrer", "relation"):
        value = _text(text, 150)
        if value is None or len(value) < 2:
            return "لطفاً کامل‌تر بنویسید."
        data[step] = value
    elif step == "amount":
        try:
            data["amount"] = parse_tomans(text)
        except ValueError:
            return "مبلغ را فقط با عدد و به تومان بنویسید، مثلاً ۲۰,۰۰۰,۰۰۰."
    elif step == "purpose":
        key = next((k for k, v in PURPOSES.items() if v == text.strip()), None)
        if key is None:
            return "یکی از گزینه‌ها را انتخاب کنید."
        data["purpose"] = key
    elif step == "description":
        value = _text(text, 500)
        if value is None:
            return "توضیح کوتاهی بنویسید (حداکثر ۵۰۰ حرف)."
        data["description"] = value
    elif step == "iban":
        iban = normalize_iban(text)
        if not is_valid_iban(iban):
            return "شماره شبا درست نیست. IR و ۲۴ رقم را بدون اشتباه بنویسید."
        data["iban"] = iban
    elif step == "holder":
        value = data["name"] if text.strip() == SAME_NAME else _text(text, 100)
        if value is None or len(value) < 3:
            return "نام کامل صاحب حساب را بنویسید."
        data["holder"] = value
    elif step == "repayment":
        key = next((k for k, v in REPAYMENTS.items() if v == text.strip()), None)
        if key is None:
            return "یکی از گزینه‌ها را انتخاب کنید."
        data["repayment"] = key
    elif step == "notes":
        if text.strip() == SKIP:
            data["notes"] = ""
        else:
            value = _text(text, 1000)
            if value is None:
                return "توضیحات حداکثر ۱۰۰۰ حرف باشد، یا «ندارم» را بزنید."
            data["notes"] = value
    return None


def _application(data: dict[str, Any]) -> Application:
    return Application(
        name=data["name"],
        phone=data["phone"],
        referrer=data["referrer"],
        referrer_relation=data["relation"],
        amount=data["amount"],
        purpose=data["purpose"],
        description=data["description"],
        iban=data["iban"],
        holder_name=data["holder"],
        repayment=data["repayment"],
        notes=data.get("notes", ""),
    )


def handle(step: str, data: dict[str, Any], msg: Incoming) -> Outcome:
    data = dict(data)
    if msg.text is not None and msg.text.strip() in (CANCEL, "/cancel"):
        return Outcome(
            "", {}, [Reply("درخواست لغو شد. هر وقت خواستید /start را بزنید.", remove_keyboard=True)], cancelled=True
        )
    if step == "review":
        choice = (msg.text or "").strip()
        if choice == SUBMIT:
            return Outcome("", {}, submitted=_application(data))
        if choice == RESTART:
            return Outcome("name", {}, [_prompt("name", {})])
        return Outcome(step, data, [_prompt(step, data)])
    error = _accept(step, data, msg)
    if error:
        return Outcome(step, data, [Reply(error), _prompt(step, data)])
    following = ORDER[ORDER.index(step) + 1]
    return Outcome(following, data, [_prompt(following, data)])
