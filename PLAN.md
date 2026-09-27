# Dast-be-Dast — Plan of Record

A back office for an informal, interest-free (قرض‌الحسنه) lending circle: friends (and friends of friends)
put money in, it is lent to people known through a referrer for necessities, and repayments flow back
out to lenders or into new loans. This document is the agreed design; keep it in sync when scope changes.

---

## 1. Principles

- **One fund, two pools.** Lenders and borrowers are never matched to each other. Money goes into one
  fund; obligations are tracked on both sides and the fund is kept solvent.
- **Human confirms every money movement.** The system suggests, parses, and forecasts; an admin confirms
  each recorded transaction and each payout. Nothing moves money on its own.
- **Real bank balance is the source of truth.** The ledger must reconcile to the balance reported by the
  bank on every SMS.
- **Everything is editable, nothing is overwritten.** Schedules are versioned, confirmed records are
  history-tracked.

## 2. Accounting model

| Bucket | Meaning | Grows with | Shrinks with |
|---|---|---|---|
| **Liabilities** | Lender principal we still owe | New lender deposit | Payout to lender, conversion to donation |
| **Receivables** | Borrower principal still owed to us | Loan disbursement | Borrower repayment, write-off |
| **Equity** | Money nobody gets back | Donations, sponsor contributions, lender→donation conversions, owner cover | Write-offs, bank fees |

**Invariant:** `cash = equity + liabilities − receivables`, checked against the bank balance in every
parsed SMS. A mismatch means a missing or wrong transaction.

**Fund health = equity.** Negative equity means lenders' money is at risk.

## 3. Core concepts

Every flow maps onto **Agreement → Schedule → Installments**, with **Transactions** allocated to installments.

### Agreement kinds

| Kind | Direction of schedule | Default schedule | Notes |
|---|---|---|---|
| `loan` (borrower) | we receive | 6 equal, first due end of month *disbursement+1* | Custom schedules negotiated by phone |
| `deposit` (lender) | we pay | 6 equal, first due end of month *deposit+2* | May be `subordinated` (see §4.3) |
| `donation` | — | none | One-off |
| `pledge` (monthly sponsor) | we receive | monthly, open-ended or N months | Counted as donation (equity). If a sponsor later wants it back, each contribution becomes a `deposit` with a single-installment schedule |

### Schedule templates

- `equal(n, start_offset_months)`: the two defaults above
- `balloon(skip_months)`: nothing for N months, then the whole amount (e.g. "nothing for 3 months, then all")
- `single(date)`: one payment at a fixed, possibly far-off, date (used for subordinated lenders who agree on a date)
- `custom`: free list of `(due_date, amount)`
- `subordinated`: no dates; paid when the surplus rule fires (§4.3)

### Dates and amounts

- **Calendar:** Jalali. **Due date = last day of the Jalali month**, regardless of disbursement day.
- **Offset counts from the month *after* the disbursement or deposit month.** Example: a loan disbursed on
  1405/07/10 has its first installment due on 1405/08/30. A deposit made on 1405/07/10 has its first
  payout due on 1405/09/30. *(Configurable per agreement.)*
- **Money is stored as integer rials.** The UI shows tomans. Installments are rounded to 100,000 tomans,
  with the remainder on the last one.

### Schedule versions

Renegotiating a schedule (borrower asks for more time, lender gets deferred) creates a **new schedule
version** that replaces only the *unpaid* installments. Earlier versions stay for history, together with
the reason and the admin who made the change.

### Allocation

- A transaction is allocated to one or more installments. This handles partial payments, several
  installments paid at once, and overpayments.
- **A borrower with several loans:** incoming money is applied to the person's **oldest unpaid
  installment across all their loans** (FIFO). An admin can override the allocation.
- Overpayment beyond everything due is held as credit on the person, not silently turned into a donation.

## 4. Policies (decided)

### 4.1 Liquidity shortfall
The forecast (§6) flags any month where projected cash is below obligations. The **deferral tool** shows
that month's lender payouts. The admin picks a few lenders to push to a later month and sees the updated
projection immediately. Only the selected lenders need to be contacted, and each deferral is a schedule
version with its reason.

- Lenders are told when they join that the fund carries risk. The onboarding message states this.
- If the owner personally covers a loss, it is recorded as an `owner_cover` transaction, which counts as equity.

