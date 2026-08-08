"""Production settings — selected explicitly via DJANGO_SETTINGS_MODULE.

Refuses to boot without a real secret key; no insecure fallbacks here.
"""

from django.core.exceptions import ImproperlyConfigured

from .base import *

DEBUG = False

SECRET_KEY = env("DJANGO_SECRET_KEY")

ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS")

# No insecure fallback: a per-process cache silently multiplies every rate limit by the worker
# count (ADR-0015), so prod refuses to boot without a shared one.
CACHES = {"default": env.cache_url("CACHE_URL")}

# No default, for the same reason SECRET_KEY has none (ADR-0027): booting with a key an attacker
# can read from the repository is worse than not booting, because the column would *look* encrypted.
if not FIELD_ENCRYPTION_KEYS:
    raise ImproperlyConfigured("FIELD_ENCRYPTION_KEYS must be set: label:base64key[,label:key…]")

# Postgres traffic was unencrypted in transit until Week 7. The database is on another host in any
# real deployment, and everything crossing that wire is balances and PII.
DATABASES["default"].setdefault("OPTIONS", {})["sslmode"] = env("DB_SSLMODE", default="require")

SECURE_SSL_REDIRECT = True
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_HSTS_SECONDS = 60 * 60 * 24 * 30
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
# HSTS was set but not preload-eligible, which meant the *first* request a browser ever made to the
# domain was still plaintext. Preload closes that window at the cost of a commitment: a domain on
# the preload list is HTTPS-only in shipped browsers, and removal takes months.
SECURE_HSTS_PRELOAD = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True

# Needed the moment a browser client posts from another origin (Week 9's SPA). The values are the
# deploy's business; the setting exists now so the deploy is a variable rather than a code change.
CSRF_TRUSTED_ORIGINS = env.list("CSRF_TRUSTED_ORIGINS", default=[])

# Hashed, manifest-named static files, so a deploy cannot serve last release's admin CSS from a
# cache. Requires STATIC_ROOT and a collectstatic step, both of which now exist.
STORAGES["staticfiles"] = {
    "BACKEND": "django.contrib.staticfiles.storage.ManifestStaticFilesStorage"
}

# Bound what an unauthenticated request can make the server allocate. Django's defaults suit a site
# that accepts uploads; this API takes small JSON bodies and serves one statement download.
DATA_UPLOAD_MAX_MEMORY_SIZE = 2 * 1024 * 1024
DATA_UPLOAD_MAX_NUMBER_FIELDS = 200
