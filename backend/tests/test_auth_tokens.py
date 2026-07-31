"""The token lifecycle: obtain, use, rotate, replay, revoke.

Nothing before Week 4 exercised any of this. Every earlier API test authenticates with
``force_authenticate``, which bypasses ``authenticate()`` and the authentication class entirely —
the whole auth stack could have been broken and the suite would still have been green. These tests
speak HTTP and carry real tokens.
"""

import jwt
import pytest
from django.contrib.auth.models import User
from django.urls import reverse
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from identity.models import AuthSession, RevokeReason
from identity.services import _blacklisted_tokens as blacklisted_tokens

from .conftest import obtain_tokens

pytestmark = pytest.mark.django_db


def _bearer(token: str) -> APIClient:
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    return client


def test_token_obtain_use_and_refresh_cycle(api_client: APIClient, password_user: User) -> None:
    """The whole loop over HTTP: obtain a pair, use the access token, rotate the refresh token."""
    tokens = obtain_tokens(api_client, password_user)
    assert set(tokens) == {"access", "refresh"}

    assert _bearer(tokens["access"]).get(reverse("account-list")).status_code == 200

    refreshed = api_client.post(
        reverse("token_refresh"), {"refresh": tokens["refresh"]}, format="json"
    )
    assert refreshed.status_code == 200
    body = refreshed.json()
    assert body["access"] != tokens["access"]
    assert _bearer(body["access"]).get(reverse("account-list")).status_code == 200


def test_refresh_rotates_and_the_old_token_is_rejected(
    api_client: APIClient, password_user: User
) -> None:
    tokens = obtain_tokens(api_client, password_user)
    first = tokens["refresh"]

    rotated = api_client.post(reverse("token_refresh"), {"refresh": first}, format="json").json()
    # Rotation means a *new* refresh token comes back, not just a new access token. Assert it
    # explicitly: without this a rotation regression passes silently.
    assert rotated["refresh"] != first

    old_jti = jwt.decode(first, options={"verify_signature": False})["jti"]
    assert blacklisted_tokens.filter(token__jti=old_jti).exists()

    replay = api_client.post(reverse("token_refresh"), {"refresh": first}, format="json")
    assert replay.status_code == 401
    assert replay.json()["error"]["code"] == "token_not_valid"


def test_refresh_reuse_revokes_the_whole_session(
    api_client: APIClient, password_user: User
) -> None:
    """The week's marquee guarantee.

    Stock SimpleJWT rejects the *replayed* token and stops there — which means in the classic theft
    scenario the attacker, holding the successor, stays logged in indefinitely and the victim's 401
    is an alarm bell nobody listens to. Here the replay takes the whole family down.
    """
    tokens = obtain_tokens(api_client, password_user)
    r1, access = tokens["refresh"], tokens["access"]

    r2 = api_client.post(reverse("token_refresh"), {"refresh": r1}, format="json").json()["refresh"]
    assert _bearer(access).get(reverse("account-list")).status_code == 200

    # The victim (or the thief) replays the token that was already rotated away.
    assert (
        api_client.post(reverse("token_refresh"), {"refresh": r1}, format="json").status_code == 401
    )

    # The successor is now dead too — this is the leg stock rotation does not give you.
    successor = api_client.post(reverse("token_refresh"), {"refresh": r2}, format="json")
    assert successor.status_code == 401

    # And so is the access token from that login, without waiting out its 15 minutes.
    assert _bearer(access).get(reverse("account-list")).status_code == 401

    session = AuthSession.objects.get(user=password_user)
    assert session.revoked_at is not None
    assert session.revoke_reason == RevokeReason.REUSE_DETECTED


