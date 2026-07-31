"""Ambient request context for audit rows, carried in a ``ContextVar`` (ADR-0014).

Deep code that records an audit event — ``ledger.services.transfer`` most of all — has no request
object and should not grow one. Who moved the money is a domain fact and stays an explicit
argument; the IP, user agent and request id are transport facts the ledger has no business knowing,
so they ride here instead.

``contextvars`` rather than a thread-local, and the reason is Week 6: Django Channels serves many
concurrent coroutines on one thread, where a thread-local would leak one user's identity into
another user's audit row. That is a correctness bug in a security log — the worst kind. A
``ContextVar`` is coroutine-scoped and behaves identically under sync WSGI, async ASGI and Celery.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace

from django.contrib.auth.models import User


@dataclass(frozen=True)
class RequestContext:
    """Transport facts about whatever is currently executing. All fields optional."""

    actor: User | None = None
    ip: str | None = None
    user_agent: str = ""
    request_id: str = ""


_EMPTY = RequestContext()

_current: ContextVar[RequestContext] = ContextVar("audit_context", default=_EMPTY)


def current_context() -> RequestContext:
    """Return the ambient context, or an empty one outside any request."""
    return _current.get()


@contextmanager
def audit_context(
    *,
    actor: User | None = None,
    ip: str | None = None,
    user_agent: str = "",
    request_id: str = "",
) -> Iterator[RequestContext]:
    """Publish ambient context for the duration of the block.

    Always restores the previous value via ``reset(token)`` rather than setting a default back:
    workers are reused, and a leaked actor produces a *wrong* audit row, which is worse than a
    missing one. A Week-6 Channels consumer wraps its handler in this and everything below it
    audits correctly.
    """
    ctx = RequestContext(actor=actor, ip=ip, user_agent=user_agent, request_id=request_id)
    token = _current.set(ctx)
    try:
        yield ctx
    finally:
        _current.reset(token)


@contextmanager
def bind_actor(actor: User | None) -> Iterator[RequestContext]:
    """Attach an actor to the ambient context, keeping the transport facts already published."""
    ctx = replace(_current.get(), actor=actor)
    token = _current.set(ctx)
    try:
        yield ctx
    finally:
        _current.reset(token)
