# Week 3 — Transfers: Execution Plan

> Deep-dive execution plan for the week sketched in [Week3-8.md](Week3-8.md) (Week 3 paragraph).
> Goal: the **first real feature on the ledger spine** — money moves between accounts through a
> transfer that is atomic, idempotent, row-locked, and overdraft-checked, exposed over the first
> real API endpoints. By Friday: a double-clicked transfer posts once, two simultaneous transfers
> can't overdraw an account or deadlock each other, and a JWT-holding client can list accounts,
> read transaction history, and move money over HTTP.

**Done when** (the one-line version): `transfer()` wraps `post_entry()` with deterministic lock
ordering, an under-lock overdraft check, and idempotency-key replay; a threaded test suite proves
no lost updates, no deadlocks, and no duplicate postings under concurrency; and
`POST /api/v1/transfers/` returns the error envelope for every failure mode.

---

## 1. Where Week 2 left off

Everything below builds on the committed ledger core — transfers *wrap* it, never bypass it:

| Already in place | Where |
| --- | --- |
| `post_entry()` — the single sanctioned write path; `get_balance()` derived read | `backend/ledger/services.py` |
| Zero-sum invariant enforced at COMMIT (deferred constraint trigger) | `ledger/migrations/0002_entry_balanced_trigger.py` |
| Domain exceptions: `LedgerError` → `InvalidEntryError`, `UnbalancedEntryError` | `backend/ledger/exceptions.py` |
| Money contract: `quantize_money()`, `ROUND_HALF_EVEN`, `NUMERIC(20,4)` (ADR-0009) | `backend/common/money.py` |
| Error envelope + cursor pagination (ADR-0006) | `backend/common/exceptions.py`, `common/pagination.py` |
| SimpleJWT token endpoints (stubs, but functional — enough for `IsAuthenticated`) | `config/urls.py` (`/api/v1/auth/token/`) |
| Locking primitive proven (row lock blocks a second connection) | `backend/tests/test_locking.py` — the stub this week's suite supersedes |
| Composite index the history query will ride | `line_account_created_idx (account, created_at)` |
| `idempotency_key` already drawn on JOURNAL_ENTRY as "unique, nullable (Week 3)" | [docs/er-diagram.md](../docs/er-diagram.md) |

Relevant locked decisions: [ADR-0005 UUIDv7 PKs](../docs/adr/0005-uuidv7-primary-keys.md),
[ADR-0007 single-currency USD](../docs/adr/0007-single-currency-usd.md),
[ADR-0008 simplified signed ledger](../docs/adr/0008-simplified-signed-ledger.md),
[ADR-0009 money precision](../docs/adr/0009-money-precision-and-rounding.md).

## 2. Decisions to lock this week (→ ADR-0010)

Write these as **ADR-0010: Transfer semantics and concurrency policy** on Day 1 — the service,
the tests, and the API all reference it.

1. **A transfer is a journal entry.** No `Transfer` table. A transfer is one `JournalEntry` with
   exactly two lines — `−amount` on the source, `+amount` on the destination — created through
   `post_entry()`. It inherits atomicity, the zero-sum trigger, append-only PROTECT semantics,
   everything Week 2 proved. New money-movement types (fees, fills in Week 5) follow the same
   pattern: new *shapes* of entries, not new tables.
2. **Overdraft policy: reject.** A transfer whose amount exceeds the source's balance raises
   `InsufficientFundsError`. Transferring *exactly* the balance (to zero) is allowed. The check
   runs **under the row lock**, so the balance it reads cannot be raced. Scope honesty, stated in
   the ADR: the check is only meaningful because `transfer()` is the sole concurrent writer and
   every writer follows the lock-first protocol; direct `post_entry()` calls (demo seeding now,
   trade fills in Week 5) can still drive a balance negative — the *ledger invariant* (zero-sum)
   holds regardless, which is the guarantee that actually lives in the database.
3. **Deterministic lock ordering.** Both account rows are locked with `select_for_update()` in
   **ascending UUID order**, always — regardless of which is source. Two simultaneous transfers
   A→B and B→A therefore request locks in the same order and queue instead of deadlocking. This
   is the interview beat of the week: a total order on lock acquisition makes deadlock
   structurally impossible, not just unlikely.