def test_forged_token_cannot_revoke_a_victims_session(
    api_client: APIClient, password_user: User
) -> None:
    """``handle_possible_reuse`` verifies the signature before acting on a token's ``sid``.

    This is the attack the check exists to stop, and it is worth spelling out because the naive
    version of the test does not exercise it at all. An attacker logs in as themselves and rotates
    once, which puts a *real, blacklisted* jti in their hands. They then mint a token carrying that
    jti — so the "was this rotated away?" check passes — but swap in the **victim's** ``sid``, and
    sign it with a key they made up. Decode without verifying (the tempting shortcut being
    ``RefreshToken(raw, verify=False)``) and the victim is logged out on demand, by anyone, forever.
    """
    victim = password_user
    victim_tokens = obtain_tokens(api_client, victim)
    victim_session = AuthSession.objects.get(user=victim)

    attacker = User.objects.create_user("attacker", password="attacker-pw-123")
    attacker_client = APIClient()
    attacker_tokens = obtain_tokens(attacker_client, attacker, password="attacker-pw-123")
    attacker_client.post(
        reverse("token_refresh"), {"refresh": attacker_tokens["refresh"]}, format="json"
    )

    burned_jti = jwt.decode(attacker_tokens["refresh"], options={"verify_signature": False})["jti"]
    assert blacklisted_tokens.filter(token__jti=burned_jti).exists()

    forged = jwt.encode(
        {
            "token_type": "refresh",
            "jti": burned_jti,
            "sid": str(victim_session.id),
            "exp": 9999999999,
        },
        "a-key-the-attacker-invented",
        algorithm="HS256",
    )

    response = api_client.post(reverse("token_refresh"), {"refresh": forged}, format="json")
    assert response.status_code == 401

    # Nothing about the victim moved.
    victim_session.refresh_from_db()
    assert victim_session.revoked_at is None
    assert _bearer(victim_tokens["access"]).get(reverse("account-list")).status_code == 200


def test_logout_blacklists_refresh_and_kills_access(
    api_client: APIClient, password_user: User
) -> None:
    tokens = obtain_tokens(api_client, password_user)
    client = _bearer(tokens["access"])

    assert client.get(reverse("account-list")).status_code == 200

    logout = client.post(reverse("logout"), {"refresh": tokens["refresh"]}, format="json")
    assert logout.status_code == 204

    # Both halves of the pair are dead. Stock TokenBlacklistView only manages the first.
    assert (
        api_client.post(
            reverse("token_refresh"), {"refresh": tokens["refresh"]}, format="json"
        ).status_code
        == 401
    )
    assert client.get(reverse("account-list")).status_code == 401

    session = AuthSession.objects.get(user=password_user)
    assert session.revoke_reason == RevokeReason.LOGOUT


def test_access_token_carries_sid_and_sid_is_required(
    api_client: APIClient, password_user: User
) -> None:
    tokens = obtain_tokens(api_client, password_user)
    session = AuthSession.objects.get(user=password_user)

    claims = jwt.decode(tokens["access"], options={"verify_signature": False})
    assert claims["sid"] == str(session.id)

    # A perfectly valid, correctly-signed token with no session binding is still refused: the
    # claim is what makes revocation possible, so a token without one is unrevokable by design.
    unbound = AccessToken.for_user(password_user)
    response = _bearer(str(unbound)).get(reverse("account-list"))
    assert response.status_code == 401


def test_revoked_session_rejects_its_access_token(
    api_client: APIClient, password_user: User
) -> None:
    tokens = obtain_tokens(api_client, password_user)
    client = _bearer(tokens["access"])
    assert client.get(reverse("account-list")).status_code == 200

    session = AuthSession.objects.get(user=password_user)
    from identity.services import revoke_session

    revoke_session(str(session.id), reason=RevokeReason.ADMIN)

    assert client.get(reverse("account-list")).status_code == 401


def test_bad_credentials_are_rejected(api_client: APIClient, password_user: User) -> None:
    response = api_client.post(
        reverse("token_obtain_pair"),
        {"username": password_user.username, "password": "wrong-password"},
        format="json",
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "no_active_account"
    assert not AuthSession.objects.exists()


def test_login_opens_exactly_one_session_recording_the_client(
    api_client: APIClient, password_user: User
) -> None:
    obtain_tokens(api_client, password_user)

    session = AuthSession.objects.get(user=password_user)
    assert session.is_active
    # Populated from the ambient audit context, not from a request argument.
    assert session.ip == "127.0.0.1"
