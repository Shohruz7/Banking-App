"""The sanctioned writer of audit rows (ADR-0014).

Explicit calls, not signals and not middleware. ``post_save`` on ``JournalEntry`` cannot tell a
transfer from a fee from a Week-5 trade fill, and is structurally blind to everything that does not
write a model — failed logins, MFA challenges, reuse detection, rejected transfers. Middleware sees
a method, a path and a status code, so it cannot tell ``201 posted`` from ``200 replayed`` without
re-deriving domain meaning from HTTP, and it does not run under Celery (Week 5) or Channels (Week
6) at all. The ledger already established "one sanctioned write path" with ``post_entry``; the
audit log gets the same shape for the same reason.
"""

from collections.abc import Mapping
from typing import Any

from django.contrib.auth.models import User

from .context import current_context
from .models import AuditAction, AuditEvent

#: Context keys whose values are never persisted. Substring match, case-insensitive: an audit log
#: that records the second factor is worse than no audit log at all.
_REDACTED_KEYS = (
    "password",
    "secret",
    "token",
    "code",
    "otp",
    "refresh",
    "access",
    "authorization",
)

_REDACTED = "[redacted]"


def _scrub(value: Any, *, depth: int = 0) -> Any:
    """Recursively redact credential-shaped keys, leaving everything else intact."""
    if depth > 5:
        return _REDACTED
    if isinstance(value, Mapping):
        return {
            key: (
                _REDACTED
                if any(marker in str(key).lower() for marker in _REDACTED_KEYS)
                else _scrub(item, depth=depth + 1)
            )
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [_scrub(item, depth=depth + 1) for item in value]
    return value


def record_audit(
    *,
    action: AuditAction,
    actor: User | None = None,
    actor_label: str = "",
    target_type: str = "",
    target_id: str = "",
    context: Mapping[str, Any] | None = None,
) -> AuditEvent:
    """Write one audit row and return it.

    ``ip``, ``user_agent`` and ``request_id`` come from the ambient context published by
    :class:`audit.middleware.AuditContextMiddleware` (or by an explicit
    :func:`audit.context.audit_context` block in a Celery task or Channels consumer). An explicit
    ``actor`` always wins over the ambient one, so a background job that knows whose order it is
    filling stays explicit and unit-testable.

    Callers decide the transaction placement, and it matters (ADR-0014): a row describing a *state
    change* belongs inside the transaction that makes the change, so the two commit or vanish
    together and the log can never claim money moved when it did not. A row describing a rejected
    *attempt* must be written by a caller after the failed transaction has unwound, or it would be
    rolled back by the very exception it records.
    """
    ambient = current_context()
    effective_actor = actor if actor is not None else ambient.actor

    if not actor_label and effective_actor is not None:
        actor_label = effective_actor.get_username()

    return AuditEvent.objects.create(
        actor=effective_actor,
        actor_label=actor_label[:150],
        action=action,
        target_type=target_type[:32],
        target_id=str(target_id)[:64],
        context=_scrub(dict(context or {})),
        ip=ambient.ip,
        user_agent=ambient.user_agent[:255],
        request_id=ambient.request_id[:36],
    )
