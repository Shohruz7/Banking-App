"""TOTP enrollment, confirmation, and the two-step login it gates.

The determinism problem is worth understanding before reading these. ``pyotp.TOTP(secret).now()``
reads the wall clock, so a code computed at 29.98s into a 30s step is verified by the server in the
*next* step — rare locally, reliably annoying in CI. Two mitigations, not one: the server accepts
one step either side (justified on its own merits by phone clock drift), and the boundary tests
below freeze the clock and use ``.at()`` rather than ``.now()``.
"""

from urllib.parse import parse_qs, urlparse

import pyotp
import pytest
from django.contrib.auth.models import User
from django.urls import reverse
from freezegun import freeze_time
from rest_framework.test import APIClient

from identity.models import AuthSession, MfaDevice
from identity.services import TOTP_INTERVAL, verify_totp

from .conftest import obtain_tokens, totp_now
from .factories import MfaDeviceFactory

pytestmark = pytest.mark.django_db

FROZEN = "2026-07-28 12:00:00"


def _code_at(secret: str, when: str) -> str:
    with freeze_time(when):
        return pyotp.TOTP(secret, interval=TOTP_INTERVAL).now()


def test_enrollment_returns_secret_uri_and_qr(auth_client: APIClient, password_user: User) -> None:
    response = auth_client.post(reverse("mfa-enroll"))

    assert response.status_code == 201
    body = response.json()

    device = MfaDevice.objects.get(user=password_user)
    # Created unconfirmed: MFA is not enforced until the user proves they scanned it.
    assert device.confirmed_at is None
    assert body["secret"] == device.secret

    parsed = urlparse(body["otpauth_uri"])
    assert parsed.scheme == "otpauth"
    assert parsed.netloc == "totp"
    params = parse_qs(parsed.query)
    assert params["secret"] == [device.secret]
    assert params["issuer"] == ["Banking App"]
    # The URI round-trips: a real authenticator app reading it produces codes the server accepts.
    assert pyotp.TOTP(params["secret"][0]).now()

    assert body["qr_svg"].startswith("data:image/svg+xml;base64,")


def test_enrollment_is_rejected_once_confirmed(auth_client: APIClient, password_user: User) -> None:
    MfaDeviceFactory.create(user=password_user, confirmed=True)

    response = auth_client.post(reverse("mfa-enroll"))
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "mfa_already_enrolled"


def test_mfa_is_not_enforced_until_confirmed(
    api_client: APIClient, auth_client: APIClient, password_user: User
) -> None:
    """The lockout-prevention rule: a mistyped enrollment must not lock anyone out."""
    auth_client.post(reverse("mfa-enroll"))

    tokens = obtain_tokens(APIClient(), password_user)
    assert "access" in tokens
    assert "mfa_required" not in tokens


def test_confirm_activates_the_device_and_a_bad_code_does_not(
    auth_client: APIClient, password_user: User
) -> None:
    secret = auth_client.post(reverse("mfa-enroll")).json()["secret"]

    bad = auth_client.post(reverse("mfa-confirm"), {"code": "000000"}, format="json")
    assert bad.status_code == 400
    assert bad.json()["error"]["code"] == "invalid_mfa_code"
    assert MfaDevice.objects.get(user=password_user).confirmed_at is None

    good = auth_client.post(reverse("mfa-confirm"), {"code": totp_now(secret)}, format="json")
    assert good.status_code == 200
    assert good.json() == {"mfa_enabled": True}
    assert MfaDevice.objects.get(user=password_user).confirmed_at is not None


def test_login_with_mfa_requires_the_second_step(
    api_client: APIClient, password_user: User, mfa_device: MfaDevice
) -> None:
    """MFA enforced at login (ADR-0012): 200 with a challenge, and no access token in sight."""
    first = api_client.post(
        reverse("token_obtain_pair"),
        {"username": password_user.username, "password": "sw0rdf1sh-test-pw"},
        format="json",
    )
    assert first.status_code == 200
    challenge = first.json()
    assert challenge["mfa_required"] is True
    assert "access" not in challenge
    assert "refresh" not in challenge
    # The password step alone opens no session.
    assert not AuthSession.objects.exists()

    second = api_client.post(
        reverse("token_mfa_verify"),
        {"mfa_token": challenge["mfa_token"], "code": totp_now(mfa_device.secret)},
        format="json",
    )
    assert second.status_code == 200
    assert {"access", "refresh"} <= set(second.json())
    assert AuthSession.objects.count() == 1


