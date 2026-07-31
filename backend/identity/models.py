"""Identity state that hangs off ``django.contrib.auth.models.User``.

ADR-0011 keeps the stock user model, so everything Week 4 adds lives in related models rather than
new columns on ``User``. That is not only cheaper than a swap — it is the shape the domain wants.
A TOTP secret belongs to a *device*, of which a user may eventually have several (plus recovery
codes); a refresh-token family belongs to a *login*, of which a user has many at once.
"""

from django.conf import settings
from django.db import models
from uuid6 import uuid7


class RevokeReason(models.TextChoices):
    LOGOUT = "logout", "Logout"
    REUSE_DETECTED = "reuse_detected", "Refresh token reuse detected"
    MFA_CHANGED = "mfa_changed", "MFA configuration changed"
    ADMIN = "admin", "Revoked by an administrator"


class AuthSession(models.Model):
    """One login, and the family of tokens minted from it.

    Every access and refresh token issued under this session carries its id as the ``sid`` claim,
    which survives refresh rotation and propagates refresh→access. Two things follow, and both are
    the point of the model (ADR-0013):

    * revoking the row kills every token in the family at once, including **access** tokens —
      SimpleJWT's blacklist only ever records refresh tokens, so this is the sole mechanism by
      which an access token becomes revocable before it expires;
    * replaying an already-rotated refresh token identifies the family it belonged to, so the
      successor the thief is holding dies along with the token that was replayed.
    """

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="auth_sessions"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(auto_now_add=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoke_reason = models.CharField(max_length=20, choices=RevokeReason.choices, blank=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["user", "-created_at"], name="session_user_created_idx")]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(revoked_at__isnull=True, revoke_reason="")
                | models.Q(revoked_at__isnull=False),
                name="session_reason_requires_revocation",
            ),
        ]

    def __str__(self) -> str:
        state = "revoked" if self.revoked_at else "active"
        return f"Session {self.id} ({self.user_id}, {state})"

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None


class MfaDevice(models.Model):
    """A user's TOTP authenticator, and the timestep it last consumed.

    Created **unconfirmed** by enrollment and only enforced at login once a correct code has been
    submitted (ADR-0012). Enforcing at enrollment instead is how users permanently lock themselves
    out after mistyping a secret — the single most common self-inflicted MFA bug.

    ``last_used_counter`` is what makes a code single-use. ``pyotp``'s ``verify()`` returns a bare
    bool and never says *which* timestep matched, so it cannot support replay protection on its
    own; ``identity.services.verify_totp`` walks the window itself and burns the matched counter
    with a conditional UPDATE.
    """

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="mfa_devices"
    )
    name = models.CharField(max_length=50, default="Authenticator app")
    secret = models.CharField(max_length=64)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    # -1 rather than 0 so the very first timestep (counter 0, at the Unix epoch) is still burnable.
    last_used_counter = models.BigIntegerField(default=-1)
    last_used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        constraints = [
            # One confirmed device per user in v1. Unconfirmed rows are not constrained, so a
            # re-enrollment attempt can always create a fresh secret to scan.
            models.UniqueConstraint(
                fields=["user"],
                condition=models.Q(confirmed_at__isnull=False),
                name="one_confirmed_mfa_device_per_user",
            ),
        ]

    def __str__(self) -> str:
        state = "confirmed" if self.confirmed_at else "unconfirmed"
        return f"{self.name} ({self.user_id}, {state})"

    @property
    def is_confirmed(self) -> bool:
        return self.confirmed_at is not None
