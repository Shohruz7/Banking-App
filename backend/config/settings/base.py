"""Shared settings for every environment.

Environment-specific modules (dev.py, prod.py) import * from here and override.
All configuration comes from the environment (ADR-0004); a local .env file is
read if present but is never committed.
"""

from datetime import timedelta
from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env()
environ.Env.read_env(BASE_DIR / ".env")

ALLOWED_HOSTS: list[str] = []

INSTALLED_APPS = [
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
    # First-party
    "accounts",
    "ledger",
    "identity",
    "audit",
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

# Unused until Celery/Channels arrive (Weeks 5–6); defined now so config has one home.
REDIS_URL = env("REDIS_URL", default="redis://localhost:6379/0")

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
