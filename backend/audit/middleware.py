"""Publish each request's actor and transport facts into the audit context.

Middleware does not *write* audit rows (ADR-0014 explains why); it only makes the ambient facts
available so that code far below the view — ``ledger.services.transfer`` — can record an accurate
row without being handed a request object.

Must sit last in ``MIDDLEWARE`` so ``AuthenticationMiddleware`` has already resolved
``request.user``. Note that DRF authenticates lazily inside the view, so for JWT requests the user
here is still anonymous; views and services pass ``actor=`` explicitly, and this fills in the rest.
"""

from collections.abc import Callable
from uuid import uuid4

from django.contrib.auth.models import User
from django.http import HttpRequest, HttpResponse

from .context import audit_context


def client_ip(request: HttpRequest) -> str | None:
    """Best-effort client IP.

    ``X-Forwarded-For`` is only consulted because Week 8 puts nginx in front; the leftmost entry is
    the original client. Behind a proxy that does not strip a client-supplied header this value is
    spoofable — it is evidence in a log, never an access-control input.
    """
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    # A header that is present but blank falls through to REMOTE_ADDR rather than yielding None:
    # a proxy that sets an empty header must not cost us the address we already have.
    client = forwarded.split(",")[0].strip()
    return client or request.META.get("REMOTE_ADDR") or None


class AuditContextMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        user = getattr(request, "user", None)
        # AnonymousUser is not a User subclass, so this single check covers both.
        actor = user if isinstance(user, User) else None

        with audit_context(
            actor=actor,
            ip=client_ip(request),
            user_agent=request.META.get("HTTP_USER_AGENT", "")[:255],
            request_id=request.headers.get("X-Request-ID") or str(uuid4()),
        ):
            return self.get_response(request)
