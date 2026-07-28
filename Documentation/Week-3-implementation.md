# Week 3 — Transfers: Implementation Summary

Build log for the week planned in [Week-3-plan.md](Week-3-plan.md). Everything below is
implemented, migrated against Postgres 16, and green across the full CI gate set.

## Outcome

Money moves. `transfer()` is the first real feature on the Week 2 ledger spine, and it is the
piece that had to be right: it locks both account rows in a fixed order so opposing transfers
queue instead of deadlocking, reads the source balance *under that lock* so a race cannot
overdraw, and replays instead of double-posting when a request carries an idempotency key. A
transfer is still just a balanced two-line journal entry, so it inherits every guarantee the
ledger core already proved — including the deferred trigger that makes an unbalanced commit
impossible.

The first REST endpoints are live: accounts with derived balances, paginated transaction history,
and the transfer endpoint, all owner-scoped behind `IsAuthenticated` and returning the ADR-0006
error envelope.

### Verification (all green)

| Gate | Result |
| --- | --- |
| `uv run pytest` | **57 passed** (24 from Week 2, 33 new) against dockerized Postgres 16 |
| `uv run ruff check .` | All checks passed |
| `uv run ruff format --check .` | Clean (53 files) |
| `uv run mypy .` | Success: no issues in 53 source files |
| Coverage | `ledger/services.py` **100%**, `ledger/views.py` **100%**, serializers 100%; 90% overall |
| `manage.py migrate` | `ledger/0003` applies cleanly; unique constraint verified in `\d ledger_journalentry` |

Uncovered lines are `__str__` methods, admin display helpers, and the demo command — deliberately
not chased, since the week's correctness lives in the service and the view.

### The concurrency tests were verified to actually fail

Green concurrency tests are worthless if they'd pass against broken code, so both were checked
against a deliberately sabotaged service before being trusted:

| Sabotage | Result |
| --- | --- |
| Lock accounts in caller order instead of sorted UUID order | `test_opposite_transfers_do_not_deadlock` fails with a real `django.db.utils.OperationalError: deadlock detected` from Postgres |
| Remove the row locks entirely | `test_concurrent_transfers_cannot_overdraw` fails: `['posted', 'posted']` — the classic lost update, 160 moved out of a 100 balance |

The service was restored and re-verified byte-identical after each experiment.

### Exercised by hand over real HTTP

Beyond the suite, the endpoints were driven end to end against `runserver` with a real JWT from
the token endpoint:

| Step | Result |
| --- | --- |
| `GET /accounts/` with no token | `401` + `{"error":{"code":"not_authenticated",...}}` |
| `POST /auth/token/` | access token issued |
| `GET /accounts/` | three demo accounts, balances as strings (`"304.2500"`) |
| `GET /accounts/{id}/transactions/` | signed lines, newest first |
| `POST /transfers/` (key `smoke-key-001`) | `201`, two lines `-12.5000` / `12.5000` |
| Same request again | **`200`**, identical entry `id` — replay, not a second posting |
| `POST /transfers/` for 999999.00 | `400` `insufficient_funds`, balance untouched |

Closing check against the database: the three demo balances sum to zero and
`SUM(amount)` over every journal line in the ledger is exactly `0.0000`, with exactly one entry
carrying `smoke-key-001`.

## What was built

### The decision record (ADR-0010)
- **`docs/adr/0010-transfer-semantics-and-concurrency.md`** — transfers as journal entries (no
  `Transfer` table), reject-on-insufficient-funds, ascending-UUID lock ordering, client-supplied
  idempotency keys with replay-not-error semantics. Includes the honest scope boundary: the
  overdraft check is sound *among writers that follow the lock-first protocol*; what holds
  unconditionally, enforced by the database, is the zero-sum invariant.
  *(`docs/` is gitignored by project design — ADRs live locally, like ADR-0001–0009.)*

### Schema
- **`ledger/migrations/0003_journalentry_idempotency_key.py`** — `CharField(max_length=64,
  null=True, unique=True)` on `JournalEntry`. Postgres unique indexes ignore NULLs, so every
  keyless entry from Week 2 is unaffected.

### The transfer service
- **`ledger/services.py`** — `post_entry()` gained a pass-through `idempotency_key`;
  `transfer()` added alongside it, returning `(entry, created)` so callers can tell a fresh
  posting from a replay. Helpers `_lock_accounts()` (sorted `select_for_update`) and
  `_entry_for_key()`.
- **`ledger/exceptions.py`** — `InsufficientFundsError` joins the domain hierarchy.

