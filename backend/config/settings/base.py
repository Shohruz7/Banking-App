"""Shared settings for every environment.

Environment-specific modules (dev.py, prod.py) import * from here and override.
All configuration comes from the environment (ADR-0004); a local .env file is
read if present but is never committed.
"""

from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import environ
from celery.schedules import crontab

BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env()
environ.Env.read_env(BASE_DIR / ".env")

ALLOWED_HOSTS: list[str] = []

INSTALLED_APPS = [
    # First, and it has to be first: daphne's app config replaces runserver's WSGI handler with an
    # ASGI one, so the WebSocket endpoint is reachable in dev without a second process (ADR-0022).
    "daphne",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Third-party
    "rest_framework",
    "drf_spectacular",
    "rest_framework_simplejwt",
    # Must be installed unconditionally: BlacklistMixin's methods are gated on INSTALLED_APPS at
    # class-definition time, so a conditional install makes refresh.blacklist() a silent no-op.
    "rest_framework_simplejwt.token_blacklist",
    "channels",
    # First-party
    "accounts",
    "ledger",
    "identity",
    "audit",
    "markets",
    "trading",
    "statements",
    "realtime",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # Immediately after SecurityMiddleware, which is where its docs put it and where the ordering
    # actually matters: it must sit inside the security headers and ahead of everything that would
    # otherwise do session and auth work for a request that is only ever going to be a CSS file.
    #
    # Static here is admin, DRF's browsable API and Swagger — no customer surface — so serving it
    # from the image that hashed it costs one Python hop and saves a cross-image COPY plus a shared
    # writable volume that can hold the previous release's bytes (ADR-0035).
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    # Last: needs request.user resolved. Publishes ambient context for audit rows (ADR-0014).
    "audit.middleware.AuditContextMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

DATABASES = {
    "default": env.db("DATABASE_URL", default="postgres://banking:banking@localhost:5432/banking"),
}
# Reuse connections rather than opening one per request. Postgres connection setup is not free and
# every request in this app makes several queries; 0 (the default) pays that cost every time.
# CONN_HEALTH_CHECKS is what makes reuse safe: without it a connection killed by a restart or a
# failover is handed to a request that then fails, once, for no reason it can act on.
DATABASES["default"]["CONN_MAX_AGE"] = env.int("CONN_MAX_AGE", default=60)
DATABASES["default"]["CONN_HEALTH_CHECKS"] = True

# Three Redis clients, three logical databases. They shared DB 0 until Week 7, which meant a
# `celery purge` or a stray FLUSHDB during an incident would also drop every live WebSocket's group
# membership and every throttle counter — three unrelated blast radii welded together.
REDIS_URL = env("REDIS_URL", default="redis://localhost:6379/0")


def _redis_db(url: str, index: int) -> str:
    """The same Redis server, a different logical database.

    Rewrites only the path, via a real URL parse. String surgery gets this wrong in a way that is
    quiet until it is not: `rstrip("/0123456789")` on `redis://host:6379/0` also eats the port.
    """
    parsed = urlsplit(url)
    return urlunsplit(parsed._replace(path=f"/{index}"))


CELERY_BROKER_URL = env("CELERY_BROKER_URL", default=_redis_db(REDIS_URL, 1))
CHANNEL_LAYER_URL = env("CHANNEL_LAYER_URL", default=_redis_db(REDIS_URL, 2))

# The channel layer (ADR-0022). It must be shared: a fill commits in a web worker and has to reach
# a socket held by a different process, and a price tick is published from Celery entirely — an
# in-process layer would deliver each event only to whichever worker happened to produce it.
#
# **`RedisPubSubChannelLayer`, not `RedisChannelLayer`, and the difference is not academic.** The
# core layer models each channel as a Redis list and reads it with a blocking pop; an idle socket —
# which every socket here is, between price ticks — eventually trips redis-py's read timeout, and
# the `TimeoutError` propagates out of the consumer's receive loop and closes the connection. The
# client reconnects, so nothing looks broken; the socket just quietly dies every minute or so under
# no load at all.
#
# Nothing in the test suite could have caught that: `config/settings/test.py` uses the in-memory
# layer, so the Redis backend had never actually been exercised until Week 9 stood the real stack up
# and watched a socket drop. It was found by `deploy/smoke_socket.py` on its first run.
#
# The pub/sub layer is also simply the right model for this application. Every publisher here calls
# `group_send` (`realtime/events.py`) and every consumer joins groups; nothing sends to a single
# named channel and nothing needs a message to survive having no listener. That is fan-out, which is
# what Redis pub/sub is, and it means no lists, no expiry bookkeeping and no blocking reads.
#
# What is given up, stated plainly: pub/sub has no persistence, so a message published while a
# consumer is briefly disconnected is lost rather than queued. That is already the contract —
# ADR-0023 says a reconnecting client re-reads over HTTP rather than replaying events, precisely
# because at-least-once delivery was never promised.
CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels_redis.pubsub.RedisPubSubChannelLayer",
        "CONFIG": {"hosts": [CHANNEL_LAYER_URL]},
    }
}

