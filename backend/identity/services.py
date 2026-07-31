"""Session lifecycle and TOTP verification — the sanctioned identity API.

Two guarantees live here, and both are things SimpleJWT does not provide:

* **Access tokens are revocable.** The blacklist app only ever records *refresh* tokens, so a
  stolen access token is otherwise valid for its full lifetime no matter what. Binding every token
  to an :class:`~identity.models.AuthSession` via the ``sid`` claim, and checking that session on
  each request, is the only mechanism that closes that window (ADR-0013).

* **Reuse of a rotated refresh token kills the whole family.** Stock rotation blacklists the old
  token, so replaying it 401s — but the *successor* stays alive, which means in the classic theft
  scenario the attacker keeps working and the victim's 401 is the only alarm bell, thrown away.
  :func:`handle_possible_reuse` catches that bell and revokes the session.

TOTP verification is likewise written out rather than delegated: ``pyotp.TOTP.verify()`` returns a
bare bool and never says which timestep matched, so it cannot support replay protection.
"""

import base64
import hmac
import io
from datetime import datetime

import pyotp
import qrcode
import qrcode.image.svg
from django.contrib.auth.models import User
from django.db import models
from django.utils import timezone
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken, OutstandingToken
from rest_framework_simplejwt.tokens import UntypedToken

from audit.models import AuditAction
from audit.services import record_audit

from .models import AuthSession, MfaDevice, RevokeReason

# django-stubs cannot synthesise `.objects` for SimpleJWT's blacklist models — the package ships no
# django-stubs-aware annotations — so the managers are bound once here rather than ignored at each
# of the half-dozen call sites.
_outstanding_tokens: models.Manager[OutstandingToken] = OutstandingToken.objects  # type: ignore[attr-defined]
_blacklisted_tokens: models.Manager[BlacklistedToken] = BlacklistedToken.objects  # type: ignore[attr-defined]

TOTP_INTERVAL = 30
#: One step either side of now (~90s of acceptance). Phone clocks drift; every production TOTP
#: implementation allows for it. The brute-force cost is answered by the `mfa` throttle scope
#: (ADR-0015), not by narrowing this window.
TOTP_VALID_WINDOW = 1

TOTP_ISSUER = "Banking App"


# --------------------------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------------------------


def start_session(user: User, *, ip: str | None = None, user_agent: str = "") -> AuthSession:
    """Open a session for a successful login. Its id becomes the ``sid`` claim on every token."""
    return AuthSession.objects.create(user=user, ip=ip, user_agent=user_agent[:255])


def active_session(sid: str | None) -> AuthSession | None:
    """Return the live session for a ``sid`` claim, or None if it is missing, unknown or revoked."""
    if not sid:
        return None
    return AuthSession.objects.filter(id=sid, revoked_at__isnull=True).first()


def touch_session(session: AuthSession) -> None:
    """Record use without a full save, so a refresh cannot clobber a concurrent revocation."""
    AuthSession.objects.filter(pk=session.pk).update(last_used_at=timezone.now())


def revoke_session(sid: str, *, reason: RevokeReason) -> AuthSession | None:
    """Revoke a session and blacklist every live refresh token in its family.

    Idempotent: a session already revoked returns None and audits nothing, so a double logout or a
    second replay does not spam the log.
    """
    updated = AuthSession.objects.filter(id=sid, revoked_at__isnull=True).update(
        revoked_at=timezone.now(), revoke_reason=reason
    )
    if not updated:
        return None

    session = AuthSession.objects.select_related("user").get(id=sid)
    _blacklist_family_refresh_tokens(session)
    record_audit(
        action=AuditAction.SESSION_REVOKED,
        actor=session.user,
        target_type="auth_session",
        target_id=str(session.id),
        context={"reason": str(reason)},
    )
    return session


def _blacklist_family_refresh_tokens(session: AuthSession) -> None:
    """Blacklist the session's outstanding refresh tokens.

    Strictly redundant with the session check on refresh — and that is the honest reason to keep
    it: the refresh path stays dead even if someone later drops the session check for performance.
    ``OutstandingToken`` has no ``sid`` column, so this decodes and matches; the set is bounded (a
    handful of live tokens per user) and this runs only on revocation.
    """
    live = _outstanding_tokens.filter(
        user_id=session.user_id,
        expires_at__gt=timezone.now(),
        blacklistedtoken__isnull=True,
    )
    for outstanding in live:
        try:
            payload = UntypedToken(outstanding.token).payload  # type: ignore[arg-type]
        except TokenError:
            continue
        if payload.get("sid") == str(session.id):
            _blacklisted_tokens.get_or_create(token=outstanding)


