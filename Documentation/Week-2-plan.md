# Week 2 — Ledger Core: Execution Plan

> Deep-dive execution plan for the week described in [Week-2-tasks.md](Week-2-tasks.md).
> Goal: a **working, tested double-entry ledger core**. By Friday: accounts exist, balanced
> journal entries post atomically, balances derive correctly, unbalanced entries are rejected by
> *both* the service layer and the database itself — and a passing test suite in CI proves all
> of it.

**Done when** (the one-line version): `post_entry()` is the only way money moves, the Postgres
deferred trigger makes an unbalanced ledger impossible even for raw SQL, every amount is an
exact `Decimal` with an explicit rounding policy, and CI is green over a suite that tests each
of those claims independently.

---

## 1. Where Week 1 left off

Everything below builds on the committed skeleton — nothing here starts from scratch:

| Already in place | Where |
| --- | --- |
| Django 5.2 + DRF project, settings split, health endpoint | `backend/config/`, `backend/common/views.py` |
| Error envelope + cursor pagination (ADR-0006) | `backend/common/exceptions.py`, `backend/common/pagination.py` |
| Empty app stubs waiting for models | `backend/accounts/models.py`, `backend/ledger/models.py` |
| `uuid6` dependency for UUIDv7 PKs (ADR-0005) | `backend/pyproject.toml` |
| pytest + factory_boy + coverage harness | `backend/tests/conftest.py` |
| CI: ruff + mypy + pytest against Postgres 16 | `.github/workflows/ci.yml` — **no CI changes needed this week** |
| The build target, field by field | [docs/er-diagram.md](../docs/er-diagram.md) |

Relevant locked decisions: [ADR-0005 UUIDv7 PKs](../docs/adr/0005-uuidv7-primary-keys.md),
[ADR-0007 single-currency USD](../docs/adr/0007-single-currency-usd.md),
[ADR-0008 simplified signed ledger](../docs/adr/0008-simplified-signed-ledger.md).

## 2. Decisions to lock this week (→ ADR-0009)

Write these as **ADR-0009: Money precision and rounding policy** on Day 1, before any model
code — every later file references it.

1. **Precision contract.** Monetary amounts are `NUMERIC(20,4)` — stored at 4 decimal places
   (headroom for interest/fee math), *presented* at 2. Prices (Week 5) are also
   `NUMERIC(20,4)`. Share quantities are `NUMERIC(20,8)`: **fractional shares are in** — they
   make the Week 5–6 brokerage demo materially richer and cost nothing now beyond writing the
   constant down.
2. **Decimal, always.** Money is `decimal.Decimal` in Python from the moment it exists. No
   `float` ever touches an amount — not in tests, not in fixtures, not in a print statement
   that later gets copy-pasted.
