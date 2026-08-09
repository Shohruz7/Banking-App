# syntax=docker/dockerfile:1.7
#
# The backend image — one image, four commands (ADR-0033).
#
# `app`, `worker`, `beat` and the one-shot `migrate` all run this. They execute the same code and
# must therefore have the same dependency set: separate images would let the web process and the
# Celery worker drift apart in exactly the way `CELERY_TASK_ALWAYS_EAGER` in `config/settings/test.py`
# already hides from the test suite, so a task would import cleanly in CI and fail on the box.
#
# Build from the repo root:
#     docker build -f backend/Dockerfile -t banking-backend .

# ------------------------------------------------------------------------------------------------
# Stage 1 — builder
# ------------------------------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS builder

# A pinned uv tag, not `:latest`. ADR-0002 and ADR-0003 pin the toolchain and the dependencies
# precisely so a build is reproducible; pulling an unpinned builder would reintroduce the drift
# those decisions exist to remove, in the one place nobody thinks to look.
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /bin/uv

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Dependencies before source, so editing a view does not reinstall Django. `--no-install-project`
# is what makes the split work: it resolves and installs the lockfile without needing the code.
COPY backend/pyproject.toml backend/uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project

COPY backend/ ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev

# `collectstatic` at build time, under prod settings.
#
# The placeholder credentials below are the same move `.github/workflows/ci.yml` already makes to
# run `check --deploy`, and they are safe for the same reason: `prod.py` refuses to import without
# a secret key and a keyring, `collectstatic` only reads static files and writes hashed copies of
# them, and nothing it produces depends on these values. None of them reaches the runtime stage —
# that stage starts from a fresh base and copies only `/app`.
#
# Doing it here rather than at container start is what makes the manifest and the code atomic:
# `CompressedManifestStaticFilesStorage` means a template's asset URL is resolved through a manifest
# built alongside it, and a start-time collect would let a restarted container regenerate one
# against code it does not match.
RUN DJANGO_SETTINGS_MODULE=config.settings.prod \
    DJANGO_SECRET_KEY=build-time-only-never-signs-anything \
    DJANGO_ALLOWED_HOSTS=build.invalid \
    CACHE_URL=locmemcache:// \
    FIELD_ENCRYPTION_KEYS=build:cGxhY2Vob2xkZXIta2V5LTMyLWJ5dGVzLWxvbmchIQ== \
    .venv/bin/python manage.py collectstatic --noinput --clear

# ------------------------------------------------------------------------------------------------
# Stage 2 — runtime
# ------------------------------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime

# No build toolchain, no uv, no caches. `psycopg[binary]` ships its own libpq and ReportLab is pure
# Python, so this image needs no system packages at all beyond the base — which is the payoff for
# the dependency choices made in Weeks 5 and 6.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH" \
    DJANGO_SETTINGS_MODULE=config.settings.prod

# A fixed non-root uid, so a bind-mounted media volume has predictable ownership across hosts.
RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --no-create-home --home-dir /app app

WORKDIR /app
COPY --from=builder --chown=app:app /app /app

# Celery Beat writes its schedule file and the statements task writes PDFs. Both are volumes in
# compose; creating them here means the container starts cleanly even without one attached.
RUN mkdir -p /app/media /var/run/celery && chown -R app:app /app/media /var/run/celery

USER app
EXPOSE 8000

# The readiness probe, not the liveness one: this reports healthy when Postgres and the cache are
# actually reachable, which is what `depends_on: service_healthy` should wait for.
#
# Two details that each cost an hour if discovered by watching a container crash-loop. `prod.py`
# sets `SECURE_SSL_REDIRECT`, so a plain HTTP probe gets a 301 and the container never reports
# healthy — hence the forwarded-proto header, which is exactly what nginx sends and what
# `SECURE_PROXY_SSL_HEADER` reads. And this is Python rather than curl because the image has no
# curl and adding one to run a health check is a package to patch forever.
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=5 \
    CMD ["python", "-c", "import urllib.request as r; \
req = r.Request('http://127.0.0.1:8000/api/v1/ready/', headers={'X-Forwarded-Proto': 'https'}); \
exit(0 if r.urlopen(req, timeout=4).status == 200 else 1)"]

# Overridden by the worker, beat and migrate services. See ADR-0034 for why gunicorn supervises
# uvicorn workers rather than daphne serving directly, and why HTTP and WebSocket share one pool.
CMD ["gunicorn", "config.asgi:application", \
     "--worker-class", "uvicorn_worker.UvicornWorker", \
     "--workers", "2", \
     "--bind", "0.0.0.0:8000", \
     "--access-logfile", "-", \
     "--error-logfile", "-", \
     # Recycling bounds the damage from a slow leak. The jitter matters: without it every worker
     # recycles on the same request count and drops its WebSocket connections simultaneously, and
     # the client's reconnect backoff turns a staggered blip into a thundering herd.
     "--max-requests", "1000", \
     "--max-requests-jitter", "100", \
     # Under an async worker this is a heartbeat between the arbiter and the child, not a request
     # deadline — a long-lived WebSocket is not a stuck worker, and treating it as one would kill
     # every open socket every 30 seconds.
     "--timeout", "120", \
     "--graceful-timeout", "30"]
