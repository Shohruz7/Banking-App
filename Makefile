# One verb per thing you actually do. Everything here is a thin wrapper — the real definitions live
# in deploy/compose.yml and the scripts beside it, and nothing in this file hides a decision.
#
# The local dev loop is deliberately *not* here: Django runs on the host against the root
# docker-compose.yml (Postgres + Redis only), because autoreload and an attachable debugger beat a
# stack that rebuilds an image per edit. `make up` is the production shape, for proving it works.

COMPOSE   := docker compose -f deploy/compose.yml -f deploy/compose.ci.yml --env-file deploy/.env.ci
IMAGE_TAG ?= local
#: History depths `make growth` seeds and measures. Override for a quicker pass:
#:     make growth DEPTHS="100 1000"
DEPTHS    ?= 100 1000 10000 100000

.DEFAULT_GOAL := help
.PHONY: help images up down logs ps shell migrate seed smoke load growth test lint

help:  ## Show this help
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | awk -F':.*?## ' '{printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

images:  ## Build both images
	docker build -f backend/Dockerfile -t banking-backend:$(IMAGE_TAG) .
	docker build -f frontend/Dockerfile -t banking-web:$(IMAGE_TAG) .

up: images  ## Bring the full stack up on http://localhost:8080
	@test -f deploy/.env.ci || { echo "deploy/.env.ci is missing — copy deploy/.env.example and fill it in"; exit 1; }
	@# --remove-orphans so a service that has been renamed or dropped does not leave a container
	@# behind holding the network open. deploy.sh does the same, for the same reason.
	$(COMPOSE) up -d --wait --wait-timeout 180 --remove-orphans
	@echo "→ http://localhost:8080"

down:  ## Stop the stack and delete its volumes
	$(COMPOSE) down -v

logs:  ## Follow every container's logs
	$(COMPOSE) logs -f --tail 100

ps:  ## What is running
	$(COMPOSE) ps

shell:  ## A Django shell in the app container
	$(COMPOSE) exec app_blue python manage.py shell

migrate:  ## Apply migrations
	$(COMPOSE) exec app_blue python manage.py migrate

seed:  ## Seed the market and the demo dataset
	$(COMPOSE) exec app_blue python manage.py seed_instruments --ticks 180 --seed 1
	$(COMPOSE) exec app_blue python manage.py seed_demo --seed 1

smoke:  ## Prove a worker-published tick reaches a socket held by the app
	$(COMPOSE) exec -T app_blue python manage.py shell --no-imports \
	  -c "from markets.models import Instrument; print(Instrument.objects.filter(is_active=True).first().symbol)" \
	  | tr -d '\r' > /tmp/banking-symbol
	@python deploy/smoke_socket.py --base http://localhost:8080 \
	  --username demo --password demo-password-1234 \
	  --symbol "$$(cat /tmp/banking-symbol)" & \
	  sleep 12; \
	  $(COMPOSE) exec -T worker celery -A config call markets.advance_prices >/dev/null; \
	  wait

load:  ## Load-test the running stack through nginx, inside its own rate limits
	@test -f deploy/loadtest/api.js || { echo "deploy/loadtest/api.js is missing"; exit 1; }
	$(COMPOSE) exec -T app_blue python manage.py shell --no-imports \
	  < deploy/loadtest/mint_tokens.py > deploy/loadtest/tokens.json
	@# `--network banking_default` because compose.yml names the project `banking`. Reaching `web`
	@# by service name rather than the published port keeps Docker Desktop's userland port forward —
	@# a laptop artifact, not a property of the system — out of the measurement.
	@# RUN_ID keeps this run's idempotency keys distinct from every previous run's against the same
	@# database. Reusing one for a different transfer is a 409, correctly (ADR-0024).
	docker run --rm -i --network banking_default \
	  -v "$(PWD)/deploy/loadtest:/loadtest" \
	  -e RUN_ID="$$(date +%s)" \
	  grafana/k6 run /loadtest/api.js
	@# The half of the claim that makes it a *banking* latency number: the ledger still balances.
	$(COMPOSE) exec -T app_blue python manage.py check_ledger_invariants

growth:  ## Measure how a derived balance scales with an account's history depth
	@# The question ADR-0008 is most often asked to defend, and the one `make load` cannot answer:
	@# it measures at one data size and says so. This varies history length with everything else
	@# held still. Seeding is quadratic on purpose -- `transfer` derives the source balance to check
	@# the overdraft, so the 100,000th transfer sums 99,999 lines first -- which makes the seed rate
	@# it reports a result rather than overhead.
	@#
	@# **Run this on a stack you are willing to throw away.** The customers it creates cannot be
	@# deleted: AuditEvent is append-only by trigger and AuditEvent.actor is PROTECT. `make down`
	@# afterwards, and see deploy/README.md on re-seeding.
	$(COMPOSE) exec -T app_blue python manage.py bench_derived_balance \
	  --depths $(DEPTHS) --explain
	$(COMPOSE) exec -T app_blue python manage.py shell --no-imports \
	  < deploy/loadtest/mint_bench_tokens.py > deploy/loadtest/bench_tokens.json
	@# Same container-on-the-compose-network shape as `make load`, and for the same reason: reaching
	@# `web` by service name keeps Docker Desktop's userland port forward out of the measurement, so
	@# the two sets of numbers are comparable.
	docker run --rm -i --network banking_default \
	  -v "$(PWD)/deploy/loadtest:/loadtest" \
	  python:3.12-alpine python /loadtest/growth_curve.py \
	  --base http://web --tokens /loadtest/bench_tokens.json

test:  ## Both suites
	cd backend && uv run pytest -q
	cd frontend && npm test

lint:  ## Both linters and both typecheckers
	cd backend && uv run ruff check . && uv run ruff format --check . && uv run mypy .
	cd frontend && npx eslint . && npx tsc --noEmit
