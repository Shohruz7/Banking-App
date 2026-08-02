"""Import the Celery app on package load so every entry point shares one instance (ADR-0019)."""

from .celery import app as celery_app

__all__ = ("celery_app",)
