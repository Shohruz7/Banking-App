# Personal Banking Platform

A personal banking and brokerage platform built around a **double-entry ledger whose balance
invariant is enforced by the database itself**. Every money movement — transfers, trades, fees —
is a balanced, atomic, idempotent journal entry.

The design goal is a system that stays correct under the conditions that actually break financial
software: concurrent writes to the same account, retried requests, and partial failure. Where a
rule matters, it is enforced somewhere it cannot be bypassed — usually Postgres — rather than by
convention in application code.

## What it does

**Ledger.** Accounts hold no balance column; a balance is the sum of an account's signed journal
lines, derived on read. Two invariants are enforced at `COMMIT` by deferred Postgres constraint
triggers, so neither can be broken even by raw SQL: an entry's signed amounts sum to exactly zero,
and its share quantities net to zero **per instrument**. The second one arrived in Week 7 — before
that, shares were protected only by a rule in the service, which meant a hand-written `INSERT` could
create them from nothing. Money is `NUMERIC(20,4)`, always `Decimal`, rounded half-even in exactly
one place.

What that second trigger does *not* claim is worth stating: it enforces conservation, not
authorization. Shares cannot appear without a corresponding issuance leg — exactly as strong as the
amount invariant, and no stronger.

**Transfers.** A transfer is just a two-line journal entry, so it inherits every ledger guarantee.
On top of that it locks both account rows in a fixed order — ascending UUID, regardless of
direction — so two opposing transfers queue instead of deadlocking, and it reads the source
balance *under that lock*, so a race cannot overdraw an account. A request carrying an idempotency
key replays instead of double-posting: the retry returns the original entry rather than an error.
A key is bound to a digest of the request that first used it, so reusing one for a *different*
transfer is a 409 rather than a silent replay of the wrong movement — and, since the key namespace
is global, so is reusing one that happens to belong to somebody else.

**Authentication.** Registration, then login that issues a short-lived access token and a rotating
refresh token. TOTP MFA is enforced at login through a two-step challenge, with QR provisioning at
enrolment and each code burned after use so it cannot be replayed. Every token is bound to a
revocable session via a `sid` claim, which is what makes an *access* token revocable — a blacklist
of refresh tokens structurally cannot do that. Replaying a rotated refresh token revokes the
entire token family, so the successor an attacker holds dies along with the one they replayed.
TOTP secrets are envelope-encrypted at rest — a database dump is not an MFA bypass — under a key
encryption key that can be rotated without a flag day, behind an interface a cloud KMS drops into.

**Brokerage.** 55 seeded instruments whose prices walk under geometric Brownian motion, advanced on
a schedule by Celery Beat. Market orders fill inside the request that places them; limit orders rest
until a price tick crosses them and then fill unattended. **A fill is an ordinary journal entry** —
a buy debits cash and credits a position account at cost. A sell is three lines, and the third is
not optional: a position account's balance *is* its cost basis, so a sell removes cost rather than
proceeds, and the residual has to land in a realized-P&L account for the entry to balance. Nothing
about a holding is stored — its share count and its cost basis are both sums over the ledger, so
they cannot drift from the postings that produced them. You cannot buy with money you don't have or
sell shares you don't own, by the same lock-then-read mechanism as the overdraft check.

**Portfolio and P&L.** Cash, market value, cost basis, unrealized and realized P&L, all derived at
read time from rows that already exist — the endpoint adds no table and no migration. Realized P&L
is literally the balance of an income account, so it cannot disagree with the sells that produced
it. Nothing is stored, so nothing can drift.

**Statements.** On the 1st of each month Celery Beat renders a PDF per cash account plus a brokerage
statement per user, and stores them through Django's storage API. Running it twice produces one
statement, because two partial unique indexes say so rather than a check that could be raced. The
generated file is reachable only through an owner-scoped download endpoint — `MEDIA_URL` is not
routed at all.

**Real-time.** A WebSocket pushes fills, balance changes, transfers and price ticks as they commit.
A browser cannot set an `Authorization` header on a handshake, so the socket authenticates in its
first frame rather than through a token in the URL, and it dies three ways: the auth deadline
passes, the access token expires, or the session behind it is revoked. **Every event is published on
`transaction.commit`**, so a posting that rolls back is never announced — a rejected order reaches
the client as a rejection and produces no balance update at all.

**Audit.** Every auth event and every money movement lands in an append-only log that Postgres
itself refuses to let anyone `UPDATE` or `DELETE`. Ledger audit rows are written inside the same
transaction as the posting, so the log and the ledger commit or vanish together. Rows written from
a Celery worker carry the same actor attribution as rows written from a request.

