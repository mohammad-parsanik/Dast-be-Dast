# Dast-be-Dast (دست به دست)

Back office for an interest-free (قرض‌الحسنه) lending circle among friends: lenders, donors and monthly
sponsors fund a single pool; people known through a referrer borrow from it for necessities and repay in
installments. The system tracks agreements and schedules on both sides, parses bank SMS notifications to
record money in and out, forecasts monthly cash, and reminds people through a [Bale](https://bale.ai) bot.

**Status:** planning. See [PLAN.md](PLAN.md) for the agreed design.

Stack (planned): Django + PostgreSQL, Jalali dates, Bale bot API + Safir, Docker Compose.

> This repository is public. It must never contain real names, phone numbers, IBANs, bank messages or
> balances — use anonymised fixtures only.
