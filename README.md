# Personal Banking Platform

A full-stack personal banking and brokerage platform built around a **double-entry ledger whose
balance invariant is enforced by the database itself**. Every money movement — transfers, trades,
fees — is a balanced, atomic, idempotent journal entry.

> Status: Week 2 — ledger core. Accounts and journal entries post through a single balanced,
> atomic primitive; the zero-sum invariant is enforced at COMMIT by a deferred Postgres
> constraint trigger, so the ledger cannot go unbalanced even via raw SQL. Balances are derived,
> money is exact `Decimal` (ADR-0009), and the suite proves each invariant. Architecture
> decisions live as ADRs in [docs/adr](docs/adr/).

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
backend/    Django project (config/, accounts/, ledger/, tests/)
frontend/   React SPA (placeholder until Week 7)
docs/adr/   Architecture Decision Records
docs/       ER diagram and design artifacts
```

## Architecture decisions

Key decisions are recorded as short ADRs — see the [index](docs/adr/README.md).
Highlights: monorepo layout, UUIDv7 primary keys, versioned API with a single error
envelope and cursor pagination, single-currency USD (multi-currency deferred by design),
and a simplified signed ledger where every journal entry's lines sum to zero.
