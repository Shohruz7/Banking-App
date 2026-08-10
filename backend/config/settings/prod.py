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

# Env-driven with a secure default, for the same reason `DB_SSLMODE` above is: the deploy differs
# from CI in exactly one way here, and that difference should be a variable rather than a lie told
# in nginx. Left at True, a stack served over plain HTTP answers every request with a 301 to a
# scheme it does not have — including the readiness probe, which then never reports healthy.
#
# The alternative was to have the CI proxy send `X-Forwarded-Proto: https` over a plaintext
# connection. That works, and it means the smoke test exercises a header the real deployment never
# produces, which is the opposite of what a smoke test is for.
#
# **Only ever False where there is no certificate**: the CI stack and a laptop. On the box this
# stays True, and `deploy/.env.example` does not offer it as a knob.
SECURE_SSL_REDIRECT = env.bool("SECURE_SSL_REDIRECT", default=True)
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_HSTS_SECONDS = 60 * 60 * 24 * 30
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
# HSTS was set but not preload-eligible, which meant the *first* request a browser ever made to the
# domain was still plaintext. Preload closes that window at the cost of a commitment: a domain on
# the preload list is HTTPS-only in shipped browsers, and removal takes months.
SECURE_HSTS_PRELOAD = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True

# The SPA authenticates with bearer tokens and needs no CSRF, so this is not for the client at all
# — it is for **Django admin**, whose login is session auth and which answers 403 behind a proxy
# without it. The values are the deploy's business; see deploy/.env.example.
CSRF_TRUSTED_ORIGINS = env.list("CSRF_TRUSTED_ORIGINS", default=[])

# Hashed, manifest-named static files, so a deploy cannot serve last release's admin CSS from a
# cache. Requires STATIC_ROOT and a collectstatic step, both of which now exist — the image runs
# `collectstatic` at build, which is what makes the manifest and the code that reads it atomic.
#
# Whitenoise's subclass of the same storage, so nothing about the manifest guarantee changes; it
# adds the gzip/brotli variants alongside each hashed file at collect time so the middleware can
# serve a pre-compressed body instead of compressing on every request.
STORAGES["staticfiles"] = {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"}

# ------------------------------------------------------------------------------------------------
# One trusted proxy hop (ADR-0038)
# ------------------------------------------------------------------------------------------------
#
# **Throttling is broken behind a reverse proxy until this is set**, in one of two ways, and neither
# announces itself. DRF's `BaseThrottle.get_ident` with `NUM_PROXIES = None` does:
#
#     return ''.join(xff.split()) if xff else remote_addr
#
# With no `X-Forwarded-For`, every request carries nginx's container address and the entire internet
# shares one 60/min bucket. With one, the throttle key becomes the *whole header* — which the client
# populates the left of — so an attacker gets a fresh bucket per request by varying a string. The
# rate limits on login, registration, MFA, refresh and transfers are exactly the ones that stop
# being real.
#
# `NUM_PROXIES = 1` makes DRF take `addrs[-1]`: the address nginx itself appended, which the client
# cannot forge. It must match the number of proxies actually in front of the app — add a CDN or a
# load balancer and this number changes with it.
#
# Note the deliberate asymmetry with `audit.middleware.client_ip`, which takes the *leftmost* entry.
# That is what the client claims about itself: evidence for a log, never an access-control input.
# This takes the rightmost: what nginx observed. Two readers, two answers, both correct.
REST_FRAMEWORK = {**REST_FRAMEWORK, "NUM_PROXIES": 1}

# Bound what an unauthenticated request can make the server allocate. Django's defaults suit a site
# that accepts uploads; this API takes small JSON bodies and serves one statement download.
DATA_UPLOAD_MAX_MEMORY_SIZE = 2 * 1024 * 1024
DATA_UPLOAD_MAX_NUMBER_FIELDS = 200
