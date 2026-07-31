"""Defensive branches that the happy paths never reach.

Everything here guards against a state the system should not get into but might: a malformed TOTP
submission, a session revoked between two steps of a login, an audit context nested too deep to be
worth walking. None of them are reachable from a well-behaved client, which is exactly why they
need tests — nothing else would notice if they broke.
"""

from typing import Any

import pytest
from django.contrib.auth.models import User
from django.urls import reverse
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from audit.models import AuditAction, AuditEvent
from audit.services import _scrub, record_audit
from identity.models import AuthSession, MfaDevice, RevokeReason
from identity.services import (
    _blacklist_family_refresh_tokens,
    revoke_session,
    start_session,
    verify_totp,
)

from .conftest import obtain_tokens, totp_now
from .factories import AuthSessionFactory, MfaDeviceFactory

pytestmark = pytest.mark.django_db


# --------------------------------------------------------------------------------------------
# TOTP input handling
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("code", ["", "abcdef", "12 34 56", "??????"])
def test_non_numeric_codes_are_rejected_without_touching_the_counter(
    mfa_device: MfaDevice, code: str
) -> None:
    assert verify_totp(mfa_device, code) is False

    mfa_device.refresh_from_db()
    assert mfa_device.last_used_counter == -1


def test_a_code_from_the_adjacent_step_is_accepted(mfa_device: MfaDevice) -> None:
    """``valid_window=1`` is what makes a drifting phone clock usable rather than infuriating."""
    import pyotp
    from django.utils import timezone

    from identity.services import TOTP_INTERVAL

    now = timezone.now()
    previous_step = int(now.timestamp()) // TOTP_INTERVAL - 1
    code = pyotp.TOTP(mfa_device.secret, interval=TOTP_INTERVAL).at(previous_step * TOTP_INTERVAL)

    assert verify_totp(mfa_device, code) is True


# --------------------------------------------------------------------------------------------
# Token family blacklisting
# --------------------------------------------------------------------------------------------


def test_blacklisting_a_family_skips_undecodable_outstanding_tokens(password_user: User) -> None:
    """A corrupt row in ``OutstandingToken`` must not stop the rest of the family being revoked."""
    from rest_framework_simplejwt.token_blacklist.models import OutstandingToken

    from identity.services import _outstanding_tokens

    session = start_session(password_user)
    good = RefreshToken.for_user(password_user)
    good["sid"] = str(session.id)
    str(good)  # materialise the outstanding row

    outstanding: Any = _outstanding_tokens.filter(user_id=password_user.pk).first()
    assert isinstance(outstanding, OutstandingToken)
    _outstanding_tokens.filter(pk=outstanding.pk).update(token="not-a-decodable-jwt")

    # Does not raise; the loop simply moves on.
    _blacklist_family_refresh_tokens(session)


# --------------------------------------------------------------------------------------------
# Login-flow races
# --------------------------------------------------------------------------------------------


def test_mfa_verify_fails_if_the_device_disappears_between_steps(
    api_client: APIClient, auth_client: APIClient, password_user: User, mfa_device: MfaDevice
) -> None:
    """The challenge is only as good as the device that issued it."""
    challenge = obtain_tokens(api_client, password_user)
    code = totp_now(mfa_device.secret)

    MfaDevice.objects.filter(pk=mfa_device.pk).delete()

    response = api_client.post(
        reverse("token_mfa_verify"),
        {"mfa_token": challenge["mfa_token"], "code": code},
        format="json",
    )
    assert response.status_code == 401


def test_refresh_fails_if_the_session_is_revoked_mid_flight(
    api_client: APIClient, password_user: User
) -> None:
    tokens = obtain_tokens(api_client, password_user)
    session = AuthSession.objects.get(user=password_user)
    revoke_session(str(session.id), reason=RevokeReason.ADMIN)

    response = api_client.post(
        reverse("token_refresh"), {"refresh": tokens["refresh"]}, format="json"
    )
    assert response.status_code == 401


