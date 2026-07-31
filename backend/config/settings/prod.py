"""Production settings — selected explicitly via DJANGO_SETTINGS_MODULE.

Refuses to boot without a real secret key; no insecure fallbacks here.
"""

from .base import *

DEBUG = False

SECRET_KEY = env("DJANGO_SECRET_KEY")

ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS")

# No insecure fallback: a per-process cache silently multiplies every rate limit by the worker
# count (ADR-0015), so prod refuses to boot without a shared one.
CACHES = {"default": env.cache_url("CACHE_URL")}

SECURE_SSL_REDIRECT = True
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_HSTS_SECONDS = 60 * 60 * 24 * 30
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
