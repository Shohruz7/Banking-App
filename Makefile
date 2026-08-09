# One verb per thing you actually do. Everything here is a thin wrapper — the real definitions live
# in deploy/compose.yml and the scripts beside it, and nothing in this file hides a decision.
#
# The local dev loop is deliberately *not* here: Django runs on the host against the root
# docker-compose.yml (Postgres + Redis only), because autoreload and an attachable debugger beat a
# stack that rebuilds an image per edit. `make up` is the production shape, for proving it works.

COMPOSE   := docker compose -f deploy/compose.yml -f deploy/compose.ci.yml --env-file deploy/.env.ci
IMAGE_TAG ?= local

.DEFAULT_GOAL := help
.PHONY: help images up down logs ps shell migrate seed smoke test lint

help:  ## Show this help
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | awk -F':.*?## ' '{printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

images:  ## Build both images
	docker build -f backend/Dockerfile -t banking-backend:$(IMAGE_TAG) .
	docker build -f frontend/Dockerfile -t banking-web:$(IMAGE_TAG) .

up: images  ## Bring the full stack up on http://localhost:8080
	@test -f deploy/.env.ci || { echo "deploy/.env.ci is missing — copy deploy/.env.example and fill it in"; exit 1; }
	$(COMPOSE) up -d --wait --wait-timeout 180
	@echo "→ http://localhost:8080"

down:  ## Stop the stack and delete its volumes
	$(COMPOSE) down -v

logs:  ## Follow every container's logs
	$(COMPOSE) logs -f --tail 100

ps:  ## What is running
	$(COMPOSE) ps

shell:  ## A Django shell in the app container
	$(COMPOSE) exec app python manage.py shell

migrate:  ## Apply migrations
	$(COMPOSE) exec app python manage.py migrate

seed:  ## Seed the market and the demo dataset
	$(COMPOSE) exec app python manage.py seed_instruments --ticks 180 --seed 1
	$(COMPOSE) exec app python manage.py seed_demo --seed 1

smoke:  ## Prove a worker-published tick reaches a socket held by the app
	$(COMPOSE) exec -T app python manage.py shell --no-imports \
	  -c "from markets.models import Instrument; print(Instrument.objects.filter(is_active=True).first().symbol)" \
	  | tr -d '\r' > /tmp/banking-symbol
	@python deploy/smoke_socket.py --base http://localhost:8080 \
	  --username demo --password demo-password-1234 \
	  --symbol "$$(cat /tmp/banking-symbol)" & \
	  sleep 12; \
	  $(COMPOSE) exec -T worker celery -A config call markets.advance_prices >/dev/null; \
	  wait

test:  ## Both suites
	cd backend && uv run pytest -q
	cd frontend && npm test

lint:  ## Both linters and both typecheckers
	cd backend && uv run ruff check . && uv run ruff format --check . && uv run mypy .
	cd frontend && npx eslint . && npx tsc --noEmit
