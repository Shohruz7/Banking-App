"""Direct tests for the identity service layer.

These cover the branches an HTTP test cannot reach cleanly: what happens when a *forged* or
*expired* token is offered to the reuse detector, what the auth backend does with a missing user,
and the defensive paths that keep revocation idempotent. Several of them exist specifically to
prove a security property holds in the negative — that something does **not** happen.
"""

from datetime import timedelta

import jwt
import pytest
from django.contrib.auth.models import User
from django.utils import timezone
from rest_framework_simplejwt.tokens import RefreshToken

from audit.models import AuditAction, AuditEvent
from identity.backends import EmailOrUsernameBackend
from identity.models import AuthSession, RevokeReason
from identity.services import (
    active_session,
    confirmed_device,
    handle_possible_reuse,
    require_mfa_for,
    revoke_session,
    start_session,
)

from .factories import TEST_PASSWORD, AuthSessionFactory, MfaDeviceFactory, UserFactory

pytestmark = pytest.mark.django_db


# --------------------------------------------------------------------------------------------
# Reuse detection: what must NOT revoke a session
# --------------------------------------------------------------------------------------------


def test_garbage_token_revokes_nothing() -> None:
    session = AuthSessionFactory.create()

    assert handle_possible_reuse("not-a-jwt-at-all") is None

    session.refresh_from_db()
    assert session.revoked_at is None


def test_forged_token_with_a_real_sid_revokes_nothing() -> None:
    """The signature check in ``handle_possible_reuse`` is the whole defence here.

    Decode without verifying and any stranger could log out any user by guessing a session id.
    """
    session = AuthSessionFactory.create()
    forged = jwt.encode(
        {"jti": "whatever", "sid": str(session.id), "exp": 9999999999},
        "wrong-signing-key",
        algorithm="HS256",
    )

    assert handle_possible_reuse(forged) is None

    session.refresh_from_db()
    assert session.revoked_at is None


def test_a_valid_but_never_rotated_token_is_not_treated_as_reuse(password_user: User) -> None:
    """Only a token that was *blacklisted* — i.e. rotated away — counts as a replay."""
    session = start_session(password_user)
    refresh = RefreshToken.for_user(password_user)
    refresh["sid"] = str(session.id)

    assert handle_possible_reuse(str(refresh)) is None

    session.refresh_from_db()
    assert session.revoked_at is None


def test_a_token_without_a_sid_revokes_nothing(password_user: User) -> None:
    refresh = RefreshToken.for_user(password_user)

    assert handle_possible_reuse(str(refresh)) is None
    assert not AuditEvent.objects.filter(action=AuditAction.TOKEN_REUSE_DETECTED).exists()


def test_a_token_naming_an_unknown_session_revokes_nothing(password_user: User) -> None:
    refresh = RefreshToken.for_user(password_user)
    refresh["sid"] = "00000000-0000-0000-0000-000000000000"
    refresh.blacklist()

    assert handle_possible_reuse(str(refresh)) is None


# --------------------------------------------------------------------------------------------
# Session lifecycle
# --------------------------------------------------------------------------------------------


def test_revoking_twice_audits_once() -> None:
    session = AuthSessionFactory.create()

    assert revoke_session(str(session.id), reason=RevokeReason.LOGOUT) is not None
    # Idempotent: a double logout, or a second replay, must not spam the log.
    assert revoke_session(str(session.id), reason=RevokeReason.ADMIN) is None

    assert AuditEvent.objects.filter(action=AuditAction.SESSION_REVOKED).count() == 1
    session.refresh_from_db()
    assert session.revoke_reason == RevokeReason.LOGOUT


def test_active_session_rejects_missing_unknown_and_revoked() -> None:
    assert active_session(None) is None
    assert active_session("") is None
    assert active_session("00000000-0000-0000-0000-000000000000") is None

    session = AuthSessionFactory.create()
    assert active_session(str(session.id)) is not None

    revoke_session(str(session.id), reason=RevokeReason.ADMIN)
    assert active_session(str(session.id)) is None


def test_revocation_blacklists_only_this_session_family(password_user: User) -> None:
    """Two logins, one revoked: the other must survive."""
    doomed = start_session(password_user)
    survivor = start_session(password_user)

    for session in (doomed, survivor):
        token = RefreshToken.for_user(password_user)
        token["sid"] = str(session.id)
        # Outstanding rows are what _blacklist_family_refresh_tokens scans.
        token.set_exp(from_time=timezone.now(), lifetime=timedelta(days=1))

    revoke_session(str(doomed.id), reason=RevokeReason.REUSE_DETECTED)

    assert AuthSession.objects.get(pk=doomed.pk).revoked_at is not None
    assert AuthSession.objects.get(pk=survivor.pk).revoked_at is None


def test_session_string_representation() -> None:
    session = AuthSessionFactory.create()
    assert "active" in str(session)
    assert session.is_active

    revoke_session(str(session.id), reason=RevokeReason.ADMIN)
    session.refresh_from_db()
    assert "revoked" in str(session)
    assert not session.is_active


# --------------------------------------------------------------------------------------------
# MFA policy and the auth backend
# --------------------------------------------------------------------------------------------


def test_require_mfa_for_gates_login_only(password_user: User) -> None:
    """v1 policy, and the named hook for gating high-value transfers later (ADR-0012)."""
    assert require_mfa_for("login", password_user) is False

    MfaDeviceFactory.create(user=password_user, confirmed=True)
    assert require_mfa_for("login", password_user) is True
    # The extension point exists but gates nothing else yet — deliberately.
    assert require_mfa_for("transfer", password_user) is False


def test_confirmed_device_ignores_unconfirmed_enrollments(password_user: User) -> None:
    MfaDeviceFactory.create(user=password_user)
    assert confirmed_device(password_user) is None

    device = MfaDeviceFactory.create(user=password_user, confirmed=True)
    assert confirmed_device(password_user) == device
    assert "confirmed" in str(device)
    assert device.is_confirmed


def test_backend_returns_none_for_unknown_user_and_wrong_password() -> None:
    backend = EmailOrUsernameBackend()
    user = UserFactory.create(username="dana", email="dana@example.com")

    assert backend.authenticate(None, username="dana", password=TEST_PASSWORD) == user
    assert backend.authenticate(None, username="dana@example.com", password=TEST_PASSWORD) == user
    assert backend.authenticate(None, username="dana", password="nope") is None
    assert backend.authenticate(None, username="ghost", password=TEST_PASSWORD) is None
    # Missing either half is not an authentication attempt at all.
    assert backend.authenticate(None, username=None, password=TEST_PASSWORD) is None
    assert backend.authenticate(None, username="dana", password=None) is None


def test_backend_refuses_inactive_users() -> None:
    user = UserFactory.create(is_active=False)
    assert (
        EmailOrUsernameBackend().authenticate(None, username=user.username, password=TEST_PASSWORD)
        is None
    )