def test_mfa_pending_token_cannot_authenticate(
    api_client: APIClient, password_user: User, mfa_device: MfaDevice
) -> None:
    """The pre-auth token is contained by construction, not by convention.

    ``AUTH_TOKEN_CLASSES`` lists ``AccessToken`` only, so ``verify_token_type`` rejects it.
    """
    challenge = obtain_tokens(api_client, password_user)

    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {challenge['mfa_token']}")
    assert client.get(reverse("account-list")).status_code == 401


def test_mfa_pending_token_is_single_use(
    api_client: APIClient, password_user: User, mfa_device: MfaDevice
) -> None:
    challenge = obtain_tokens(api_client, password_user)
    payload = {"mfa_token": challenge["mfa_token"], "code": totp_now(mfa_device.secret)}

    assert api_client.post(reverse("token_mfa_verify"), payload, format="json").status_code == 200

    # Same challenge, a fresh valid code: still refused, because the challenge itself was spent.
    payload["code"] = totp_now(mfa_device.secret)
    replay = api_client.post(reverse("token_mfa_verify"), payload, format="json")
    assert replay.status_code == 401


def test_mfa_pending_token_expires(
    api_client: APIClient, password_user: User, mfa_device: MfaDevice
) -> None:
    with freeze_time(FROZEN):
        challenge = obtain_tokens(api_client, password_user)

    with freeze_time("2026-07-28 12:06:00"):
        response = api_client.post(
            reverse("token_mfa_verify"),
            {"mfa_token": challenge["mfa_token"], "code": totp_now(mfa_device.secret)},
            format="json",
        )
    assert response.status_code == 401


def test_wrong_code_is_rejected(
    api_client: APIClient, password_user: User, mfa_device: MfaDevice
) -> None:
    challenge = obtain_tokens(api_client, password_user)

    response = api_client.post(
        reverse("token_mfa_verify"),
        {"mfa_token": challenge["mfa_token"], "code": "000000"},
        format="json",
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_mfa_code"


def test_stale_code_is_rejected(
    api_client: APIClient, password_user: User, mfa_device: MfaDevice
) -> None:
    """One step of drift is accepted; three is not."""
    with freeze_time(FROZEN):
        challenge = obtain_tokens(api_client, password_user)
        stale = _code_at(mfa_device.secret, "2026-07-28 11:58:30")

        response = api_client.post(
            reverse("token_mfa_verify"),
            {"mfa_token": challenge["mfa_token"], "code": stale},
            format="json",
        )
    assert response.status_code == 400


def test_the_same_code_cannot_be_used_twice(mfa_device: MfaDevice) -> None:
    """The timestep burn. ``pyotp.verify()`` alone cannot do this — it never says which step hit."""
    with freeze_time(FROZEN):
        code = totp_now(mfa_device.secret)

        assert verify_totp(mfa_device, code) is True
        # Same code, same instant, still inside its validity window — and refused, because the
        # counter it matched has been burned.
        assert verify_totp(mfa_device, code) is False

    mfa_device.refresh_from_db()
    assert mfa_device.last_used_counter > -1
    assert mfa_device.last_used_at is not None


def test_a_replayed_code_is_refused_at_the_login_step(
    api_client: APIClient, password_user: User, mfa_device: MfaDevice
) -> None:
    """End-to-end version of the burn: the same code cannot complete two logins."""
    with freeze_time(FROZEN):
        code = totp_now(mfa_device.secret)

        first = obtain_tokens(api_client, password_user, code=code)
        assert "access" in first

        challenge = obtain_tokens(APIClient(), password_user)
        second = api_client.post(
            reverse("token_mfa_verify"),
            {"mfa_token": challenge["mfa_token"], "code": code},
            format="json",
        )
    assert second.status_code == 400
    assert second.json()["error"]["code"] == "invalid_mfa_code"


def test_disable_requires_a_valid_code_and_revokes_sessions(
    auth_client: APIClient, password_user: User, mfa_device: MfaDevice
) -> None:
    bad = auth_client.post(reverse("mfa-disable"), {"code": "000000"}, format="json")
    assert bad.status_code == 400
    assert MfaDevice.objects.filter(user=password_user).exists()

    good = auth_client.post(
        reverse("mfa-disable"), {"code": totp_now(mfa_device.secret)}, format="json"
    )
    assert good.status_code == 204
    assert not MfaDevice.objects.filter(user=password_user).exists()

    # Changing the authentication requirements of an account kills its live sessions.
    assert not AuthSession.objects.filter(revoked_at__isnull=True).exists()
    assert auth_client.get(reverse("account-list")).status_code == 401


def test_confirm_without_enrolling_is_a_conflict(auth_client: APIClient) -> None:
    response = auth_client.post(reverse("mfa-confirm"), {"code": "123456"}, format="json")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "mfa_not_enrolled"