def test_logout_with_a_malformed_refresh_token_is_a_401(auth_client: APIClient) -> None:
    response = auth_client.post(reverse("logout"), {"refresh": "not-a-token"}, format="json")
    assert response.status_code == 401


def test_disable_without_a_confirmed_device_is_a_conflict(auth_client: APIClient) -> None:
    response = auth_client.post(reverse("mfa-disable"), {"code": "123456"}, format="json")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "mfa_not_enrolled"


def test_enrolling_twice_replaces_the_unconfirmed_secret(
    auth_client: APIClient, password_user: User
) -> None:
    """A user who abandoned an enrollment gets a clean one, not a second dangling row."""
    first = auth_client.post(reverse("mfa-enroll")).json()["secret"]
    second = auth_client.post(reverse("mfa-enroll")).json()["secret"]

    assert first != second
    assert MfaDevice.objects.filter(user=password_user).count() == 1


# --------------------------------------------------------------------------------------------
# Audit scrubbing and representation
# --------------------------------------------------------------------------------------------


def test_scrub_handles_lists_and_bottoms_out_on_deep_nesting() -> None:
    assert _scrub([{"password": "x"}, {"amount": "1.0000"}]) == [
        {"password": "[redacted]"},
        {"amount": "1.0000"},
    ]

    # Deeper than the walk limit is redacted wholesale rather than recursed forever.
    deep: dict[str, Any] = {"a": {"b": {"c": {"d": {"e": {"f": {"g": "too deep"}}}}}}}
    assert _scrub(deep)["a"]["b"]["c"]["d"]["e"]["f"] == "[redacted]"


def test_audit_event_is_readable_at_a_glance(password_user: User) -> None:
    event = record_audit(action=AuditAction.LOGIN_SUCCEEDED, actor=password_user)
    assert password_user.username in str(event)
    assert "auth.login_succeeded" in str(event)

    anonymous = record_audit(action=AuditAction.LOGIN_FAILED)
    assert "—" in str(anonymous)


def test_audit_truncates_oversized_values(password_user: User) -> None:
    """Long user agents and identifiers are trimmed to fit rather than raising at INSERT."""
    event = record_audit(
        action=AuditAction.LOGIN_FAILED,
        actor_label="x" * 500,
        target_type="y" * 100,
        target_id="z" * 200,
    )
    assert len(event.actor_label) == 150
    assert len(event.target_type) == 32
    assert len(event.target_id) == 64


def test_session_admin_action_revokes_and_audits(password_user: User) -> None:
    """The admin's revoke button goes through the service, so it blacklists and audits like any
    other revocation rather than being a second, quieter code path."""
    from django.contrib.admin.sites import AdminSite
    from django.http import HttpRequest

    from identity.admin import AuthSessionAdmin
    from identity.models import AuthSession as SessionModel

    live = AuthSessionFactory.create(user=password_user)
    already_revoked = AuthSessionFactory.create(user=password_user)
    revoke_session(str(already_revoked.id), reason=RevokeReason.LOGOUT)

    admin = AuthSessionAdmin(SessionModel, AdminSite())
    request = HttpRequest()
    request.session = {}  # type: ignore[assignment]
    request._messages = type("_M", (), {"add": lambda *a, **k: None})()  # type: ignore[attr-defined]

    admin.revoke_selected_sessions(request, SessionModel.objects.all())

    live.refresh_from_db()
    assert live.revoke_reason == RevokeReason.ADMIN
    # The already-revoked one keeps its original reason — revocation is idempotent.
    already_revoked.refresh_from_db()
    assert already_revoked.revoke_reason == RevokeReason.LOGOUT

    reasons = set(
        AuditEvent.objects.filter(action=AuditAction.SESSION_REVOKED).values_list(
            "context__reason", flat=True
        )
    )
    assert reasons == {"logout", "admin"}


def test_unconfirmed_device_is_not_a_confirmed_one(password_user: User) -> None:
    device = MfaDeviceFactory.create(user=password_user)
    assert not device.is_confirmed
    assert "unconfirmed" in str(device)