4. **Idempotency: client-supplied key, replay returns the original.** `idempotency_key` becomes a
   real column on `JournalEntry`: `CharField(max_length=64, null=True, unique=True)` (Postgres
   unique indexes ignore NULLs, so non-transfer entries are unaffected). A retry with the same key
   does **not** error — it returns the already-posted entry (HTTP 200 vs 201 for a fresh post).
   The unique constraint is the backstop for the race two threads can still hit; the recovery path
   catches `IntegrityError` **outside** the atomic block (inside, the transaction is poisoned) and
   refetches by key. v1 replay semantics: same key ⇒ same entry returned, payload not re-compared
   (documented limitation; strict payload-match validation is a later hardening).
5. **Money over JSON is a string.** DRF's `DecimalField` serializes to `"25.0000"` by default
   (`COERCE_DECIMAL_TO_STRING`) — keep it. No float ever crosses the wire, matching the no-float
   rule of ADR-0009.
6. **API authentication posture.** Set DRF's default to `IsAuthenticated` now (health endpoint
   explicitly `AllowAny`); clients get JWTs from the existing SimpleJWT stub endpoints. Real auth
   hardening (rotation, blocklist, MFA) stays in Week 4 — this week only *requires* a token, it
   doesn't harden the token lifecycle.

## 3. Day-by-day schedule

### Day 1 (Mon) — ADR-0010 and the idempotency plumbing

**Morning:**
- Write **ADR-0010** (`docs/adr/0010-transfer-semantics-and-concurrency.md`) covering §2; add to
  the ADR index.
- Migration `ledger/0003_journalentry_idempotency_key`: the nullable unique column, exactly as
  drawn in the ER diagram since Week 1.

**Afternoon:**
- Extend `post_entry()` with `idempotency_key: str | None = None` passed through to the entry
  (no behavior change when `None` — all 24 existing tests stay green untouched).
- Add `InsufficientFundsError(LedgerError)` to `ledger/exceptions.py`.
- Plumbing tests: key stored; duplicate key at the ORM level raises `IntegrityError`; multiple
  `NULL` keys coexist.

### Day 2 (Tue) — The transfer service

- `transfer()` in `backend/ledger/services.py` (full spec §4.2): validate → lock in UUID order →
  replay check → overdraft check under lock → `post_entry()` — plus the `IntegrityError`
  race-recovery path outside the transaction.
- Non-concurrent service tests (`test_transfers.py`): happy path, non-positive amount, same
  account, insufficient funds (nothing persisted), exact-to-zero allowed, sequential replay
  returns the original entry, quantization.

### Day 3 (Wed) — The concurrency suite (the heart of the week)

This is the "full concurrency suite" the Week 2 `test_locking.py` docstring promised:

- **Lost update**: balance 100, two threads each transfer 80 → exactly one succeeds, final
  balance 20, ledger still zero-sum.
- **Deadlock avoidance**: A→B and B→A fired simultaneously → both complete inside a timeout
  (with random lock order this deadlocks; with UUID ordering it queues).
- **Idempotency race**: two threads, same key, released by a barrier → exactly one entry exists;
  both callers get it back.
- Mechanics: `@pytest.mark.django_db(transaction=True)`, `ThreadPoolExecutor`, `threading.Barrier`
  for real simultaneity, per-thread `connection.close()` in a `finally` (§5 gotchas).

### Day 4 (Thu) — The first API surface

- **Accounts**: `AccountSerializer` + owner-scoped `AccountViewSet` (list/retrieve) with the
  balance annotated in `get_queryset()` — same one-query pattern already used in
  `accounts/admin.py`.
- **History**: `GET /api/v1/accounts/{id}/transactions/` — the account's journal lines, cursor
  pagination via existing `common/pagination.py`, riding `line_account_created_idx`.
- **Transfers**: `POST /api/v1/transfers/` — serializer validates shape; view resolves the source
  from *the requester's own accounts* (unowned/unknown source → 404, no existence leak), calls
  `transfer()`, maps domain errors into the ADR-0006 envelope (§4.4). 201 fresh / 200 replay.
- DRF default permission flips to `IsAuthenticated`; health endpoint pinned `AllowAny`.
- API tests: 401 without token, owner scoping, envelope shape and codes, replay status, string
  money.

### Day 5 (Fri) — Prove it, then polish

- Finish the test matrix (§5); `uv run pytest --cov` — `ledger/services.py` stays at 100%.
- `ruff check`, `ruff format --check`, `mypy`, full suite — green locally and in CI (no CI
  changes needed; PG16 service already there).
