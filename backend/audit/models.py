"""The append-only audit log (ADR-0014).

One row per state change or security-relevant attempt: who, what, when, and enough context to
reconstruct it. Rows are written only through :func:`audit.services.record_audit`, and the database
itself refuses to UPDATE or DELETE them — the same defense-in-depth shape as the ledger's zero-sum
trigger, where the application validates and Postgres is what makes it *true*.
"""

from typing import Any, NoReturn

from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models
from uuid6 import uuid7


class AuditAction(models.TextChoices):
    """Every auditable event, namespaced by the subsystem that emits it."""

    USER_REGISTERED = "user.registered", "User registered"
    LOGIN_SUCCEEDED = "auth.login_succeeded", "Login succeeded"
    LOGIN_FAILED = "auth.login_failed", "Login failed"
    MFA_CHALLENGED = "auth.mfa_challenged", "MFA challenge issued"
    MFA_VERIFIED = "auth.mfa_verified", "MFA code verified"
    MFA_FAILED = "auth.mfa_failed", "MFA code rejected"
    MFA_ENROLLED = "auth.mfa_enrolled", "MFA device enrolled"
    MFA_CONFIRMED = "auth.mfa_confirmed", "MFA device confirmed"
    MFA_DISABLED = "auth.mfa_disabled", "MFA disabled"
    TOKEN_REFRESHED = "auth.token_refreshed", "Refresh token rotated"
    TOKEN_REUSE_DETECTED = "auth.token_reuse_detected", "Refresh token reuse detected"
    SESSION_REVOKED = "auth.session_revoked", "Session revoked"
    LOGOUT = "auth.logout", "Logout"
    TRANSFER_POSTED = "ledger.transfer_posted", "Transfer posted"
    TRANSFER_REPLAYED = "ledger.transfer_replayed", "Transfer replayed (idempotent)"
    TRANSFER_REJECTED = "ledger.transfer_rejected", "Transfer rejected"
    # A key reused for a different request (ADR-0024). Worth its own row rather than folding into
    # TRANSFER_REJECTED: a rejection is a request the rules refused, while this is a request the
    # system could not safely tell apart from an earlier one — including, before ADR-0024, somebody
    # else's. A cluster of these on one key is the signature worth being able to search for.
    TRANSFER_KEY_CONFLICT = "ledger.transfer_key_conflict", "Transfer idempotency key conflict"
    ORDER_PLACED = "trading.order_placed", "Order placed"
    ORDER_RESTED = "trading.order_rested", "Limit order resting"
    ORDER_FILLED = "trading.order_filled", "Order filled"
    ORDER_REJECTED = "trading.order_rejected", "Order rejected"
    ORDER_CANCELLED = "trading.order_cancelled", "Order cancelled"
    ORDER_KEY_CONFLICT = "trading.order_key_conflict", "Order idempotency key conflict"
    # Written on every reconciliation run, clean or not (ADR-0026). The all-clear rows are the
    # valuable ones: a gap in the sequence says the check stopped running, which is the failure
    # mode a report that only speaks up on bad news cannot tell you about.
    LEDGER_RECONCILED = "ledger.reconciled", "Ledger invariants checked"


class AuditEvent(models.Model):
    """One immutable record of something that happened.

    Two field choices carry most of the design:

    ``target_type``/``target_id`` are denormalized strings rather than a ``GenericForeignKey``.
    A GFK's ``object_id`` cannot cleanly hold both the UUID PKs of ledger objects and the integer
    PKs of users; it adds a real FK to ``ContentType``, coupling audit rows to the lifecycle of
    whatever they describe; and an audit log must be able to outlive its targets and be shipped to
    cold storage without joins.

    ``context`` uses ``DjangoJSONEncoder`` so a ``Decimal`` serializes as the string ``"30.0000"``
    and never as a float — ADR-0009's money contract followed all the way into the log.
    :func:`audit.services.record_audit` additionally scrubs credential-shaped keys before writing:
    an audit log that leaks the second factor is worse than no audit log at all.
    """

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    # Nullable because a failed login for a username that does not exist has no actor and is
    # exactly the kind of event worth keeping. PROTECT matches Account.owner; combined with
    # append-only it makes a user with history undeletable, which is correct for a bank.
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="audit_events",
    )
    # Snapshot of who acted, so the row still reads correctly when actor is NULL (a failed login
    # names the attempted identifier) or if the user is ever renamed or anonymized.
    actor_label = models.CharField(max_length=150, blank=True)
    action = models.CharField(max_length=32, choices=AuditAction.choices)
    target_type = models.CharField(max_length=32, blank=True)
    target_id = models.CharField(max_length=64, blank=True)
    context = models.JSONField(default=dict, blank=True, encoder=DjangoJSONEncoder)
    ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=255, blank=True)
    # Correlates the several rows one request can produce (challenge → verify → login).
    request_id = models.CharField(max_length=36, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["actor", "-created_at"], name="audit_actor_created_idx"),
            models.Index(fields=["action", "-created_at"], name="audit_action_created_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.created_at:%Y-%m-%d %H:%M:%S} {self.action} by {self.actor_label or '—'}"

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Refuse to rewrite an existing row.

        The trigger in migration 0002 is what makes immutability *true*; this makes a mistake
        surface as a clear Python error at the call site instead of an opaque InternalError from
        Postgres that has already poisoned the surrounding transaction.
        """
        if self._state.adding is False:
            raise ValueError("AuditEvent is append-only: existing rows cannot be modified.")
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> NoReturn:
        raise ValueError("AuditEvent is append-only: rows cannot be deleted.")
