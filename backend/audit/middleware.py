"""Publish each request's actor and transport facts into the audit context.

Middleware does not *write* audit rows (ADR-0014 explains why); it only makes the ambient facts
available so that code far below the view — ``ledger.services.transfer`` — can record an accurate
row without being handed a request object.

Must sit last in ``MIDDLEWARE`` so ``AuthenticationMiddleware`` has already resolved
``request.user``. Note that DRF authenticates lazily inside the view, so for JWT requests the user
here is still anonymous; views and services pass ``actor=`` explicitly, and this fills in the rest.
"""

import ipaddress
from collections.abc import Callable
from uuid import uuid4

from django.contrib.auth.models import User
from django.http import HttpRequest, HttpResponse

from .context import audit_context


def _well_formed(candidate: str) -> str | None:
    """Return ``candidate`` if it is an IP address, else ``None``.

    **This is the difference between a spoofable value and a crash.** ``AuditEvent.ip`` is a
    ``GenericIPAddressField``, so anything that is not an address raises ``ValueError`` when the row
    is written — after the work is done, from inside middleware, as a 500 with no useful body. Since
    the leftmost ``X-Forwarded-For`` entry is whatever the caller typed, that made
    ``X-Forwarded-For: not-an-ip`` a one-header 500 on every audited endpoint, including the two
    that take no credentials at all: login and registration.

    Spoofable was always the documented bargain and it is a fair one for evidence. Unvalidated was
    not part of it: a value that cannot be stored is not evidence, it is an outage.
    """
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        return None
    return candidate


def client_ip(request: HttpRequest) -> str | None:
    """Best-effort client IP.

    ``X-Forwarded-For`` is only consulted because nginx is in front (ADR-0038); the leftmost
    entry is the original client. Behind a proxy that does not strip a client-supplied header
    this value is spoofable — it is evidence in a log, never an access-control input.

    Note the deliberate contrast with throttling, which takes the *rightmost* entry (ADR-0038):
    what nginx observed, because that one is an access-control input.

    A claim that is not an address is discarded rather than stored, and the row then records what
    nginx observed instead. That is a strictly better fallback than failing: ``REMOTE_ADDR`` comes
    off the socket, so it is the one address in this function nobody can choose.
    """
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    # A header that is present but blank, or malformed, falls through to REMOTE_ADDR rather than
    # yielding None: a proxy that sets an empty header, or a caller that sends rubbish, must not
    # cost us the address we already have.
    claimed = _well_formed(forwarded.split(",")[0].strip())
    return claimed or _well_formed(request.META.get("REMOTE_ADDR", "")) or None


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
        ) as ctx:
            response = self.get_response(request)
            # Handed back to the caller, which is the half of correlated logging that was missing.
            # ``deploy/nginx/nginx.conf`` has described this id as "echoed to the client as
            # ``X-Request-ID``" since it was written, and nothing did it: nginx forwards the header
            # upstream but adds none to the response, and Django set none either. So the id joined
            # a log line to an audit row and stopped there, reachable only by someone who already
            # had shell on the box.
            #
            # It matters most for the failures a user is most likely to report and least able to
            # describe. A 500 already carries the id in its body (ADR-0006), but a slow request, a
            # wrong balance or a 403 carries nothing, and this makes all of them quotable.
            #
            # Set from ``ctx`` rather than re-reading the header, so the value returned is the one
            # the rest of the request actually used, including the generated one when the client
            # sent none.
            response["X-Request-ID"] = ctx.request_id
            return response