### 4.2 Sponsors (monthly contributors)
Their contributions are donations (equity). Pledges are tracked as expected against received, and they
get reminders like borrowers do. They can be converted to repayable per §3.

### 4.3 Subordinated ("pay me last") lenders
They have no dates. A **surplus alert** fires when:

```
cash + collectible receivables − all remaining regular (non-subordinated) lender payouts  ≥  subordinated balance
```

- *collectible receivables* excludes installments of loans under review (§4.4).
- The alert lists which subordinated lenders fit. The admin then chooses one of:
  - (a) repay them (a schedule is generated),
  - (b) keep lending and snooze the alert,
  - (c) convert them to a `single(date)` schedule at an agreed far-off date.

### 4.4 Overdue and default
| State | Trigger | System action |
|---|---|---|
| due | installment date reached | reminder to borrower |
| **follow-up** | 1 missed installment | admin task: message or call |
| **review** | ≥ 2 missed installments | flagged on the dashboard; loan excluded from "collectible" in forecasts |
| **written off** | manual admin decision with a reason (superuser only) | remaining principal moves from receivables to a loss against equity |

### 4.5 Referrers
The referrer is used only to decide who gets a loan. The borrower types the referrer's name as free text.
An admin links it to a known person and **approves the referrer** before the loan can be approved.
The referrer takes on no obligation afterwards and receives no messages about repayment.

### 4.6 Lender → donation
A lender can forgive part or all of what is left. This is recorded as a conversion: the amount moves from
liabilities to equity, and the remaining schedule is adjusted.

### 4.7 Roles (≈3 users)
| Role | Can | Cannot |
|---|---|---|
| **superuser** (owner) | everything | — |
| **operator** | add people and requests, paste and record transactions (as drafts), send reminders, view reports | confirm payouts, approve loans or referrers, write off, edit or delete confirmed transactions, change schedules of active agreements |

Every change is kept in an audit history (who, when, before and after).

## 5. Flows

### 5.1 Borrower application (Bale bot)
The bot collects:
1. Full name
2. Phone number, sent through Bale's `request_contact` button, so it is verified by Bale rather than typed
3. Referrer: free text, plus how the borrower knows them
4. Requested amount (the admin may approve a different amount)
5. Purpose: a category (medical, rent, education, living costs, other) plus a short description
6. IBAN (شبا), with an `IR` + 24-digit checksum check, and the **account holder's name** (it may not be the
   applicant's own account)
7. Repayment choice: "6 monthly installments" (default) or "please contact me to agree on terms"
8. Optional notes

After submitting, the application moves through these statuses: `new → contacting → referrer approved →
approved (final amount + schedule) → disbursed`, or it can end as `rejected` / `withdrawn`.

- The approval queue is ordered manually. There is no automatic priority.
- The applicant gets a bot message at each status change.
- When the loan is disbursed, the admin records the outgoing transaction and the schedule starts.
- A second loan is allowed while an earlier one is still open, with FIFO allocation (§3).

### 5.2 Recording incoming money
There are three entry points, each available both in the admin web panel and in the admin section of the bot.

- **«دریافت قسط» (installment received).** Paste the bank SMS; the parser fills in amount, date, balance
  and account. The system **suggests matching people** from due or overdue installments with that amount;
  the admin picks one, and the money is allocated per §3.
- **«وام / اهدای جدید» (new deposit / donation / pledge payment).** Paste the SMS. Then pick an existing
  person or create one (name, phone, IBAN), and pick the kind: `deposit` (choose a schedule template),
  `donation`, or `pledge` payment.
- **«پرداخت انجام شد» (outgoing).** Paste the debit SMS or tick rows of the monthly payout list. This covers
  loan disbursements and payouts to lenders.

Common to all three:
- Every recorded transaction starts as a **draft**. It counts once it is **confirmed** (operator drafts,
  superuser confirms; a superuser can do both in one step).
- Duplicate protection: a hash of bank, amount, timestamp and balance.

### 5.3 Bank SMS parser
Formats seen so far (samples in [`docs/bank-sms-samples.md`](docs/bank-sms-samples.md)):

