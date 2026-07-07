# Week 2 — Ledger Core: Implementation Summary

Build log for the week planned in [Week-2-plan.md](Week-2-plan.md). Everything below is
implemented, migrated against Postgres 16, and green across the full CI gate set.

## Outcome

A working, tested double-entry ledger core. Accounts exist; balanced journal entries post through
a single atomic primitive; balances derive on read; and unbalanced entries are rejected by **both**
the posting service and the database itself. The zero-sum invariant is enforced at COMMIT by a
`DEFERRABLE INITIALLY DEFERRED` Postgres constraint trigger, so the ledger cannot go unbalanced
even via raw SQL.

### Verification (all green)

| Gate | Result |
| --- | --- |
| `uv run pytest` | **24 passed** (~16s, against dockerized Postgres 16) |
| `uv run ruff check .` | All checks passed |
| `uv run ruff format --check .` | Clean |
| `uv run mypy .` | Success: no issues in 42 source files |
| `manage.py migrate` | Applies cleanly incl. trigger; verified `tgdeferrable`/`tginitdeferred` = true, CHECK constraint and composite index present |

## What was built

### The money contract (ADR-0009)
- **`docs/adr/0009-money-precision-and-rounding.md`** — locks money at `NUMERIC(20,4)`, always
  `Decimal`, `ROUND_HALF_EVEN`, share quantities at `NUMERIC(20,8)` (fractional shares = yes).
  *(Note: `docs/` is gitignored by project design — ADRs live locally, like ADR-0001–0008.)*
- **`backend/common/money.py`** — `MONEY_QUANTUM`, `CENT`, `SHARE_QUANTUM`, and `quantize_money()`:
  the single sanctioned place money is rounded.

### Models (`docs/er-diagram.md` build target)
- **`backend/accounts/models.py`** — `Account` (UUIDv7 PK, `owner` FK `PROTECT`, `AccountType`
  text choices, `currency` default USD) and the `AccountType` enum.
- **`backend/ledger/models.py`** — `JournalEntry` (UUIDv7 header) and `JournalLine`
  (signed `DecimalField(20,4)`, `PROTECT` FKs, `CheckConstraint` amount ≠ 0, composite index on
  `(account, created_at)`).
- Migrations: `accounts/0001_initial`, `ledger/0001_initial`.

### The posting primitive
- **`backend/ledger/exceptions.py`** — `LedgerError` → `InvalidEntryError`, `UnbalancedEntryError`.
- **`backend/ledger/services.py`** — `LineSpec` dataclass; `post_entry()` (validates ≥2 lines →
  quantize → no zero lines → uniform USD → zero-sum, then creates entry + `bulk_create` lines in
  one `transaction.atomic()`); `get_balance()` (aggregate `Sum` + `Coalesce` to `0.0000`).

### The database guarantee
- **`backend/ledger/migrations/0002_entry_balanced_trigger.py`** — `RunSQL` creating the plpgsql
  `assert_entry_balanced()` function and a `CONSTRAINT TRIGGER … DEFERRABLE INITIALLY DEFERRED`
  on `ledger_journalline` (INSERT/UPDATE/DELETE), with reverse SQL.

### Feedback surfaces
- **`backend/accounts/admin.py`** — `AccountAdmin` with a derived balance column annotated in one
  query (no N+1).
- **`backend/ledger/admin.py`** — `JournalEntryAdmin` + read-only `JournalLineInline`; delete
  disabled (append-only posture).
- **`backend/ledger/management/commands/post_demo_entries.py`** — seeds a demo user + two accounts,
  posts two balanced entries through the service, prints derived balances.

### Tests (`backend/tests/`)
`factories.py` (factory_boy `UserFactory`/`AccountFactory` + `post_balanced_entry` helper) plus:

| File | Covers |
| --- | --- |
| `test_money.py` | banker's rounding at the tie, sign symmetry, no float drift |
| `test_models.py` | zero-amount CHECK violation; `PROTECT` on account/entry delete |
| `test_posting.py` | happy path, unbalanced rejected (nothing persisted), <2 lines, zero line, non-USD, quantization, multi-account derived balances, empty = 0 |
| `test_trigger.py` | unbalanced rejected via `SET CONSTRAINTS ALL IMMEDIATE`; commit-time failure under `transaction=True`; balanced hand-built entry commits |
| `test_locking.py` | `select_for_update` requires a transaction; real two-connection `FOR UPDATE NOWAIT` contention proof |

## Notable engineering notes
- **Deferred-trigger test gotcha:** pytest-django wraps each test in a rolled-back transaction, so
  the deferred trigger never fires. Trigger tests use `SET CONSTRAINTS ALL IMMEDIATE` (fast) and
  one `django_db(transaction=True)` test for the honest commit-time proof.
- **factory_boy + strict mypy:** calling a factory directly (`AccountFactory()`) types as the
  factory, not the model. Tests use `AccountFactory.create()`, which factory_boy 3.3's typed
  `create() -> T` resolves to `Account`.
- **Postgres-only from here:** the trigger and `select_for_update` don't exist on SQLite — dev and
  CI both run Postgres 16, so this is fine.

## Deferred (not this week)
Transfers, idempotency keys, full concurrency/lock-ordering tests, REST API endpoints (Week 3);
auth/MFA/audit (Week 4); balance cache and normal-balance sign conventions (later, by design).