def handle_possible_reuse(raw_token: str) -> AuthSession | None:
    """Revoke the whole family when an already-rotated refresh token comes back.

    Decoding uses ``UntypedToken``, which **verifies the signature** and ``exp`` while skipping the
    blacklist check (it is not a ``BlacklistMixin`` subclass). This is load-bearing: the obvious
    alternative, ``RefreshToken(raw, verify=False)``, sets ``verify_signature: False``, which would
    let anyone forge a JWT carrying a victim's ``sid`` and force-log-them-out at will — a trivially
    exploitable denial of service.

    A token that fails for any *other* reason (expired, malformed, forged) revokes nothing.
    """
    try:
        payload = UntypedToken(raw_token).payload  # type: ignore[arg-type]
    except TokenError:
        return None

    jti, sid = payload.get("jti"), payload.get("sid")
    if not jti or not sid:
        return None

    # Only a token that was rotated away — i.e. blacklisted — counts as reuse.
    if not _blacklisted_tokens.filter(token__jti=jti).exists():
        return None

    session = AuthSession.objects.filter(id=sid).select_related("user").first()
    if session is None:
        return None

    # A session already revoked needs no second alarm. Once reuse takes a family down, *every*
    # remaining token in it is blacklisted and will arrive here — the successor the thief holds,
    # the one the honest client retries with. Auditing each of those as a fresh detection would
    # bury the one event that actually mattered under its own consequences.
    if session.revoked_at is not None:
        return session

    record_audit(
        action=AuditAction.TOKEN_REUSE_DETECTED,
        actor=session.user,
        target_type="auth_session",
        target_id=str(session.id),
        context={"jti": jti},
    )
    revoke_session(str(session.id), reason=RevokeReason.REUSE_DETECTED)
    return session


# --------------------------------------------------------------------------------------------
# TOTP
# --------------------------------------------------------------------------------------------


def confirmed_device(user: User) -> MfaDevice | None:
    """The user's active authenticator, or None. Unconfirmed enrollments do not count."""
    return MfaDevice.objects.filter(user=user, confirmed_at__isnull=False).first()


def require_mfa_for(action: str, user: User) -> bool:
    """Whether ``action`` must be re-authenticated with a second factor.

    v1 gates login only (ADR-0012). This is the named extension point for gating high-value
    transfers later: the call site exists, the policy is one branch, and nothing else has to move.
    """
    return action == "login" and confirmed_device(user) is not None


def enroll_totp(user: User) -> MfaDevice:
    """Create an **unconfirmed** device with a fresh secret.

    Unconfirmed on purpose: MFA is not enforced until a correct code proves the user actually
    scanned the QR. Enforcing at enrollment is how a mistyped secret becomes a permanent lockout.
    """
    MfaDevice.objects.filter(user=user, confirmed_at__isnull=True).delete()
    return MfaDevice.objects.create(user=user, secret=pyotp.random_base32())


def provisioning_uri(device: MfaDevice, user: User) -> str:
    """The ``otpauth://`` URI an authenticator app expects behind a QR code."""
    return pyotp.TOTP(device.secret, interval=TOTP_INTERVAL).provisioning_uri(
        name=user.get_username(), issuer_name=TOTP_ISSUER
    )


def qr_svg_data_uri(uri: str) -> str:
    """Render the provisioning URI as an inline SVG data URI.

    SVG via ``SvgPathImage`` keeps this pure Python — no Pillow, no binary image dependency for
    what is ultimately a string the client could render itself.
    """
    image = qrcode.make(uri, image_factory=qrcode.image.svg.SvgPathImage)
    buffer = io.BytesIO()
    image.save(buffer)
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/svg+xml;base64,{encoded}"


def verify_totp(device: MfaDevice, code: str, *, now: datetime | None = None) -> bool:
    """Verify a code and burn its timestep, so any given code works exactly once.

    The conditional ``UPDATE ... WHERE last_used_counter < counter`` is what makes the burn safe
    under concurrency: two simultaneous submissions of the same code both match, but only one
    UPDATE reports a modified row, and the loser is treated as a replay. That is the same "let the
    database settle the race" move as the idempotency unique constraint in ``ledger.transfer``.
    """
    if not code or not code.isdigit():
        return False

    totp = pyotp.TOTP(device.secret, interval=TOTP_INTERVAL)
    current = int((now or timezone.now()).timestamp()) // TOTP_INTERVAL

    for offset in range(-TOTP_VALID_WINDOW, TOTP_VALID_WINDOW + 1):
        counter = current + offset
        expected = totp.at(counter * TOTP_INTERVAL)
        # Constant-time: a timing side channel on a 6-digit secret is a real oracle.
        if not hmac.compare_digest(code, expected):
            continue
        burned = MfaDevice.objects.filter(pk=device.pk, last_used_counter__lt=counter).update(
            last_used_counter=counter, last_used_at=timezone.now()
        )
        return bool(burned)

    return False


def confirm_totp(device: MfaDevice, code: str, *, now: datetime | None = None) -> bool:
    """Activate an enrolled device once the user proves they can generate its codes."""
    if not verify_totp(device, code, now=now):
        return False
    device.confirmed_at = timezone.now()
    device.save(update_fields=["confirmed_at"])
    return True