**API.** Everything is authenticated by default; public endpoints opt out explicitly. Errors share
a single envelope. Rate limits sit on the endpoints worth attacking — registration, login, MFA,
refresh, transfers and orders each have their own ceiling.

## Stack

| Layer   | Choice                                                       |
| ------- | ------------------------------------------------------------ |
| Backend | Python 3.12 · Django 5.2 · Django REST Framework · SimpleJWT |
| Data    | PostgreSQL 16 · Redis 7                                      |
| Auth    | JWT with rotating refresh · TOTP MFA (pyotp)                 |
| Async   | Celery + Beat (price ticks, limit-order matching, statements) |
| Realtime| Django Channels over a Redis channel layer (daphne/ASGI)     |
| Reports | ReportLab PDF statements behind Django's storage API         |
| Tooling | uv (env + lockfile) · ruff (lint + format) · mypy · pytest   |

## Quickstart

```sh
# 1. Infrastructure (Postgres + Redis)
docker compose up -d

# 2. Backend
cd backend
cp .env.example .env        # adjust if needed
uv sync                     # installs Python 3.12 + all deps from the lockfile
uv run python manage.py migrate
uv run python manage.py seed_instruments --ticks 200   # 55 tickers, with chart history
uv run python manage.py runserver
```

Then: `curl http://localhost:8000/api/v1/health/` → `{"status": "ok"}`

The market only moves while Celery is running — two more terminals, from `backend/`:

```sh
uv run celery -A config worker -l info
uv run celery -A config beat -l info
```

Beat advances every instrument once a minute (`MARKET_TICK_SECONDS`), sweeps resting limit orders
after each tick, and generates statements on the 1st. Without them the API still works; prices
simply stand still.

`runserver` serves ASGI (daphne is first in `INSTALLED_APPS`), so the WebSocket is live on the same
port. Statements for any past month can be generated on demand:

```sh
uv run python manage.py generate_statements --period 2026-07
```

## API

All endpoints live under `/api/v1/`. Everything is authenticated by default; the exceptions opt out
explicitly.