3. **Rounding policy.** `ROUND_HALF_EVEN` (banker's rounding) everywhere, applied through
   exactly one helper — `quantize_money()` in `common/money.py`. Chosen explicitly: half-even
   avoids systematic drift when summing many rounded values. No inline `.quantize()` calls
   anywhere else in the codebase.
4. **Balance strategy.** Balance = `SUM(journal_line.amount)` for the account — **derived on
   read, never stored** (ADR-0008). A denormalized balance cache is a later optimization with
   its own invalidation problems; deriving keeps correctness unambiguous while the ledger is
   young.
5. **Enforcement strategy — defense in depth.** The posting service validates first (fast,
   good error messages); the database enforces the same invariant with a deferred constraint
   trigger (unforgeable, catches buggy code paths and raw SQL alike). Both layers get their own
   tests, independently.

## 3. Day-by-day schedule

### Day 1 (Mon) — The money contract and the models

**Morning — money first, models second:**
- `backend/common/money.py`:

  ```python
  from decimal import ROUND_HALF_EVEN, Decimal

  MONEY_QUANTUM = Decimal("0.0001")     # NUMERIC(20,4) — storage precision
  CENT = Decimal("0.01")                # presentation precision
  SHARE_QUANTUM = Decimal("0.00000001") # NUMERIC(20,8) — fractional shares (Week 5)

  def quantize_money(value: Decimal) -> Decimal:
      """The only place money is rounded. ROUND_HALF_EVEN per ADR-0009."""
      return value.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)
  ```
- Write **ADR-0009** (`docs/adr/0009-money-precision-and-rounding.md`) covering §2 above; add
  it to the ADR index.

**Afternoon — models exactly as drawn in the [ER diagram](../docs/er-diagram.md):**
- `accounts.Account` and `ledger.JournalEntry` / `ledger.JournalLine` (full field specs in
  §4.1 below).
- Generate `0001_initial` migrations for both apps; `migrate` against dockerized Postgres.
- Sanity check in `shell`: create an account, confirm the UUIDv7 PK is time-ordered.

### Day 2 (Tue) — The posting primitive

- `backend/ledger/exceptions.py`: small domain hierarchy — `LedgerError` base,
  `InvalidEntryError`, `UnbalancedEntryError`.
- `backend/ledger/services.py`: `post_entry()` — the single choke point every future money
  movement (transfers W3, fills W5, fees) will call. Full spec in §4.2.
- `get_balance(account)` query helper (§4.3).
- First tests alongside: balanced entry posts; each service-level rejection raises and
  persists nothing.

### Day 3 (Wed) — The database guarantee (the flex)

- New migration `ledger/migrations/0002_entry_balanced_trigger.py` using `RunSQL` with both
  forward and reverse SQL — full listing in §4.4.
- Key property: the trigger is `DEFERRABLE INITIALLY DEFERRED`, so it runs at **COMMIT**,
  after the entry header and *all* its lines are inserted within one transaction. Mid-transaction
  imbalance is fine; committing an imbalance is impossible.
- Prove it by hand before writing tests: raw `INSERT` an unbalanced line in `dbshell`, watch
  `COMMIT` fail.
- Then the real tests — including the pytest transaction gotcha explained in §5.

### Day 4 (Thu) — Feedback surfaces: admin, factories, demo command

- Register models in Django admin (§4.5) — visual feedback while building, and the append-only
  posture (no delete buttons) is itself a statement.
- `backend/tests/factories.py`: `UserFactory`, `AccountFactory`, and a `post_balanced_entry()`
  test helper that goes through the service (never `objects.create` for lines in feature tests).
- Management command `post_demo_entries` (§4.6): seeds a demo user, two accounts, posts a
  couple of entries via `post_entry()`, prints derived balances.

### Day 5 (Fri) — Prove it, then polish

- Finish the test matrix (§5); run `uv run pytest --cov` and fill any gaps in
  `ledger/services.py` coverage.
- `uv run ruff check . && uv run ruff format --check . && uv run mypy .` — all green locally,
  then confirm CI is green.
- Docs sync: bump README status line (Week 1 → Week 2 done), confirm
  [docs/er-diagram.md](../docs/er-diagram.md) still matches what was actually built, cross-link
  ADR-0009.
- Buffer for whatever slipped. If somehow ahead: draft the Week 3 transfer service signature on
  paper (not in code).

## 4. Component specs

### 4.1 Models

**`accounts.Account`** (`backend/accounts/models.py`):

| Field | Type | Notes |
| --- | --- | --- |
| `id` | `UUIDField(primary_key=True, default=uuid6.uuid7, editable=False)` | ADR-0005 |
| `owner` | `ForeignKey(settings.AUTH_USER_MODEL, on_delete=PROTECT, related_name="accounts")` | |
| `name` | `CharField(max_length=100)` | |
| `account_type` | `CharField(max_length=10, choices=AccountType.choices)` | `TextChoices`: `ASSET/LIABILITY/EQUITY/INCOME/EXPENSE` — present from day one, not yet driving signs (ADR-0008) |
| `currency` | `CharField(max_length=3, default="USD")` | ADR-0007 |
| `created_at` | `DateTimeField(auto_now_add=True)` | |

**`ledger.JournalEntry`** (`backend/ledger/models.py`):

| Field | Type | Notes |
| --- | --- | --- |
| `id` | `UUIDField(primary_key=True, default=uuid6.uuid7, editable=False)` | |
| `description` | `CharField(max_length=255)` | |
| `created_at` | `DateTimeField(auto_now_add=True)` | |

`idempotency_key` is **deliberately absent** — it lands with transfers in Week 3 (already drawn
in the ER diagram so the unique constraint is planned, not improvised).

**`ledger.JournalLine`**:

| Field | Type | Notes |
| --- | --- | --- |
| `id` | default `BigAutoField` | internal-only, never exposed via API — no UUID needed |
| `entry` | `ForeignKey(JournalEntry, on_delete=PROTECT, related_name="lines")` | PROTECT: the ledger is append-only |
| `account` | `ForeignKey("accounts.Account", on_delete=PROTECT, related_name="lines")` | |
| `amount` | `DecimalField(max_digits=20, decimal_places=4)` | signed: positive = credit to the account, negative = debit (ADR-0008) |
| `currency` | `CharField(max_length=3, default="USD")` | |
| `created_at` | `DateTimeField(auto_now_add=True)` | |

`Meta`:
```python
constraints = [
    models.CheckConstraint(condition=~models.Q(amount=0), name="journal_line_amount_nonzero"),
]
indexes = [
    models.Index(fields=["account", "created_at"], name="line_account_created_idx"),
]
```
The composite index is the one the balance and history queries will actually hit — no other
indexes until a query needs them.

### 4.2 The posting primitive — `ledger/services.py`

```python
@dataclass(frozen=True)
class LineSpec:
    account: Account
    amount: Decimal  # signed; will be quantized by the service

def post_entry(*, description: str, lines: Sequence[LineSpec]) -> JournalEntry:
    ...
```

Validation order (each failure raises before anything touches the database):

1. **≥ 2 lines** — a journal entry with fewer can't balance → `InvalidEntryError`.
2. **Quantize every amount** through `quantize_money()` — validation happens on exactly the
   values that will be stored.
3. **No zero lines** (post-quantization) → `InvalidEntryError`.
4. **Uniform currency** — every line's currency matches its account's, and all are `USD`
   (ADR-0007) → `InvalidEntryError`.
5. **Zero-sum**: `sum(amounts) == Decimal("0")` → `UnbalancedEntryError` with the offending sum
   in the message.

Then, inside `transaction.atomic()`: create the `JournalEntry`, `bulk_create` the lines, return
the entry. `bulk_create` skips signals/`save()` — acceptable because this service is the *only*
sanctioned write path to `JournalLine`.

No transfers, no API endpoint, no idempotency here — this is the core primitive Week 3 wraps.

### 4.3 Balance query

```python
def get_balance(account: Account) -> Decimal:
    result = account.lines.aggregate(
        balance=Coalesce(
            Sum("amount"),
            Value(Decimal("0.0000"), output_field=DecimalField(max_digits=20, decimal_places=4)),
        )
    )
    return result["balance"]
```

Lives in `ledger/services.py` next to `post_entry` (a service-layer read, not an `Account`
method — `accounts` shouldn't import ledger internals). `Coalesce` makes the empty-account case
return `Decimal("0.0000")`, not `None`.

### 4.4 The database guarantee — deferred constraint trigger

`ledger/migrations/0002_entry_balanced_trigger.py`, depending on `("ledger", "0001_initial")`:

```sql
-- Forward
CREATE FUNCTION assert_entry_balanced() RETURNS trigger AS $$
DECLARE
    target_entry uuid;
    total numeric;
BEGIN
    target_entry := COALESCE(NEW.entry_id, OLD.entry_id);
    SELECT COALESCE(SUM(amount), 0) INTO total
    FROM ledger_journalline
    WHERE entry_id = target_entry;
    IF total <> 0 THEN
        RAISE EXCEPTION 'journal entry % is unbalanced: lines sum to %', target_entry, total;
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE CONSTRAINT TRIGGER journal_entry_balanced
    AFTER INSERT OR UPDATE OR DELETE ON ledger_journalline
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION assert_entry_balanced();
```

```sql
-- Reverse
DROP TRIGGER journal_entry_balanced ON ledger_journalline;
DROP FUNCTION assert_entry_balanced();
```

Why each piece matters (this is the interview beat — be able to say all of it):

- **`CONSTRAINT TRIGGER … DEFERRABLE INITIALLY DEFERRED`**: the check runs at `COMMIT`, not per
  statement — so inserting header + lines one row at a time inside a transaction is fine, but
  the transaction *cannot commit* unbalanced. A plain trigger would fire after the first line
  (necessarily unbalanced) and make multi-line inserts impossible.
- **`AFTER INSERT OR UPDATE OR DELETE`**: covers every mutation path — you can't unbalance an
  entry by editing or deleting a line either. (`COALESCE(NEW, OLD)` handles `DELETE`, where
  `NEW` is null. Edge case: an `UPDATE` that moves a line to a *different* entry re-checks only
  the destination — acceptable for v1 since nothing updates `entry_id`, but worth knowing.)
- **Re-summing per row** is O(lines-per-entry) at commit — entries have 2–4 lines, so this is
  noise. Correctness over cleverness.

Now even a buggy code path or a psql `INSERT` can't leave the ledger unbalanced. *"The database
itself enforces the invariant"* — hard for an interviewer to argue with.

### 4.5 Django admin

- `AccountAdmin`: `list_display = (name, owner, account_type, currency, balance, created_at)`
  where `balance` comes from a `Sum("lines__amount")` annotation in `get_queryset()` (one
  query, no N+1).
- `JournalEntryAdmin`: `JournalLineInline` (`TabularInline`) so an entry and its lines read as
  one document; `list_display = (id, description, created_at)`.
- Append-only posture: `has_delete_permission` → `False` on entry and line admins;
  lines read-only in the inline. The admin is a viewing/debugging surface, not a write path.

### 4.6 Management command

`backend/ledger/management/commands/post_demo_entries.py`:
`uv run python manage.py post_demo_entries` — idempotent-ish demo seeding: get-or-create a demo
user with a checking and a savings account, post 2–3 balanced entries **via `post_entry()`**
(e.g. an opening deposit against an equity account, a small movement between the two), then
print each account's derived balance. Toy data only — the real Faker seed script is Week 8.

## 5. Test plan — the heart of the week

New files under `backend/tests/`: `factories.py`, `test_money.py`, `test_models.py`,
`test_posting.py`, `test_trigger.py`, `test_locking.py`.

### The gotcha to understand before writing `test_trigger.py`

pytest-django's default `django_db` wraps each test in a transaction it **rolls back** — the
`COMMIT` never happens, so a deferred trigger never fires and a naive test passes vacuously.
Two escape hatches, use both:

1. **`SET CONSTRAINTS ALL IMMEDIATE`** — inside a normal test, create the unbalanced line, then
   execute this statement to force deferred constraints to check *now*. Fast; keeps the
   rollback-based isolation.
2. **`@pytest.mark.django_db(transaction=True)`** — a real transaction with a real `COMMIT`
   (TransactionTestCase semantics; slower, truncates tables). Use for exactly one test that
   proves the honest end-to-end claim: *commit itself fails*.

The trigger's `RAISE EXCEPTION` surfaces through the driver as a `django.db.utils` error
(`InternalError`/`OperationalError` depending on mapping) — assert on `django.db.Error` plus
`"unbalanced"` in the message rather than pinning the exact subclass.

### Test matrix

| # | Test | Proves | Mechanism |
| --- | --- | --- | --- |
| 1 | `test_quantize_half_even_at_boundary` | Rounding policy is exactly as specified | `Decimal("0.00005") → 0.0000`, `Decimal("0.00015") → 0.0002`, `Decimal("0.00025") → 0.0002` (ties go to even) |
| 2 | `test_no_float_drift` | Decimal arithmetic is exact where float is not | `Decimal("0.1") + Decimal("0.2") == Decimal("0.3")`; sum of 10 × `0.10` deposits equals exactly `1.0000` |
| 3 | `test_balanced_entry_posts` | Happy path | `post_entry` with `+100.00 / -100.00`; entry + 2 lines persisted, amounts stored quantized |
| 4 | `test_unbalanced_entry_rejected_by_service` | Service-layer guard | Lines summing to `0.01`; `UnbalancedEntryError`; **assert nothing persisted** |
| 5 | `test_entry_requires_two_lines` | Structural guard | 0 and 1 line → `InvalidEntryError` |
| 6 | `test_zero_amount_line_rejected` | Zero-line guard, both layers | Service raises `InvalidEntryError`; separately, direct ORM insert violates the CHECK constraint |
| 7 | `test_currency_mismatch_rejected` | ADR-0007 guard | A non-USD line → `InvalidEntryError` |
| 8 | `test_unbalanced_insert_rejected_by_trigger` | **DB enforces the invariant without the service** | Bypass `post_entry`, insert a lone unbalanced line via ORM, `SET CONSTRAINTS ALL IMMEDIATE` → `django.db.Error` with "unbalanced" |
| 9 | `test_unbalanced_commit_fails` | The real commit-time guarantee | Same bypass under `django_db(transaction=True)`; the `atomic` block's exit (COMMIT) raises |
| 10 | `test_balances_derive_across_accounts` | Balance correctness | 3 accounts, several entries; each `get_balance` matches hand-computed sums; empty account → `0.0000` |
| 11 | `test_select_for_update_locks_row` | Locking intent (stub for Week 3) | Under `transaction=True`: `Account.objects.select_for_update().get(...)` inside `atomic`; a second connection with `SELECT … FOR UPDATE NOWAIT` fails to acquire. Full concurrency suite arrives with transfers in Week 3 |

Factories in feature tests go **through the service** — `objects.create` on `JournalLine`
appears only in tests whose purpose is bypassing the service (6, 8, 9).

Coverage: `ledger/services.py` and `common/money.py` at 100% — they're small and they're the
point of the week.

## 6. Risks & gotchas

- **The deferred-trigger/test-transaction interaction** (§5) is the one that silently produces
  a passing-but-meaningless test. Write test 8 first and make sure it *fails* before the
  trigger migration exists.
- **Migration ordering**: `0002_entry_balanced_trigger` must depend on `0001_initial`; the
  trigger names the concrete table `ledger_journalline` — if the model is ever renamed, the
  migration is part of the rename.
- **Postgres-only** from here on: the trigger and `select_for_update` don't exist on SQLite.
  Fine — dev runs dockerized Postgres and CI already has a PG16 service. Never "just quickly
  test on SQLite."
- **mypy strictness** (`disallow_untyped_defs`) applies to the new service layer — type
  `LineSpec`, `post_entry`, `get_balance` fully; `django-stubs` handles the managers.
- **PROTECT everywhere**: deleting a user with accounts, an account with lines, or an entry
  with lines must all raise `ProtectedError`. That's correct — an append-only ledger forgets
  nothing.
- **Admin is not a write path**: the inline is read-only precisely because admin edits would
  bypass `post_entry` (the trigger would still catch imbalance — that's the depth in
  defense-in-depth — but the service's error messages are the intended UX).

## 7. Explicitly out of scope this week

| Deferred | Lands |
| --- | --- |
| Transfers (two-legged postings wrapping `post_entry`) | Week 3 |
| Idempotency keys + unique constraint | Week 3 |
| Full concurrency tests, deterministic lock ordering, overdraft policy | Week 3 |
| REST API endpoints for accounts/entries | Week 3+ (nothing user-facing needs them yet) |
| Auth hardening, MFA, audit log | Week 4 |
| Denormalized balance cache | Later, if ever — derive-on-read is the v1 contract |
| Normal-balance sign conventions per `account_type` | Post-v1 (ADR-0008 keeps the path open) |

## 8. Definition of done

- [ ] ADR-0009 written and indexed; `common/money.py` is the only place rounding happens
- [ ] `Account`, `JournalEntry`, `JournalLine` migrated with CHECK constraint and composite index
- [ ] `post_entry()` posts balanced entries atomically; every invalid shape raises a domain error
- [ ] Deferred constraint trigger live; unbalanced data cannot COMMIT even via raw SQL
- [ ] Balances derive correctly via `get_balance()`; empty account reads `0.0000`
- [ ] Admin registered (append-only posture); `post_demo_entries` seeds and prints balances
- [ ] Test matrix (§5) complete and green; services + money helper at 100% coverage
- [ ] `ruff check`, `ruff format --check`, `mypy`, `pytest --cov` all green locally **and in CI**
- [ ] README status updated; ER diagram matches reality

## 9. Suggested commit sequence

Six logical commits, each independently green:

1. `docs: ADR-0009 money precision and rounding policy` — ADR + `common/money.py` + `test_money.py`
2. `feat(ledger): Account, JournalEntry, JournalLine models and initial migrations` — + `test_models.py`, factories
3. `feat(ledger): post_entry posting primitive and derived balances` — services, exceptions, `test_posting.py`
4. `feat(ledger): enforce zero-sum invariant with deferred Postgres constraint trigger` — migration + `test_trigger.py`
5. `feat(ledger): admin registration and post_demo_entries command`
6. `test(ledger): select_for_update locking intent; docs sync` — `test_locking.py`, README/ER updates
