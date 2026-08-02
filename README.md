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
lines, derived on read. Every entry's lines must sum to exactly zero, and that invariant is
enforced at `COMMIT` by a deferred Postgres constraint trigger — so the ledger cannot go
unbalanced even if something writes to it via raw SQL. Money is `NUMERIC(20,4)`, always `Decimal`,
rounded half-even in exactly one place.

**Transfers.** A transfer is just a two-line journal entry, so it inherits every ledger guarantee.
On top of that it locks both account rows in a fixed order — ascending UUID, regardless of
direction — so two opposing transfers queue instead of deadlocking, and it reads the source
balance *under that lock*, so a race cannot overdraw an account. A request carrying an idempotency
key replays instead of double-posting: the retry returns the original entry rather than an error.

**Authentication.** Registration, then login that issues a short-lived access token and a rotating
refresh token. TOTP MFA is enforced at login through a two-step challenge, with QR provisioning at
enrolment and each code burned after use so it cannot be replayed. Every token is bound to a
revocable session via a `sid` claim, which is what makes an *access* token revocable — a blacklist
of refresh tokens structurally cannot do that. Replaying a rotated refresh token revokes the
entire token family, so the successor an attacker holds dies along with the one they replayed.

**Brokerage.** 55 seeded instruments whose prices walk under geometric Brownian motion, advanced on
a schedule by Celery Beat. Market orders fill inside the request that places them; limit orders rest
until a price tick crosses them and then fill unattended. **A fill is an ordinary journal entry** —
a buy debits cash and credits a position account at cost. A sell is three lines, and the third is
not optional: a position account's balance *is* its cost basis, so a sell removes cost rather than
proceeds, and the residual has to land in a realized-P&L account for the entry to balance. Nothing
about a holding is stored — its share count and its cost basis are both sums over the ledger, so
they cannot drift from the postings that produced them. You cannot buy with money you don't have or
sell shares you don't own, by the same lock-then-read mechanism as the overdraft check.

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
| Async   | Celery + Beat (scheduled price ticks, limit-order matching)  |
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

Beat advances every instrument once a minute (`MARKET_TICK_SECONDS`) and sweeps resting limit
orders after each tick. Without them the API still works; prices simply stand still.

## API

All endpoints live under `/api/v1/`. Everything is authenticated by default; the exceptions opt out
explicitly.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/health/` | Liveness probe (public, never throttled) |
| POST | `/auth/register/` | Create an account |
| POST | `/auth/token/` | Log in — returns a token pair, or an MFA challenge |
| POST | `/auth/token/mfa/` | Complete the MFA challenge with a TOTP code |
| POST | `/auth/token/refresh/` | Rotate the refresh token |
| POST | `/auth/logout/` | Blacklist the refresh token and revoke the session |
| GET | `/auth/me/` | The requesting user's profile |
| POST | `/auth/mfa/enroll/` | Start TOTP enrolment — returns the secret, URI and QR |
| POST | `/auth/mfa/confirm/` | Activate the authenticator |
| POST | `/auth/mfa/disable/` | Turn MFA off and revoke every session |
| GET | `/accounts/`, `/accounts/{id}/` | Owned cash accounts with derived balances |
| GET | `/accounts/{id}/transactions/` | Journal lines, newest first, cursor-paginated |
| POST | `/transfers/` | Move money — 201 posted, 200 on idempotent replay |
| GET | `/instruments/`, `/instruments/{symbol}/` | Tradeable instruments with their latest price |
| GET | `/instruments/{symbol}/prices/` | Tick history, newest first, cursor-paginated |
| POST | `/orders/` | Place an order — 201 `filled` (market) or 201 `open` (limit) |
| GET | `/orders/`, `/orders/{id}/` | Own orders and their outcomes |
| POST | `/orders/{id}/cancel/` | Withdraw a resting order — 409 if it already resolved |
| GET | `/holdings/` | Positions: quantity, cost basis, average cost, market value |

Errors share one envelope: `{"error": {"code": ..., "message": ..., "details": {...}}}`.
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

The concurrency, security and rounding tests were each checked against a deliberately broken
implementation first, to confirm they actually fail when the property they claim to protect is
removed. One test was rewritten after that check showed it passed against the broken version too.

```sh
cd backend
uv run pytest               # test suite
uv run ruff check .         # lint
uv run ruff format .        # format
uv run mypy .               # type check
uvx pre-commit install      # git hooks (ruff + mypy on every commit)
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
  trading/    orders, the fill service, the limit-order matching sweep
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
- **UUIDv7 primary keys**, **single-currency USD** with the currency field already in place, and a
  **versioned API** with one error envelope and cursor pagination.