- Docs sync: README status bump (Week 2 → Week 3 done), er-diagram note updated
  (`idempotency_key` now real), `Documentation/Week-3-implementation.md` build log.
- Buffer. If somehow ahead: extend `post_demo_entries` to seed a demo transfer through the new
  service.

## 4. Component specs

### 4.1 Schema change — `JournalEntry.idempotency_key`

| Field | Type | Notes |
| --- | --- | --- |
| `idempotency_key` | `CharField(max_length=64, null=True, blank=True, unique=True)` | Client-supplied opaque token (UUIDs encouraged, not enforced). `unique=True` → Postgres unique index; NULLs don't collide, so entries posted without a key (all of Week 2's) are untouched. |

Migration `0003` — a plain `AddField`, no data migration needed.

### 4.2 The transfer service — `ledger/services.py`

```python
def transfer(
    *,
    source: Account,
    destination: Account,
    amount: Decimal,
    description: str = "Transfer",
    idempotency_key: str | None = None,
) -> tuple[JournalEntry, bool]:
    """Move amount from source to destination. Returns (entry, created) —
    created=False when an idempotency-key replay returned the original entry."""
```

Flow (each numbered failure raises before any write):

1. `amount = quantize_money(amount)`; `amount <= 0` → `InvalidEntryError`.
2. `source.pk == destination.pk` → `InvalidEntryError` (a self-transfer would be two lines that
   cancel on one account — legal arithmetic, meaningless banking).
3. Fast-path replay: if `idempotency_key` is set and an entry with it exists, return
   `(entry, False)` — no locks taken for a pure retry.
4. Inside `transaction.atomic()`:
   - **Lock both rows in ascending UUID order** — sequential `select_for_update().get()` calls,
     which makes acquisition order explicit by construction (a single
     `filter(pk__in=…).order_by()` does *not* guarantee lock order under every plan):

     ```python
     for pk in sorted((source.pk, destination.pk)):
         Account.objects.select_for_update().get(pk=pk)
     ```
   - Re-check replay under the lock (a racing thread may have posted between step 3 and here).
   - Overdraft: `get_balance(source) < amount` → `InsufficientFundsError`. Sound because every
     concurrent transfer holds this same lock before reading (§2.2).
   - `post_entry(description=…, lines=[LineSpec(source, -amount), LineSpec(destination, amount)],
     idempotency_key=…)` → return `(entry, True)`.
5. Around the atomic block: `except IntegrityError` naming the idempotency unique constraint →
   refetch by key, return `(entry, False)`. Catching *outside* matters — inside a poisoned
   transaction every query raises `TransactionManagementError`.

`transfer()` is intentionally the only public function added — no "withdraw"/"deposit" helpers
until something needs them.

### 4.3 API surface

| Method | Path | Returns |
| --- | --- | --- |
| `GET` | `/api/v1/accounts/` | Requester's accounts, each with derived `balance` |
| `GET` | `/api/v1/accounts/{id}/` | One owned account (404 otherwise) |
| `GET` | `/api/v1/accounts/{id}/transactions/` | Cursor-paginated journal lines, newest first |
| `POST` | `/api/v1/transfers/` | 201 the posted entry; 200 on idempotent replay |

New files: `accounts/serializers.py`, `accounts/views.py`, `ledger/serializers.py`,
`ledger/views.py`; routes wired in `config/urls.py` (a `DefaultRouter` for the accounts viewset,
an explicit `path()` for transfers).

**`AccountSerializer`**: `id`, `name`, `account_type`, `currency`, `balance` (read-only
`DecimalField(max_digits=20, decimal_places=4)` fed by the queryset annotation), `created_at`.
Queryset: `Account.objects.filter(owner=request.user).annotate(balance=Coalesce(Sum("lines__amount"), …))`
— the exact pattern already in `accounts/admin.py`, one query, no N+1.

**`TransferCreateSerializer`**: `source_account` (UUID), `destination_account` (UUID), `amount`
(`DecimalField(max_digits=20, decimal_places=4, min_value=Decimal("0.0001"))`), `description`
(optional), `idempotency_key` (optional, ≤ 64 chars). The view resolves `source_account` against
`request.user.accounts` (miss → 404); `destination_account` against all accounts (miss → 400
`destination_not_found`). Response serializes the entry with its two lines.

