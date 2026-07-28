"""Account read endpoints: authentication, owner scoping, derived balances, history paging."""

from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from tests.factories import AccountFactory, UserFactory, fund_account, post_balanced_entry


@pytest.mark.django_db
def test_accounts_require_authentication(api_client: APIClient) -> None:
    response = api_client.get("/api/v1/accounts/")

    assert response.status_code == 401
    body = response.json()
    assert set(body) == {"error"}
    assert body["error"]["code"] == "not_authenticated"


@pytest.mark.django_db
def test_account_list_is_scoped_to_the_owner(api_client: APIClient) -> None:
    owner = UserFactory.create()
    stranger = UserFactory.create()
    mine = AccountFactory.create(owner=owner, name="Checking")
    AccountFactory.create(owner=stranger, name="Not yours")
    fund_account(mine, Decimal("150.00"))

    api_client.force_authenticate(user=owner)
    response = api_client.get("/api/v1/accounts/")

    assert response.status_code == 200
    names = {row["name"] for row in response.json()["results"]}
    assert "Not yours" not in names
    assert "Checking" in names


@pytest.mark.django_db
def test_account_balance_is_derived_and_serialized_as_a_string(api_client: APIClient) -> None:
    owner = UserFactory.create()
    account = AccountFactory.create(owner=owner)
    fund_account(account, Decimal("150.00"))

    api_client.force_authenticate(user=owner)
    response = api_client.get(f"/api/v1/accounts/{account.pk}/")

    assert response.status_code == 200
    # A float here would mean the serializer field is mis-declared (ADR-0009: money is never float).
    assert response.json()["balance"] == "150.0000"


@pytest.mark.django_db
def test_retrieving_another_users_account_is_a_404(api_client: APIClient) -> None:
    """404 rather than 403 — a 403 would confirm the account exists."""
    owner = UserFactory.create()
    stranger = UserFactory.create()
    theirs = AccountFactory.create(owner=stranger)

    api_client.force_authenticate(user=owner)
    response = api_client.get(f"/api/v1/accounts/{theirs.pk}/")

    assert response.status_code == 404


@pytest.mark.django_db
def test_transactions_endpoint_paginates(api_client: APIClient) -> None:
    owner = UserFactory.create()
    account = AccountFactory.create(owner=owner)
    counterparty = AccountFactory.create(owner=owner)
    fund_account(account, Decimal("500.00"))
    for _ in range(24):
        post_balanced_entry(account, counterparty, Decimal("1.00"))

    api_client.force_authenticate(user=owner)
    response = api_client.get(f"/api/v1/accounts/{account.pk}/transactions/")

    assert response.status_code == 200
    body = response.json()
    assert len(body["results"]) == 20  # PAGE_SIZE
    assert body["next"] is not None

    second_page = api_client.get(body["next"])
    assert second_page.status_code == 200
    assert len(second_page.json()["results"]) == 5


@pytest.mark.django_db
def test_transactions_for_another_users_account_is_a_404(api_client: APIClient) -> None:
    owner = UserFactory.create()
    stranger = UserFactory.create()
    theirs = AccountFactory.create(owner=stranger)

    api_client.force_authenticate(user=owner)
    response = api_client.get(f"/api/v1/accounts/{theirs.pk}/transactions/")

    assert response.status_code == 404