| Bank | Sign | Date | Notes |
|---|---|---|---|
| Pasargad | `+20,000,000` | `07/04_17:41` (MM/DD, no year) | account `201.8000.…` |
| Melli | `پايا:116,000,000+` | `0701-19:18` (MMDD, no year) | channel (پایا) in text |
| Resalat | `+45,670,068` | `07/14_09:18` | |
| Mehr Iran | none — label `پرداخت گروهی` | `1405/6/23-10:07` (full) | direction inferred from label, or asked |

- The parser extracts: bank, direction, amount (in rials), resulting balance, timestamp (the year is taken
  from the current date when missing), masked account, and channel.
- Persian and Arabic digits and letters (ي/ی, ك/ک) are normalised before parsing.
- If any field is uncertain, the parser asks the admin instead of guessing.
- The **balance in each SMS is compared against the ledger** (§2).
- Each bank's parser is a small function with its own fixture tests. New banks are added as their SMS format appears.

### 5.4 Monthly report and forecast
For each Jalali month, from the current month through the next 12:

- **Expected in:** borrower installments plus sponsor pledges
- **Expected out:** lender payouts plus approved loans not yet disbursed
- **Actual in and out**, and overdue amounts (per person, and how many months overdue)
- **Projected cash** at the end of each month
- **Free to lend now:** the lowest projected cash over the forecast horizon, minus a safety margin set by
  the admin. Lending more than this creates a shortfall somewhere in the horizon.
- Alerts: shortfall (§4.1), surplus (§4.3), follow-up and review (§4.4), and a ledger/bank mismatch (§2)
- **Payout list** for the month: who, how much, which IBAN. It can be exported as a bulk-transfer file (§7).

### 5.5 Reminders
| Who | When | Channel |
|---|---|---|
| Borrower | 3 days before due; on the due date; +3 days → operator follow-up task | Bale bot (they started it when applying) |
| Sponsor | at the start of each month | Safir (by phone number) or the bot |
| Lender | when a payout is sent, and when a payout is deferred (only if an admin chooses to notify) | Safir or the bot |
| Admins | daily digest: due today, overdue, drafts to confirm, alerts | admin Bale chat |

- Message templates are editable.
- Reminders are logged so nobody is messaged twice.
- There is a mute switch per person, for cases handled by phone.

## 6. Architecture

- **Django 5 + PostgreSQL** (SQLite for local development).
  - The Django admin provides the back-office screens, auth, and permission groups.
  - Jalali dates via `jdatetime` / `django-jalali`.
  - Audit history via `django-simple-history`.
  - Custom views on top of the admin: dashboard and forecast, SMS paste forms, approval queue, deferral tool.
- **Domain core as pure Python** (`src/dastbedast/core/`): schedule generation, allocation, forecast,
  alerts, SMS parsers. Fully unit-tested, with no Django imports.