Flow: quantize and reject non-positive amounts → reject self-transfers → cheap replay check
(a settled retry takes no locks at all) → `atomic` → lock both rows in UUID order → re-check
replay under the lock → overdraft check under the lock → `post_entry()`. Around it all, an
`IntegrityError` handler **outside** the atomic block recovers the losing side of a key race by
refetching.

### The API surface
| Method | Path | Notes |
| --- | --- | --- |
| `GET` | `/api/v1/accounts/` | Owner-scoped, balance annotated in one query |
| `GET` | `/api/v1/accounts/{id}/` | 404 for someone else's account |
| `GET` | `/api/v1/accounts/{id}/transactions/` | Cursor-paginated lines, rides `line_account_created_idx` |
| `POST` | `/api/v1/transfers/` | **201** fresh, **200** on idempotent replay |

- **`accounts/serializers.py`**, **`accounts/views.py`** — `AccountSerializer` (money as a
  `DecimalField`, so `"150.0000"` crosses the wire as a string, never a float) and a
  `ReadOnlyModelViewSet` scoped by `get_queryset`.
- **`ledger/serializers.py`**, **`ledger/views.py`** — entry/line projections, the transfer
  request contract, the history list view, and the transfer endpoint.
- **`ledger/api_exceptions.py`** — `insufficient_funds`, `same_account`, `destination_not_found`,
  `invalid_transfer`: distinct machine codes so a client can branch without parsing prose. They
  flow through the existing ADR-0006 handler unchanged.
- **`config/settings/base.py`** — DRF default permission is now `IsAuthenticated`. Health and the
  SimpleJWT token views opt out explicitly; the safe direction to forget in is "denied".
- **`config/urls.py`** — `DefaultRouter` for accounts, explicit paths for history and transfers.

### Reuse, not duplication
- **`accounts/models.py`** — the balance annotation became `AccountQuerySet.with_balance()`, and
  `accounts/admin.py` was refactored onto it. One definition of "balance", used by the admin
  changelist and the API, still one query for N accounts. It annotates through the `lines`
  reverse accessor by name, so the accounts app still imports nothing from `ledger`.

### Demo surface
- **`ledger/management/commands/post_demo_entries.py`** — now seeds a savings account and moves
  money into it through `transfer()`, printing whether the transfer posted or replayed.

## Tests (`backend/tests/`)
`factories.py` gained `fund_account()` — opening balances posted against a fresh equity account,
so funding never runs through the code under test.

| File | Covers |
| --- | --- |
| `test_transfers.py` (14) | key stored/unique, NULL keys coexist, money moves, non-positive amounts, self-transfer, insufficient funds (nothing persisted), exact-to-zero allowed, sequential replay, quantization, signed opposite lines, simulated key-race recovery, and two tests that a non-collision `IntegrityError` is re-raised rather than reported as a replay |
| `test_concurrency.py` (3) | no lost updates, no deadlock under opposing transfers, one entry under an idempotency race |
| `test_api_accounts.py` (6) | 401 unauthenticated, owner scoping, balance as string, 404 for others' accounts, pagination across two pages, 404 on others' history |
| `test_api_transfers.py` (10) | 401, happy path with read-side agreement, insufficient-funds envelope, same-account, replay 201→200, unowned source 404, unknown destination, non-positive amounts, and a service-level rejection mapping to a 4xx envelope rather than a 500 |

## Notable engineering notes
- **`IntegrityError` recovery must sit outside `atomic`.** Inside a failed transaction every
  subsequent query raises `TransactionManagementError`, so the refetch would fail exactly when
  it's needed.
- **The same-pair idempotency race never reaches that handler**, and that's the design working:
  the loser blocks on the row lock, and its post-lock re-check sees the winner's committed entry.
  The `IntegrityError` path is the backstop for races that share a key but not an account pair, so
  it's covered by a deterministic simulation rather than a flaky timing test.
- **Lock order has to be explicit.** `select_for_update()` over a `pk__in` filter locks in plan
  order, which the query doesn't control; sequential `.get()` calls over a sorted list do.
- **Concurrency tests need `transaction=True`, a `Barrier`, per-thread `connection.close()`, and
  future timeouts.** Miss the barrier and the calls serialize; miss the timeout and a real
  deadlock hangs CI instead of failing it.
- **DRF rejects over-precise amounts** rather than quantizing them: `"10.12345"` is a field
  validation error at the API boundary, while the service quantizes half-even for its own callers.

## Deferred (not this week)
Auth hardening — refresh rotation, reuse detection, blocklist, TOTP MFA — plus the audit log and
throttling (Week 4); instruments, prices, orders, Celery (Week 5); positions, statements,
WebSockets (Week 6). Payload-match validation on idempotent replay is a documented v1 limitation
in ADR-0010. The balance cache stays deferred by design — derive-on-read is still the contract.