**Error mapping** (view catches domain exceptions, re-raises DRF `ValidationError` subtypes so
the existing `api_exception_handler` wraps them — no handler changes needed):

| Condition | HTTP | Envelope `code` |
| --- | --- | --- |
| Insufficient funds | 400 | `insufficient_funds` |
| Source == destination | 400 | `same_account` |
| Bad shape / amounts | 400 | `validation_error` (per-field `details`) |
| Source not owned / unknown | 404 | `not_found` |
| No/invalid token | 401 | `not_authenticated` |
| Idempotent replay | 200 | — (success body, same entry) |

### 4.4 Settings

`config/settings/base.py`: `REST_FRAMEWORK["DEFAULT_PERMISSION_CLASSES"] =
["rest_framework.permissions.IsAuthenticated"]` and JWT as a default authentication class;
`HealthView` gets `permission_classes = [AllowAny]`. One deliberate global default beats
remembering a decorator on every future view.

## 5. Test plan — the heart of the week

New files under `backend/tests/`: `test_transfers.py`, `test_concurrency.py`,
`test_api_accounts.py`, `test_api_transfers.py`. Week 2's `test_locking.py` stays — it proves the
primitive; the new suite proves the protocol built on it.

### The gotchas to understand before writing `test_concurrency.py`

- **`transaction=True` is mandatory.** Worker threads open their own DB connections and issue
  real COMMITs; under the default rollback-wrapped `django_db` they couldn't see the test's data
  or each other's writes.
- **Close connections in every thread** (`finally: connection.close()`), or the test run leaks
  Postgres connections until the pool starves later tests.
- **`threading.Barrier(2)`** right before each thread calls `transfer()` — otherwise the GIL
  often serializes the calls and the test passes without ever exercising contention.
- **Timeouts, not hangs**: harvest futures with `future.result(timeout=10)` so a real deadlock
  fails the test instead of freezing CI.
- `transaction=True` tests TRUNCATE tables and run slower — keep them to the three that need it.

### Test matrix

| # | Test | Proves | Mechanism |
| --- | --- | --- | --- |
| 1 | `test_idempotency_key_stored_and_unique` | Plumbing + constraint | `post_entry` with key persists it; duplicate direct insert → `IntegrityError`; two NULL-key entries coexist |
| 2 | `test_transfer_moves_money` | Happy path | Balances shift by exactly ±amount; entry has 2 lines; description kept |
| 3 | `test_transfer_rejects_nonpositive_amount` | Input guard | `0` and `-10` (parametrized) → `InvalidEntryError` |
| 4 | `test_transfer_rejects_same_account` | Input guard | source == destination → `InvalidEntryError` |
| 5 | `test_transfer_rejects_insufficient_funds` | Overdraft policy | balance 50, transfer 50.01 → `InsufficientFundsError`; **nothing persisted** |
| 6 | `test_transfer_to_exact_zero_allowed` | Policy boundary | balance 50, transfer 50 → succeeds; source reads `0.0000` |
| 7 | `test_transfer_replay_returns_original` | Idempotency, sequential | Same key twice → same entry PK, `created` False second time, money moved **once** |
| 8 | `test_transfer_quantizes_amount` | ADR-0009 continuity | `10.12345` → lines carry `10.1234` (half-even) |
| 9 | `test_concurrent_transfers_cannot_overdraw` | **No lost updates** | Balance 100; 2 threads × transfer 80; exactly one `InsufficientFundsError`; final balance 20 |
| 10 | `test_opposite_transfers_do_not_deadlock` | **Lock ordering works** | A→B ∥ B→A through a barrier; both `result(timeout=10)` complete; net balances correct |
| 11 | `test_idempotency_race_creates_one_entry` | **Constraint backstop** | 2 threads, same key, barrier → 1 entry in DB; both callers returned it |
| 12 | `test_accounts_api_requires_auth` | Auth posture | No token → 401, envelope shape `{"error": {code, message, details}}` |
| 13 | `test_accounts_list_owner_scoped_with_balances` | Read API | Two users; each sees only their accounts; `balance` is a **string** `"150.0000"` |
| 14 | `test_transactions_endpoint_paginates` | History API | > page-size lines → cursor pagination; other user's account id → 404 |
| 15 | `test_transfer_api_happy_path` | Write API | POST → 201; follow-up GET shows moved balances |
| 16 | `test_transfer_api_insufficient_funds_envelope` | Error mapping | → 400, `code == "insufficient_funds"` |
| 17 | `test_transfer_api_replay_returns_200` | API idempotency | Same key POSTed twice → 201 then 200, same entry id in both bodies |
| 18 | `test_transfer_api_unowned_source_404` | No existence leak | Token A, source owned by B → 404 |