# How long an accepted socket may stay anonymous before it is closed (ADR-0022). The client's only
# legal first move is an auth frame; this is the budget for making it.
WS_AUTH_DEADLINE_SECONDS = env.int("WS_AUTH_DEADLINE_SECONDS", default=5)

# Per-socket price-subscription ceiling. A client watching a whole 57-symbol market is a chart
# nobody is reading; the throttles (ADR-0015) cover HTTP, and this is the socket's equivalent.
WS_MAX_SUBSCRIPTIONS = env.int("WS_MAX_SUBSCRIPTIONS", default=25)

# Celery (ADR-0019). Redis is the broker; there is no result backend because nothing waits on a
# return value — Beat fires tasks, tasks write rows, and the database is the result.
CELERY_TIMEZONE = "UTC"
# Surface a task exception instead of swallowing it into a result nobody reads.
CELERY_TASK_EAGER_PROPAGATES = True
# A tick that arrives late is worthless — a price is only current for one interval. Better to drop
# a backlog than to replay a stale market at speed after an outage.
CELERY_BROKER_TRANSPORT_OPTIONS = {"visibility_timeout": 3600}

# A worker killed mid-task should not silently lose the task (ADR-0019). With acks_late the message
# is only acknowledged once the task returns, so a SIGKILL between tick and commit means the tick is
# redelivered rather than dropped. Safe here because every scheduled task is already idempotent —
# `advance_prices` holds a cache mutex, statement generation collides with a unique index, and the
# reconciliation scan writes nothing.
CELERY_TASK_ACKS_LATE = True
# With acks_late, prefetching hoards messages a dying worker would have to give back. One at a time.
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
# Soft first, so a task gets an exception it can log before the hard kill takes the process.
CELERY_TASK_SOFT_TIME_LIMIT = env.int("CELERY_TASK_SOFT_TIME_LIMIT", default=300)
CELERY_TASK_TIME_LIMIT = env.int("CELERY_TASK_TIME_LIMIT", default=360)
# A worker that starts before Redis is reachable should wait, not crash-loop the container.
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
# Recycle workers periodically: the cheapest defence against a slow leak in a long-lived process.
CELERY_WORKER_MAX_TASKS_PER_CHILD = 500

# How often the simulated market moves (ADR-0017). Scales the GBM step, so changing it changes the
# clock the annualized drift and volatility are measured against, not just the row count.
MARKET_TICK_SECONDS = env.int("MARKET_TICK_SECONDS", default=60)

# The price source, resolved by dotted path the way Django resolves its own backends. Swapping in
# a live market-data feed is this one string (ADR-0017).
PRICE_SOURCE = env("PRICE_SOURCE", default="markets.pricing.GBMPriceSource")

# Two entries, in a plain dict. django-celery-beat would buy a database-backed schedule editable
# from the admin, at the cost of a dependency, a migration and a schedule that can drift from the
# code that defines it — not worth it for a fixed pair.
CELERY_BEAT_SCHEDULE = {
    "advance-prices": {
        "task": "markets.advance_prices",
        "schedule": MARKET_TICK_SECONDS,
    },
    # A safety net, not the primary path: advance_prices chains matching after every tick. This
    # catches orders that were resting when a chained dispatch was lost.
    "match-resting-orders": {
        "task": "trading.match_resting_orders",
        "schedule": MARKET_TICK_SECONDS * 5,
    },
    # Statements for the month that just closed (ADR-0021). A quarter past midnight rather than on
    # the hour: nothing else is scheduled there, and a late tick from the previous month has long
    # since landed. The task is idempotent, so a retry after an outage costs nothing.
    "generate-monthly-statements": {
        "task": "statements.generate_monthly",
        "schedule": crontab(day_of_month="1", hour=0, minute=15),
    },
    # Nightly reconciliation (ADR-0026). Three of the ledger's invariants are enforced by Postgres
    # at COMMIT; the no-overdraft rule cannot be, because a deferred trigger cannot see uncommitted
    # transactions. This is the backstop for that one, run over committed data where the question is
    # answerable. 03:20 so it does not share a minute with anything else.
    "check-ledger-invariants": {
        "task": "ledger.check_invariants",
        "schedule": crontab(hour=3, minute=20),
    },
    # Retention for machine-generated market data (ADR-0041). Weekly rather than nightly: it deletes
    # a rolling window, so running it seven times as often deletes a seventh as much each time and
    # buys nothing. 04:10 keeps it clear of the reconciliation pass above.
    "purge-price-ticks": {
        "task": "markets.purge_price_ticks",
        "schedule": crontab(day_of_week="sun", hour=4, minute=10),
    },
    # Promised in Week 4, floated in Week 5, never landed until now: SimpleJWT's OutstandingToken
    # table grows by one row per login forever, and nothing was pruning it. Weekly is ample — the
    # rows are only interesting while a refresh token could still be presented, which is one day.
    "flush-expired-tokens": {
        "task": "identity.flush_expired_tokens",
        "schedule": crontab(day_of_week="sun", hour=4, minute=0),
    },
}

