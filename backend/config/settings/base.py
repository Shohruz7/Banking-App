"""Shared settings for every environment.

Environment-specific modules (dev.py, prod.py) import * from here and override.
All configuration comes from the environment (ADR-0004); a local .env file is
read if present but is never committed.
"""

from datetime import timedelta
from pathlib import Path

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

REDIS_URL = env("REDIS_URL", default="redis://localhost:6379/0")

# The channel layer (ADR-0022). It must be shared: a fill commits in a web worker and has to reach
# a socket held by a different process, and a price tick is published from Celery entirely — an
# in-process layer would deliver each event only to whichever worker happened to produce it.
CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels_redis.core.RedisChannelLayer",
        "CONFIG": {"hosts": [REDIS_URL]},
    }
}

# How long an accepted socket may stay anonymous before it is closed (ADR-0022). The client's only
# legal first move is an auth frame; this is the budget for making it.
WS_AUTH_DEADLINE_SECONDS = env.int("WS_AUTH_DEADLINE_SECONDS", default=5)

# Per-socket price-subscription ceiling. A client watching a whole 55-symbol market is a chart
# nobody is reading; the throttles (ADR-0015) cover HTTP, and this is the socket's equivalent.
WS_MAX_SUBSCRIPTIONS = env.int("WS_MAX_SUBSCRIPTIONS", default=25)

# Celery (ADR-0019). Redis is the broker; there is no result backend because nothing waits on a
# return value — Beat fires tasks, tasks write rows, and the database is the result.
CELERY_BROKER_URL = env("CELERY_BROKER_URL", default=REDIS_URL)
CELERY_TIMEZONE = "UTC"
# Surface a task exception instead of swallowing it into a result nobody reads.
CELERY_TASK_EAGER_PROPAGATES = True
# A tick that arrives late is worthless — a price is only current for one interval. Better to drop
# a backlog than to replay a stale market at speed after an outage.
CELERY_BROKER_TRANSPORT_OPTIONS = {"visibility_timeout": 3600}

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
}

# DRF keeps throttle history here (ADR-0015). The default LocMemCache is per-process, so behind
# Week 8's multiple workers the effective rate becomes N × configured — prod.py overrides with no
# fallback, the same posture SECRET_KEY takes in ADR-0004.
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

# Generated statement PDFs (ADR-0021). Written through Django's storage API so Week 8 repoints
# `default` at S3 without touching a line of statement code.
MEDIA_ROOT = env("MEDIA_ROOT", default=str(BASE_DIR / "media"))
# Deliberately no MEDIA_URL route. The only path to a statement is the owner-scoped download view;
# a file served off a guessable static path would bypass every ownership check in the system.
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# API conventions locked in ADR-0006: one error envelope, cursor pagination.
REST_FRAMEWORK = {
    # Session-aware, so revoking an AuthSession kills its access tokens too (ADR-0013).
    "DEFAULT_AUTHENTICATION_CLASSES": ("identity.authentication.SessionAwareJWTAuthentication",),
    # Locked shut by default; endpoints that are genuinely public (health, token issuance) opt
    # out explicitly. The safe direction to forget in is "denied".
    "DEFAULT_PERMISSION_CLASSES": ("rest_framework.permissions.IsAuthenticated",),
    "EXCEPTION_HANDLER": "common.exceptions.api_exception_handler",
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