Service tests go through `transfer()`; direct ORM writes appear only where the constraint itself
is under test (#1). Coverage: `ledger/services.py` back to 100% including both replay paths.

## 6. Risks & gotchas

- **`IntegrityError` inside `atomic` poisons the transaction** — any query before exit raises
  `TransactionManagementError`. The replay-race recovery *must* sit outside the atomic block.
  Write test #11 first and watch it fail before the recovery path exists.
- **Lock order is only deterministic if acquisition is explicit.** `select_for_update()` on a
  `pk__in` filter locks rows in plan order, not query order. Sequential `.get()` calls in sorted
  order are boring and provably correct — boring wins.
- **The overdraft check's soundness boundary** (§2.2): it holds among lock-following writers.
  Say so in ADR-0010 rather than implying a guarantee the system doesn't make; Week 5's fill
  path adopts the same protocol when it arrives.
- **Threaded tests can pass vacuously** if threads never actually overlap — the barrier is not
  optional. Also assert the *failure* leg (#9's one rejection), not just the success leg.
- **`transaction=True` flushes by truncation** — factories re-create everything per test; never
  rely on data from another test. Slow marker on the three concurrency tests if suite time grows.
- **DRF decimals**: assert API money is the string `"20.0000"`, not `20.0` — a float here means a
  serializer field is mis-declared.
- **404 vs 403 for unowned sources**: 404 everywhere (queryset scoping does this naturally) —
  a 403 confirms the account exists, which is an information leak.
- **SimpleJWT stubs are enough** for `IsAuthenticated` tests via
  `APIClient.force_authenticate()` — no need to mint real tokens per test (one smoke test can
  exercise the real token endpoint end-to-end).

## 7. Explicitly out of scope this week

| Deferred | Lands |
| --- | --- |
| Auth hardening: refresh rotation, reuse detection, blocklist, TOTP MFA | Week 4 |
| Append-only audit log wired into transfer/auth paths | Week 4 |
| DRF throttling / rate limits | Week 4 |
| Instruments, prices, orders, Celery | Week 5 |
| Positions, statements, WebSockets | Week 6 |
| Payload-match validation on idempotency replay | Later hardening (documented in ADR-0010) |
| Denormalized balance cache | Later, if ever — derive-on-read remains the contract |
| Scheduled/recurring transfers, external transfers | Not in the 8-week scope |

## 8. Definition of done

- [ ] ADR-0010 written and indexed; overdraft, lock-ordering, and replay semantics documented
- [ ] `idempotency_key` migrated with unique constraint; `post_entry()` accepts and stores it
- [ ] `transfer()` posts balanced two-line entries; every invalid shape raises a domain error
- [ ] Concurrency suite green: no lost updates (#9), no deadlocks (#10), no duplicate postings (#11)
- [ ] `GET /accounts/`, `GET /accounts/{id}/transactions/`, `POST /transfers/` live under
      `/api/v1/` with `IsAuthenticated` and envelope-shaped errors
- [ ] Full matrix (§5) green; `ledger/services.py` at 100% coverage
- [ ] `ruff check`, `ruff format --check`, `mypy`, `pytest --cov` green locally **and in CI**
- [ ] Endpoints exercised once by hand (httpie/curl with a stub JWT) — recorded in the build log
- [ ] README status updated; er-diagram idempotency note updated; `Week-3-implementation.md` written

## 9. Suggested commit sequence

Six logical commits, each independently green:

1. `feat(ledger): idempotency keys on journal entries` — migration 0003, `post_entry` plumbing,
   `InsufficientFundsError`, test #1
2. `feat(ledger): transfer service with lock ordering and overdraft policy` — `transfer()`,
   tests #2–8
3. `test(ledger): concurrency suite — lost updates, deadlock avoidance, idempotent replay` —
   tests #9–11
4. `feat(api): account endpoints with derived balances and transaction history` — serializers,
   viewset, auth default, tests #12–14
5. `feat(api): transfer endpoint with idempotent replay and error envelope` — tests #15–18
6. `docs: Week 3 sync` — README, er-diagram note, `Week-3-implementation.md`