# DRF keeps throttle history here (ADR-0015). The default LocMemCache is per-process, so behind the
# two gunicorn workers in deploy/compose.yml the effective rate becomes N × configured — prod.py
# overrides with no fallback, the same posture SECRET_KEY takes in ADR-0004.
CACHES = {"default": env.cache_url("CACHE_URL", default="locmemcache://")}

# Login accepts an email or a username; the resolution lives in a backend, not in a swapped user
# model (ADR-0011).
AUTHENTICATION_BACKENDS = [
    "identity.backends.EmailOrUsernameBackend",
    "django.contrib.auth.backends.ModelBackend",
]

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
# Without this `collectstatic` cannot run, so a production deploy serves the admin with no CSS and
# no JS. It went unnoticed for six weeks because dev serves static files from the apps directly.
STATIC_ROOT = env("STATIC_ROOT", default=str(BASE_DIR / "staticfiles"))

# Logging (ADR-0028). Django ships a default that sends `django.*` at WARNING to the console and
# leaves every other logger inheriting a root that does nothing — so before this, all eight
# `logger.info` calls in realtime/, statements/, markets/ and trading/ were silently discarded and
# the `logger.exception` calls arrived with no request id attached.
LOGGING = {
    "version": 1,
    # False, deliberately: the default config is replaced rather than layered under, so there is one
    # place that decides where a line goes.
    "disable_existing_loggers": False,
    "filters": {
        "request_id": {"()": "common.logging.RequestIDFilter"},
    },
    "formatters": {
        # Key=value rather than prose: greppable, and the shape a log shipper can parse later
        # without a regex per message.
        "standard": {
            "format": (
                "%(asctime)s %(levelname)-8s %(name)s request_id=%(request_id)s %(message)s"
            ),
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "standard",
            "filters": ["request_id"],
        },
    },
    "root": {"handlers": ["console"], "level": env("LOG_LEVEL", default="INFO")},
    "loggers": {
        # Django's own request logger duplicates what DRF's exception handler already reports for
        # handled 4xx; propagate=False keeps one line per event rather than two.
        "django.request": {"handlers": ["console"], "level": "ERROR", "propagate": False},
        # Query logging is a deliberate opt-in. At DEBUG this logger prints every SQL statement,
        # which in a ledger means printing amounts and account ids into the log.
        "django.db.backends": {"level": "WARNING", "propagate": True},
    },
}

# Generated statement PDFs (ADR-0021). Written through Django's storage API, so the day a second
# app host makes S3 worth its dependencies, that is one entry in `STORAGES` and not a line of
# statement code (ADR-0039).
MEDIA_ROOT = env("MEDIA_ROOT", default=str(BASE_DIR / "media"))
# Deliberately no MEDIA_URL route. The only path to a statement is the owner-scoped download view;
# a file served off a guessable static path would bypass every ownership check in the system.
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Key encryption keys for the envelope-encrypted columns (ADR-0027), newest first: the first entry
# encrypts new writes, and every entry can still decrypt what it wrote. Rotation is "prepend a key,
# deploy, re-save the rows, drop the old one" — no flag day.
#
# Format is `label:urlsafe-b64-32-bytes` pairs, comma-separated, in one variable so a deploy carries
# its whole keyring rather than N variables that can be half-updated. dev.py supplies an insecure
# default; prod.py has none, and refuses to boot without one, the same posture SECRET_KEY takes.
FIELD_ENCRYPTION_KEYS: dict[str, str] = {
    label: key
    for label, _, key in (
        pair.partition(":") for pair in env.list("FIELD_ENCRYPTION_KEYS", default=[])
    )
    if key
}