**The authoritative reference is generated, not this table.** `GET /api/v1/schema/` serves an
OpenAPI document and `/api/v1/docs/` renders it; a test asserts the generator produces no warnings,
because a schema that silently omits an endpoint is worse than a stale table. The table below is a
map, and it is checked against the schema by nothing — read it for orientation and the schema for
truth.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/health/` | Liveness probe — checks nothing, on purpose (public, never throttled) |
| GET | `/ready/` | Readiness probe — Postgres and cache; 503 names the failing one |
| GET | `/schema/`, `/docs/` | The OpenAPI document, and Swagger over it |
| POST | `/auth/register/` | Create an account |
| POST | `/auth/token/` | Log in — returns a token pair, or an MFA challenge |
| POST | `/auth/token/mfa/` | Complete the MFA challenge with a TOTP code |
| POST | `/auth/token/refresh/` | Rotate the refresh token |
| POST | `/auth/logout/` | Blacklist the refresh token and revoke the session |
| GET | `/auth/me/` | The requesting user's profile |
| POST | `/auth/mfa/enroll/` | Start TOTP enrolment — returns the secret, URI and QR |
| POST | `/auth/mfa/confirm/` | Activate the authenticator |
| POST | `/auth/mfa/disable/` | Turn MFA off and revoke every session |
| GET | `/accounts/` | Owned cash accounts, balances derived, numbers masked |
| GET | `/accounts/{id}/` | One account, with its account number in full |
| GET | `/accounts/{id}/transactions/` | Journal lines, newest first, cursor-paginated |
| POST | `/transfers/` | Move money — 201 posted, 200 on replay, 409 on a key reused for a different request |
| GET | `/instruments/`, `/instruments/{symbol}/` | Tradeable instruments with their latest price |
| GET | `/instruments/{symbol}/prices/` | Tick history, newest first, cursor-paginated |
| POST | `/orders/` | Place an order — 201 `filled` (market) or 201 `open` (limit) |
| GET | `/orders/`, `/orders/{id}/` | Own orders and their outcomes, cursor-paginated |
| POST | `/orders/{id}/cancel/` | Withdraw a resting order — 409 if it already resolved |
| GET | `/holdings/` | Positions: quantity, cost basis, average cost, market value (paginated) |
| GET | `/portfolio/` | Cash, holdings value, cost basis, unrealized and realized P&L |
| GET | `/statements/` | Generated monthly statements, newest first |
| GET | `/statements/{id}/download/` | Stream the PDF — the only path to a generated file |

One WebSocket endpoint, `ws/v1/stream/`:

```js
socket.send(JSON.stringify({ type: "auth", token: accessToken }));       // → auth.ok
socket.send(JSON.stringify({ type: "subscribe", symbols: ["AAPL"] }));   // → price.tick
// → order.filled · order.rejected · order.cancelled · balance.updated · transfer.posted
```

The socket closes `4401` if it never authenticates or its token expires, `4403` when its session is
revoked, and `4429` past the subscription ceiling.

Errors share one envelope: `{"error": {"code": ..., "message": ..., "details": {...}}}` — including
404s on unrouted paths and unhandled 500s, which returned Django's HTML pages until Week 7.
An account owned by someone else returns 404, never 403 — a 403 would confirm it exists.
Money crosses the wire as a string (`"150.0000"`), never a float; so do share quantities
(`"6.50000000"`).

`/accounts/` lists cash accounts only. A position account's balance is a *cost basis*, not spendable
money, so it is reported by `/holdings/` alongside the share count that explains it.

## Testing

The suite carries more code than the application does, because the invariants are the product. It
proves the things that would be expensive to get wrong: that an unbalanced entry cannot commit,
that concurrent transfers or trades cannot overdraw an account, oversell a position, or deadlock
each other, that a retried transfer or fill posts once, that a replayed refresh token takes down its
whole family, that a forged token is rejected, and that closing a position leaves no cost-basis dust
behind.

It also proves the things a live system gets wrong quietly: that a rolled-back posting is never
announced on the socket, that a revoked session closes the connection it authenticated, and that
regenerating a month produces one statement rather than two.

The concurrency, security, rounding and event-delivery tests were each checked against a
deliberately broken implementation first, to confirm they actually fail when the property they claim
to protect is removed. Two tests were rewritten after that check showed they passed against the
broken version too.

Coverage is a gate rather than a number in a commit message: CI fails below 96%. The figure is
**app code only, with branch coverage** — it excludes the test suite, which is ~100% covered by
construction and was inflating the headline. Measured honestly it is 97%, and what remains uncovered
is admin display helpers and the demo seeding command.

```sh
cd backend
uv run pytest                            # test suite (fails under 96% coverage)
uv run ruff check .                      # lint
uv run ruff format .                     # format
uv run mypy .                            # type check
uv run python manage.py check --deploy   # production settings
uv run python manage.py check_ledger_invariants   # reconcile the ledger
uvx pre-commit install                   # git hooks (ruff + mypy on every commit)
```

## Repository layout

```
backend/
  config/     settings (base/dev/test/prod), URLs, Celery app, ASGI/WSGI
  accounts/   ledger accounts — cash and instrument positions alike
  ledger/     journal entries, the posting primitive, transfers, lock ordering
  identity/   registration, TOTP MFA, revocable auth sessions
  audit/      the append-only audit log
  markets/    instruments, price ticks, the GBM price engine, the tick task
  trading/    orders, the fill service, the matching sweep, portfolio valuation
  statements/ monthly statement data, the PDF renderer, the Beat task
  realtime/   the WebSocket consumer, socket auth, and the event publishers
  common/     money, pagination, error envelope, shared helpers
  tests/
frontend/     web client (not yet implemented)
```

## Architecture decisions

Design decisions are recorded as ADRs, each framed as *"X over Y"* with the tradeoff stated
explicitly. The ones that shape the system most:

- **A simplified signed ledger** over full normal-balance accounting — every entry's lines sum to
  zero, and `account_type` is recorded without yet driving debit/credit signs.
- **Balances derived on read** over a denormalized balance column, which trades a cheap read for
  an invalidation problem the ledger is too young to need.
- **Transfers as journal entries** over a dedicated transfer table, so there is only one
  definition of whether a transfer happened.
- **Deterministic lock ordering** over relying on deadlock detection and retries, which turns a
  correctness property into a latency problem that is far harder to test.
- **Token sessions** over stock rotate-and-blacklist, making access tokens revocable and refresh
  reuse detectable.
- **An append-only audit log enforced by the database** over trusting application code not to
  rewrite history.
- **Position accounts inside the ledger** over a separate holdings table, so a holding's share count
  and cost basis are both derived and cannot disagree — at the cost, stated plainly, of share
  quantities sitting outside the zero-sum invariant.
- **Synthetic prices behind a source interface** over a live market-data feed, so the simulation is
  one class deep and a real feed is one settings string away.
- **Portfolio valuation derived on read** over stored positions and a P&L column, refusing a second
  source of truth for facts the ledger already holds.
- **Real-time events published on commit** over publishing at the call site — the mirror image of
  the audit log's rule, and for the same reason: neither may claim a movement that never happened.
- **First-message WebSocket authentication** over a token in the query string, so a bearer
  credential is never written to an access log — with the socket's lifetime bounded by its token's
  expiry and by its session's revocation.
- **UUIDv7 primary keys**, **single-currency USD** with the currency field already in place, and a
  **versioned API** with one error envelope and cursor pagination.
