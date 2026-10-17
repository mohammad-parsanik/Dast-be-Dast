# Dast-be-Dast (دست به دست)

Back office for an interest-free (قرض‌الحسنه) lending circle among friends: lenders, donors and monthly
sponsors fund a single pool; people known through a referrer borrow from it for necessities and repay in
installments. The system tracks agreements and schedules on both sides, parses bank SMS notifications to
record money in and out, forecasts monthly cash, and reminds people through a [Bale](https://bale.ai) bot.

**Status:** domain core, data model, admin panel, roles, SMS paste, dashboard, forecast and the Bale bot
(applications, status updates, admin entry flows, digest) are built. Reminders and the bank-specific
bulk-transfer format come next.
See [PLAN.md](PLAN.md) for the agreed design and §8 for phase status.

Stack: Django 5.2 + PostgreSQL (SQLite locally), Jalali dates, Bale bot API + Safir, Docker Compose.

## Development

```sh
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
export DJANGO_DEBUG=1            # SQLite, no 2FA
python manage.py migrate
python manage.py setup_roles     # creates the "operator" group
python manage.py createsuperuser
python manage.py runserver       # http://localhost:8000/admin/
python manage.py run_bot         # Bale bot, long polling (needs BALE_BOT_TOKEN in the environment)

pytest && ruff check . && ruff format --check . && mypy
```

## First run in production

1. `cp .env.example .env`, fill it in, then `docker compose up -d --build`.
2. `docker compose exec app python manage.py createsuperuser`, then
   `docker compose exec app python manage.py otp_setup <username>` and scan the QR code (admin login
   needs the 6-digit code).
3. In the admin (as superuser): add each **fund account** (حساب صندوق), then record its opening balance
   as a transaction of kind «مانده اول دوره» and confirm it.
4. Create the other admins as staff users in the **operator** group, and run `otp_setup` for each.
5. Bot: each admin sends `/whoami` to the bot; the superuser adds them under «مدیران در بازو» with that id.
   Schedule `docker compose exec bot python manage.py send_digest` daily (cron) for the admins' digest.

## Day-to-day

- **Money in/out:** Transactions → Add. Paste the bank SMS; amount, time, balance and account are filled in.
  Pick the kind and the person, tick «همین حالا تأیید شود» or confirm drafts later from the list.
  A new lender deposit creates its agreement and 6-payment schedule automatically.
- **Loans:** Loan requests → approve the referrer → approve → after paying the borrower, record a
  «پرداخت وام» transaction for the agreement; the schedule starts from that date.
- **In the bot (admins):** «دریافت قسط», «وام / اهدای جدید», «پرداخت انجام شد»: paste the bank SMS, pick
  the kind and person, then «ثبت و تأیید» or «فقط پیش‌نویس». «درخواست‌ها» lists the loan queue with
  approve/reject buttons (approve and reject ask twice); «خلاصه روز» shows the digest.
- **Mistakes:** confirmed transactions are never edited. Use the «برگشت» action, then record the right one.
- **Schedules:** on an agreement page use «تغییر جدول / تعویق», «سوخت» or «تبدیل به اهدا».
- **Dashboard:** «وضعیت صندوق» on the admin home: ledger check, overdue list, this month's payouts and
  the bulk-transfer file, the 12-month forecast, and the "pay me last" surplus alert.

> This repository is public. It must never contain real names, phone numbers, IBANs, bank messages or
> balances — use anonymised fixtures only.
