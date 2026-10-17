"""Admins' daily digest (PLAN §5.5): due today, overdue, drafts to confirm, alerts. Amounts in tomans."""

from __future__ import annotations

from datetime import date

from django.conf import settings

from dastbedast.core import schedule as sch
from dastbedast.core.jalali import JMonth, format_jalali
from dastbedast.core.money import format_tomans
from dastbedast.fund import services
from dastbedast.fund.models import Agreement, LoanRequest, Transaction


def due_by(kind: str, until: date, since: date | None = None) -> list[tuple[Agreement, int]]:
    rows = []
    for a in Agreement.objects.filter(kind=kind, status=Agreement.Status.ACTIVE).select_related("person"):
        due = sum(
            c.remaining
            for c in sch.cover(a.plan(), services.paid_total(a))
            if c.due_date <= until and (since is None or c.due_date >= since)
        )
        if due > 0:
            rows.append((a, due))
    return rows


def build(on: date | None = None) -> str:
    on = on or services.today()
    lines = [f"خلاصه روز {format_jalali(on)} (تومان)"]

    due_today = due_by(Agreement.Kind.LOAN, on, since=on)
    lines.append(f"\nاقساط سررسید امروز: {len(due_today)}")
    lines += [f"• {a.person} — {format_tomans(amount)}" for a, amount in due_today]

    overdue = services.overdue(on)
    lines.append(f"\nمعوقات: {len(overdue)}")
    for r in overdue:
        label = "بررسی دستی" if r.standing == sch.Standing.REVIEW else "پیگیری"
        lines.append(f"• {r.agreement.person} — {len(r.missed)} قسط، {format_tomans(r.amount)} ({label})")

    payouts = due_by(Agreement.Kind.DEPOSIT, JMonth.of(on).last_day())
    lines.append(f"\nبازپرداخت به وام‌دهندگان تا پایان ماه: {format_tomans(sum(a for _, a in payouts))}")

    drafts = Transaction.objects.filter(status=Transaction.Status.DRAFT).count()
    requests = LoanRequest.objects.filter(status=LoanRequest.Status.NEW).count()
    lines.append(f"\nپیش‌نویس‌های منتظر تأیید: {drafts}")
    lines.append(f"درخواست‌های وام جدید: {requests}")

    alerts = []
    buckets = services.fund_buckets()
    if services.cash_on_hand() != buckets.cash or not buckets.balanced:
        alerts.append("ناهمخوانی در دفتر صندوق؛ تراکنش‌ها را بررسی کنید.")
    if buckets.equity < 0:
        alerts.append("سرمایه صندوق منفی است.")
    forecast = services.monthly_forecast(on, horizon=settings.FUND_FORECAST_HORIZON, margin=settings.FUND_SAFETY_MARGIN)
    if forecast.shortfall_months:
        alerts.append("کمبود نقدینگی در ماه‌های: " + "، ".join(str(m) for m in forecast.shortfall_months))
    surplus = services.surplus()
    if surplus:
        names = "، ".join(str(s.key.person) for s in surplus.fitting)  # type: ignore[attr-defined]
        alerts.append(f"مازاد: امکان بازپرداخت وام‌دهندگان «آخر از همه»: {names}")
    lines.append(f"\nقابل وام‌دهی اکنون: {format_tomans(forecast.free_to_lend)}")
    if alerts:
        lines.append("\nهشدارها:")
        lines += [f"⚠ {a}" for a in alerts]
    return "\n".join(lines)
