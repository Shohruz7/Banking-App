Week 1 — Foundations, tooling, and the decisions you don't want to redo
Goal: an empty-but-correct skeleton that boots, has a green test harness, passes CI, and a set of written decisions. Almost no feature code — this week is scaffolding and ADRs.
Decisions to lock (write each as a short ADR in /docs/adr — same format you liked in the design doc):

Repo layout — monorepo with backend/ and frontend/. One place to show the whole system; simpler CI. (Two repos only buys you independent deploys you don't need.)
Toolchain — uv for env/deps + lockfile, ruff for lint and format (drops black/isort/flake8), mypy for typing. Coherent modern Astral stack and a small resume signal. Poetry is the fine alternative if you'd rather.
Versions — Python 3.12, Django 5.x, DRF latest. Pin them.
Settings split — base/dev/prod modules, secrets via env (django-environ, or pydantic-settings if you want typed config). Nothing sensitive in git, ever.
Primary keys — UUID PKs on externally-exposed resources (accounts, transactions) so IDs aren't enumerable. Use UUIDv7 (via a lib like uuid6) for index locality; uuid4 is fine if you'd rather not add a dep.
API conventions — version under /api/v1/, a single consistent error envelope, cursor or page-number pagination chosen now. Decide once so every endpoint matches.
Currency scope — single-currency USD for v1, but put a currency field (default USD) on accounts/lines so multi-currency is a later addition, not a migration nightmare. Multi-currency double-entry (FX legs) is a genuine rabbit hole; consciously defer it.
The big modeling call: accounting fidelity — do you model account types (asset/liability/equity/income/expense) with proper normal-balance sign conventions, or a simplified signed ledger? My rec: simplified signed ledger for v1 — the invariant is "an entry's lines sum to zero," balance = sum of signed lines — but keep an account_type field so you can layer real normal-balance semantics later. Worth knowing for interviews: a customer's deposit is actually a liability of the bank (the bank owes them), so a "real" ledger signs by account type. You can speak to that even while v1 keeps it simple. This is exactly the kind of nuance a payments interviewer probes.

Setup & scaffolding:

Init repo: license, .gitignore, .editorconfig, README skeleton, pre-commit wired to ruff + mypy.
docker-compose.yml with just Postgres + Redis for now. Run Django locally against them — fast dev loop. Full app containerization is a Week 8 task, not now.
Scaffold the Django project + first two apps (accounts, ledger). Wire DRF. Install djangorestframework-simplejwt and stub the token endpoints (no real auth yet).
Test harness: pytest + pytest-django + factory_boy + pytest-cov. Write one trivial passing test to prove it runs.
CI from day one: a GitHub Actions workflow that runs ruff + mypy + pytest on every push, plus branch protection requiring the check to pass. Cheap now, and "CI green since commit 3" is a real signal.
Draft the ER diagram (even hand-sketched) for the ledger tables you'll build in Week 2.

Done when: repo boots, Django runs against dockerized Postgres/Redis, DRF + JWT installed, one test passes, CI is green on a near-empty project, and ~8 ADRs are written.
