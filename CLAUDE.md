# CLAUDE.md — Dast-be-Dast

**`PLAN.md` is the plan of record.** Read it before changing behaviour; update it when scope changes.

## Money-safety rules
- Money is integer **rials** everywhere in code; tomans only in display.
- Never add a path that moves money or confirms a transaction without an admin confirmation.
- Confirmed transactions are never edited in place — reverse/adjust with a new record.
- Schedules are versioned; renegotiation replaces unpaid installments only.
- The ledger invariant `cash = equity + liabilities − receivables` must hold; tests enforce it.

## PII
- Public repo: no real names, phones, IBANs, bank SMS or balances in code, fixtures, commits or issues.
- Never log IBANs or phone numbers.

## Bale
- Use Bale's own API per https://docs.bale.ai (`tapi.bale.ai`, long polling) — not a Telegram library.
- Safir (bulk/by-phone messaging) client pattern: see the owner's Bale-Message-Sender project.

## Style
- Domain logic in `src/dastbedast/core/` is pure Python (no Django imports), fully unit-tested.
- ruff is the source of truth for lint/format; type hints on `src/` signatures; pytest.
