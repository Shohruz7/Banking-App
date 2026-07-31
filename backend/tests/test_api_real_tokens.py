"""Week 3's endpoints, re-run against a real Bearer token.

Every other API test in the suite authenticates with ``force_authenticate``, which installs a user
on the request and skips the authentication class entirely. That is fine for testing *views*, but
it means those tests would stay green even if the token pipeline were completely broken. This file
drives the same happy paths through ``SessionAwareJWTAuthentication`` — signature verification,
the ``sid`` claim, the session lookup — so a regression in the auth stack fails something.
"""

from decimal import Decimal

import pytest
from django.contrib.auth.models import User
from django.urls import reverse
from rest_framework.test import APIClient

from identity.models import AuthSession, RevokeReason
from identity.services import revoke_session

from .factories import AccountFactory, fund_account

pytestmark = pytest.mark.django_db


def test_accounts_list_and_detail_with_a_real_token(
    auth_client: APIClient, password_user: User
) -> None:
    account = AccountFactory.create(owner=password_user)
    fund_account(account, Decimal("250.0000"))

    listed = auth_client.get(reverse("account-list"))
    assert listed.status_code == 200
    rows = {row["id"]: row for row in listed.json()["results"]}
    # fund_account also creates the opening-balances equity account, so expect both.
    assert str(account.pk) in rows
    assert rows[str(account.pk)]["balance"] == "250.0000"

    detail = auth_client.get(reverse("account-detail", args=[account.pk]))
    assert detail.status_code == 200
    assert detail.json()["balance"] == "250.0000"


def test_owner_scoping_still_404s_with_a_real_token(auth_client: APIClient) -> None:
    someone_elses = AccountFactory.create()

    assert auth_client.get(reverse("account-detail", args=[someone_elses.pk])).status_code == 404
    assert (
        auth_client.get(reverse("account-transactions", args=[someone_elses.pk])).status_code == 404
    )


def test_transactions_and_transfer_with_a_real_token(
    auth_client: APIClient, password_user: User
) -> None:
    source = AccountFactory.create(owner=password_user)
    destination = AccountFactory.create(owner=password_user)
    fund_account(source, Decimal("100.0000"))

    created = auth_client.post(
        reverse("transfer-create"),
        {
            "source_account": str(source.pk),
            "destination_account": str(destination.pk),
            "amount": "40.0000",
            "idempotency_key": "real-token-key",
        },
        format="json",
    )
    assert created.status_code == 201

    # The idempotent replay still reads 200, not 201.
    replay = auth_client.post(
        reverse("transfer-create"),
        {
            "source_account": str(source.pk),
            "destination_account": str(destination.pk),
            "amount": "40.0000",
            "idempotency_key": "real-token-key",
        },
        format="json",
    )
    assert replay.status_code == 200

    history = auth_client.get(reverse("account-transactions", args=[source.pk]))
    assert history.status_code == 200
    amounts = [row["amount"] for row in history.json()["results"]]
    assert "-40.0000" in amounts

    assert (
        auth_client.get(reverse("account-detail", args=[source.pk])).json()["balance"] == "60.0000"
    )


def test_every_protected_endpoint_dies_with_the_session(
    auth_client: APIClient, password_user: User
) -> None:
    """Revocation is enforced by the authentication class, so it covers the whole API at once."""
    account = AccountFactory.create(owner=password_user)
    assert auth_client.get(reverse("account-list")).status_code == 200

    session = AuthSession.objects.get(user=password_user)
    revoke_session(str(session.id), reason=RevokeReason.ADMIN)

    for url in (
        reverse("account-list"),
        reverse("account-detail", args=[account.pk]),
        reverse("account-transactions", args=[account.pk]),
        reverse("me"),
    ):
        assert auth_client.get(url).status_code == 401, url

    assert auth_client.post(reverse("transfer-create"), {}, format="json").status_code == 401


def test_unauthenticated_requests_are_still_refused(api_client: APIClient) -> None:
    assert api_client.get(reverse("account-list")).status_code == 401
    assert api_client.post(reverse("transfer-create"), {}, format="json").status_code == 401


def test_a_malformed_bearer_token_is_refused(api_client: APIClient) -> None:
    api_client.credentials(HTTP_AUTHORIZATION="Bearer not-a-real-token")
    response = api_client.get(reverse("account-list"))
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "token_not_valid"