- **Bale bot (Bale's own bot API, not a Telegram library)**, written directly against
  [docs.bale.ai](https://docs.bale.ai):
  - A thin HTTP client over `https://tapi.bale.ai/bot<token>/METHOD`. It follows the client pattern from the
    owner's `Bale-Message-Sender` project (throttle, retry, no retries on 4xx).
  - It receives updates by long polling with `getUpdates`. This needs no public HTTPS endpoint; Bale
    webhooks only accept ports 443 and 88.
  - It runs as a Django management command (`manage.py run_bot`).
  - It uses `request_contact` for phone verification, inline keyboards for menus, and callback data of 64
    bytes or less.
  - Admin chats are whitelisted by Bale user id.
- **Safir** (the Bale bulk-messaging API, as used in `Bale-Message-Sender`) sends reminders by phone number
  to people who never started the bot.
- **Scheduler:** a management command run by cron (or a small loop container) for reminders, alerts, and the daily digest.
- **Deploy:** Docker Compose (app, bot, scheduler, postgres), hosted inside Iran so the Bale APIs are
  reliably reachable (e.g. ArvanCloud, as with previous projects).
- **Security:**
  - IBANs and phone numbers are PII. The public repo holds **no real data**, and fixtures are anonymised.
  - Secrets live in `.env`.
  - Admin logins require 2FA (`django-otp`); backups are encrypted.
  - Personal data is never logged.

### Data model (first cut)
```
Person(name, phone, bale_user_id, referred_by→Person?, referrer_text, referrer_approved, muted, notes)
BankAccount(person, iban, holder_name)
FundAccount(bank, masked_number, label)                # our accounts
Agreement(person, kind, principal, status, subordinated, opened_on, notes)
ScheduleVersion(agreement, version, reason, created_by, active)
Installment(schedule_version, seq, due_date, amount, direction)   # status derived from allocations
Transaction(fund_account, direction, amount, occurred_at, balance_after, raw_sms,
            kind[installment_in|deposit|donation|pledge|disbursement|payout|fee|owner_cover|other],
            person?, status[draft|confirmed], dedupe_hash)
Allocation(transaction, installment, amount)
Adjustment(kind[write_off|to_donation], agreement, amount, reason, by)
LoanRequest(person, requested_amount, purpose, description, repayment_choice, status,
            approved_amount, agreement?)
Reminder(person, installment?, channel, sent_at, template, result)
```

## 7. Automating money movement — findings

| Stage | What | Verdict |
|---|---|---|
| **A. Assisted manual payouts** | The system generates the month's payout list and a **bank bulk-transfer file** (پرداخت گروهی / دستور پرداخت گروهی پایا). The admin uploads it to internet banking and approves it there. | **Build now.** The Mehr Iran SMS shows the account already supports «پرداخت گروهی». Melli offers «دستور پرداخت گروهی پایا», and Pasargad offers Excel bulk transfers. We need the exact file template of the bank used for payouts. |
| **B. Automatic incoming payments** | A reminder carries a Bale **wallet invoice** (`sendInvoice`). The payment lands in the bot's wallet and a `successful_payment` update confirms the installment with no SMS pasting. `inquireTransaction` re-checks status. | **Promising; verify first.** Bale removed card-to-card invoices, so wallet payments are now the only official method. To confirm with Bale support: can a *personal* wallet receive them; what are the per-transaction and daily limits (installments are often 10–20M tomans); what are the fees; how fast are withdrawals to the fund's bank account. |
| **C. Automatic payouts via API** | Payout APIs: Jibit (transfer/settlement), Vandar (Paya payout, up to 1,000 rows per file), Zibal, Finnotech, Podium (group Paya/Satna). | **Not feasible for now.** These are built for registered businesses: merchant onboarding, KYC or a contract, and payouts funded from a provider wallet rather than the fund's own bank account. Registering an informal lending circle as a business adds regulatory exposure. Revisit only if the fund formalises. Regardless of the stage, a human keeps the final approval of every payout. |

Opening accounts at a new bank is therefore **not needed** for automation. Pick the payout bank based on
which one has the most convenient bulk-transfer upload.

## 8. Build phases

1. **Scaffold:** Django project, settings, Docker Compose, ruff/mypy/pytest, CI.
2. **Domain core:** Jalali month math, schedule templates, allocation (partial, over-, multi-loan FIFO),
   schedule versioning, ledger buckets, and the invariant. Property tests for rounding and allocation.
3. **Models, admin and roles:** superuser/operator groups, history, 2FA. Import the current records
   (from whatever spreadsheet exists today) with a one-off script and reconcile to the bank balance.
4. **SMS parsers and the transaction entry flows** in the web panel (§5.2, §5.3).
5. **Forecast and dashboard:** monthly report, free-to-lend, and the shortfall, surplus, and overdue alerts,
   plus the deferral tool.
6. **Bale bot:** borrower application, status updates, and admin flows (the three buttons, approval
   actions, daily digest).
7. **Reminders:** bot plus Safir, templates, logging.
8. **Bulk-transfer export** in the payout bank's format (Stage A).
9. **Bale wallet invoices** (Stage B), once §7 is verified.

## 9. Still open

- [ ] The current records to import: what are they kept in (spreadsheet or notes), and how many active loans and lenders are there?
- [ ] Which bank is used for payouts, plus a sample (blank) bulk-transfer template from its internet banking.
- [ ] Confirm the month-offset example in §3 (disbursed 1405/07/10, first installment 1405/08/30).
- [ ] A Bale bot token and Safir access (a new bot, or reuse the existing Safir account?).
- [ ] Hosting: ArvanCloud as before?
- [ ] Bale wallet questions from §7-B, sent to Bale support.
