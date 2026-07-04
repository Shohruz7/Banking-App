"""Local development settings — the default for manage.py."""

from .base import *

DEBUG = True

SECRET_KEY = env("DJANGO_SECRET_KEY", default="dev-only-insecure-secret-key")

ALLOWED_HOSTS = ["localhost", "127.0.0.1"]
