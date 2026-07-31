# Personal Banking Platform

A full-stack personal banking and brokerage platform built around a **double-entry ledger whose
balance invariant is enforced by the database itself**. Every money movement — transfers, trades,
fees — is a balanced, atomic, idempotent journal entry.

> Status: Week 4 — auth, MFA, audit. Real authentication now: registration, TOTP MFA enforced at
> login via a two-step challenge with QR provisioning, and refresh tokens that rotate. Every token
> is bound to a revocable session, so replaying a rotated refresh token takes down the *whole
> family* — the successor an attacker holds dies with it, which stock rotate-and-blacklist does not
> do — and logging out kills access tokens immediately rather than leaving a 15-minute window.
> Every auth event and every money movement lands in an append-only audit log that Postgres itself
> refuses to let anyone `UPDATE` or `DELETE`. Scoped rate limits sit on the endpoints worth
> attacking.
>
> Underneath it, the Week 2–3 ledger: every movement is a balanced journal entry whose zero-sum
> invariant is enforced at COMMIT by a deferred Postgres constraint trigger, and transfers lock both
> account rows in a fixed order (so opposing transfers queue instead of deadlocking), check the
> balance under that lock (so a race can't overdraw), and replay instead of double-posting when a
> request carries an idempotency key. Architecture decisions live as ADRs in [docs/adr](docs/adr/).

## Stack

| Layer     | Choice                                                        |
| --------- | ------------------------------------------------------------- |
| Backend   | Python 3.12 · Django 5.2 · Django REST Framework · SimpleJWT  |
| Data      | PostgreSQL 16 · Redis 7                                       |
| Tooling   | uv (env + lockfile) · ruff (lint + format) · mypy · pytest    |
| Frontend  | React + TypeScript (Week 7)                                   |

## Quickstart

```sh
# 1. Infrastructure (Postgres + Redis)
docker compose up -d

# 2. Backend
cd backend
cp .env.example .env        # adjust if needed
uv sync                     # installs Python 3.12 + all deps from the lockfile
uv run python manage.py migrate
uv run python manage.py runserver
```

Then: `curl http://localhost:8000/api/v1/health/` → `{"status": "ok"}`

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
| GET | `/accounts/`, `/accounts/{id}/` | Owned accounts with derived balances |
| GET | `/accounts/{id}/transactions/` | Journal lines, newest first, cursor-paginated |
| POST | `/transfers/` | Move money — 201 posted, 200 on idempotent replay |

Errors share one envelope: `{"error": {"code": ..., "message": ..., "details": {...}}}`.
An account owned by someone else returns 404, never 403 — a 403 would confirm it exists.

## Development

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
  config/     settings (base/dev/test/prod), URLs, ASGI/WSGI
  accounts/   ledger accounts and their derived balances
  ledger/     journal entries, the posting primitive, transfers
  identity/   registration, TOTP MFA, revocable auth sessions
  audit/      the append-only audit log
  common/     money, pagination, error envelope, shared helpers
  tests/
frontend/     React SPA (placeholder until Week 7)
docs/adr/     Architecture Decision Records
docs/         ER diagram and design artifacts
```

## Architecture decisions

Key decisions are recorded as short ADRs — see the [index](docs/adr/README.md).
Highlights: monorepo layout, UUIDv7 primary keys, versioned API with a single error
envelope and cursor pagination, single-currency USD (multi-currency deferred by design),
a simplified signed ledger where every journal entry's lines sum to zero, token sessions
that make access tokens revocable and refresh-token reuse detectable, and an audit log
whose immutability is enforced by the database rather than by convention.