# What a newly registered customer starts with (ledger/onboarding.py). Credited to Checking from
# their own opening-balances equity account, as an ordinary balanced entry — money never appears.
# `seed_demo` passes its own per-customer amount rather than reading this, which is what keeps it
# off `post_entry` (ADR-0037). Set to 0 to open accounts with no deposit and no equity account.
ONBOARDING_OPENING_DEPOSIT = Decimal(env("ONBOARDING_OPENING_DEPOSIT", default="1000.0000"))

# How long price history is kept (ADR-0041). 400 days rather than 90, and the reason is a coupling
# that is easy to miss: `statements.services.period_end_prices` falls back to an instrument's
# initial price when a period holds no tick, so regenerating a statement whose ticks have been
# purged would produce different numbers than the PDF that was issued. 400 days puts that beyond any
# plausible regeneration window. 0 disables the purge entirely.
PRICE_TICK_RETENTION_DAYS = env.int("PRICE_TICK_RETENTION_DAYS", default=400)

# API conventions locked in ADR-0006: one error envelope, cursor pagination.
REST_FRAMEWORK = {
    # Session-aware, so revoking an AuthSession kills its access tokens too (ADR-0013).
    "DEFAULT_AUTHENTICATION_CLASSES": ("identity.authentication.SessionAwareJWTAuthentication",),
    # Locked shut by default; endpoints that are genuinely public (health, token issuance) opt
    # out explicitly. The safe direction to forget in is "denied".
    "DEFAULT_PERMISSION_CLASSES": ("rest_framework.permissions.IsAuthenticated",),
    "EXCEPTION_HANDLER": "common.exceptions.api_exception_handler",
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_PAGINATION_CLASS": "common.pagination.DefaultCursorPagination",
    "PAGE_SIZE": 20,
    # All three run everywhere (ADR-0015). ScopedRateThrottle short-circuits on views without a
    # throttle_scope, so unscoped views cost nothing — and a future view that forgets its scope
    # still falls back to the anon/user ceiling. The safe direction to forget in is "limited".
    "DEFAULT_THROTTLE_CLASSES": (
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
        "rest_framework.throttling.ScopedRateThrottle",
    ),
    "DEFAULT_THROTTLE_RATES": {
        "anon": "60/min",
        "user": "240/min",
        "register": "5/hour",
        "login": "10/min",
        # 10^6 codes over a ~90s acceptance window at 5 guesses/min is ~300 tries/hour, i.e. a
        # ~0.03% chance per hour of a blind hit. Account lockout is unnecessary at that rate.
        "mfa": "5/min",
        "refresh": "30/min",
        "transfer": "30/min",
        "order": "30/min",
    },
}

# Real auth (ADR-0012, ADR-0013): short-lived access tokens, rotating refresh with the old token
# blacklisted, and every token bound to a revocable session via a `sid` claim.
SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=15),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=1),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "UPDATE_LAST_LOGIN": True,
    # Only AccessToken may authenticate a request. MFAPendingToken is deliberately absent, which
    # is what stops a half-finished login from being usable as a credential.
    "AUTH_TOKEN_CLASSES": ("rest_framework_simplejwt.tokens.AccessToken",),
    "TOKEN_OBTAIN_SERIALIZER": "identity.serializers.TokenObtainPairWithMFASerializer",
    "TOKEN_REFRESH_SERIALIZER": "identity.serializers.SessionRefreshSerializer",
}

# The generated API description (ADR-0028). Replaces a hand-maintained README table that had already
# drifted from the code it described.
SPECTACULAR_SETTINGS = {
    "TITLE": "Banking Platform API",
    "DESCRIPTION": (
        "Double-entry ledger, transfers, brokerage, statements and a real-time stream. "
        "Every monetary value crosses the wire as a decimal string, never a float (ADR-0009); "
        "errors share one envelope (ADR-0006); lists are cursor-paginated."
    ),
    "VERSION": "1.0.0",
    # The schema endpoint serves the document; it does not need to appear inside it.
    "SERVE_INCLUDE_SCHEMA": False,
    # Strip the prefix from every operation id, so `accounts_list` is not `api_v1_accounts_list`.
    "SCHEMA_PATH_PREFIX": "/api/v1",
    # Loaded for its side effect: the module registers the OpenApiAuthenticationExtension for this
    # project's JWT class, without which every authenticated endpoint documents as public.
    "EXTENSIONS_INFO": {},
    "PREPROCESSING_HOOKS": ["common.schema_hooks.register_extensions"],
}
