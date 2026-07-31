"""Registration and the /me profile endpoint."""

import pytest
from django.contrib.auth.models import User
from django.urls import reverse
from rest_framework.test import APIClient

from .conftest import obtain_tokens
from .factories import TEST_PASSWORD, MfaDeviceFactory, UserFactory

pytestmark = pytest.mark.django_db


def test_register_creates_user_and_hashes_password(api_client: APIClient) -> None:
    response = api_client.post(
        reverse("register"),
        {"username": "alice", "email": "alice@example.com", "password": "corr3ct-h0rse-batt"},
        format="json",
    )

    assert response.status_code == 201
    user = User.objects.get(username="alice")
    assert user.check_password("corr3ct-h0rse-batt")
    # The stored value is a hash, not the password.
    assert user.password != "corr3ct-h0rse-batt"

    body = response.json()
    assert body["username"] == "alice"
    assert body["mfa_enabled"] is False
    assert "password" not in body


@pytest.mark.parametrize(
    ("payload", "bad_field"),
    [
        ({"username": "bob", "email": "bob@example.com", "password": "pw"}, "password"),
        ({"username": "bob", "email": "bob@example.com", "password": "12345678"}, "password"),
        ({"username": "bob", "email": "bob@example.com", "password": "password"}, "password"),
        ({"username": "bob", "email": "not-an-email", "password": "corr3ct-h0rse"}, "email"),
        ({"username": "bob", "password": "corr3ct-h0rse"}, "email"),
    ],
)
def test_register_rejects_invalid_payloads(
    api_client: APIClient, payload: dict[str, str], bad_field: str
) -> None:
    response = api_client.post(reverse("register"), payload, format="json")

    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "validation_error"
    assert bad_field in body["error"]["details"]
    assert not User.objects.filter(username="bob").exists()


def test_register_rejects_duplicate_username_and_email(api_client: APIClient) -> None:
    UserFactory.create(username="taken", email="Taken@Example.com")

    dup_username = api_client.post(
        reverse("register"),
        {"username": "taken", "email": "fresh@example.com", "password": "corr3ct-h0rse-batt"},
        format="json",
    )
    assert dup_username.status_code == 400
    assert "username" in dup_username.json()["error"]["details"]

    # Case-insensitive: the model has no unique constraint on email, the serializer enforces it.
    dup_email = api_client.post(
        reverse("register"),
        {"username": "fresh", "email": "taken@example.com", "password": "corr3ct-h0rse-batt"},
        format="json",
    )
    assert dup_email.status_code == 400
    assert "email" in dup_email.json()["error"]["details"]


def test_registered_user_can_log_in_with_email_or_username(api_client: APIClient) -> None:
    """The EmailOrUsernameBackend is what makes the second call work (ADR-0011)."""
    api_client.post(
        reverse("register"),
        {"username": "carol", "email": "carol@example.com", "password": "corr3ct-h0rse-batt"},
        format="json",
    )

    for identifier in ("carol", "CAROL@example.com"):
        response = api_client.post(
            reverse("token_obtain_pair"),
            {"username": identifier, "password": "corr3ct-h0rse-batt"},
            format="json",
        )
        assert response.status_code == 200, identifier
        assert "access" in response.json()


def test_me_returns_the_requesting_user_only(api_client: APIClient) -> None:
    alice, bob = UserFactory.create(), UserFactory.create()

    for user in (alice, bob):
        client = APIClient()
        tokens = obtain_tokens(client, user, password=TEST_PASSWORD)
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")

        body = client.get(reverse("me")).json()
        assert body["id"] == user.pk
        assert body["username"] == user.username
        assert body["mfa_enabled"] is False


def test_me_reports_mfa_only_once_confirmed(auth_client: APIClient, password_user: User) -> None:
    assert auth_client.get(reverse("me")).json()["mfa_enabled"] is False

    MfaDeviceFactory.create(user=password_user)
    assert auth_client.get(reverse("me")).json()["mfa_enabled"] is False

    MfaDeviceFactory.create(user=password_user, confirmed=True)
    assert auth_client.get(reverse("me")).json()["mfa_enabled"] is True


def test_me_requires_authentication(api_client: APIClient) -> None:
    assert api_client.get(reverse("me")).status_code == 401
