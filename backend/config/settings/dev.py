"""Local development settings — the default for manage.py."""

from .base import *

DEBUG = True

SECRET_KEY = env("DJANGO_SECRET_KEY", default="dev-only-insecure-secret-key")

ALLOWED_HOSTS = ["localhost", "127.0.0.1"]

# A fixed, published key so `manage.py` works out of the box and a dev database survives a restart.
# Insecure by construction and that is the point: nothing encrypted with it is protected, which is
# the correct expectation for a development database. prod.py has no default at all.
FIELD_ENCRYPTION_KEYS = FIELD_ENCRYPTION_KEYS or {
    "dev": "ZGV2LW9ubHktaW5zZWN1cmUta2V5LTMyLWJ5dGVzISE="
}
