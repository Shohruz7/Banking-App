"""POST /api/v1/transfers/ — the write endpoint, its status codes, and its error envelope."""

from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from ledger import views as ledger_views
from ledger.exceptions import InvalidEntryError
from ledger.services import get_balance
from tests.factories import AccountFactory, UserFactory, fund_account


@pytest.mark.django_db
def test_transfer_requires_authentication(api_client: APIClient) -> None:
    response = api_client.post("/api/v1/transfers/", data={}, format="json")

    assert response.status_code == 401


@pytest.mark.django_db
def test_transfer_posts_and_moves_money(api_client: APIClient) -> None:
    owner = UserFactory.create()
    source = AccountFactory.create(owner=owner)
    destination = AccountFactory.create(owner=owner)
    fund_account(source, Decimal("100.00"))

    api_client.force_authenticate(user=owner)
    response = api_client.post(
        "/api/v1/transfers/",
        data={
            "source_account": str(source.pk),
            "destination_account": str(destination.pk),
            "amount": "30.00",
            "description": "rent",
        },
        format="json",
    )

    assert response.status_code == 201
    body = response.json()
    assert body["description"] == "rent"
    assert sorted(line["amount"] for line in body["lines"]) == ["-30.0000", "30.0000"]
    assert get_balance(source) == Decimal("70.0000")

    # The read side agrees with the write side.
    listed = api_client.get(f"/api/v1/accounts/{source.pk}/")
    assert listed.json()["balance"] == "70.0000"


@pytest.mark.django_db
def test_transfer_with_insufficient_funds_returns_the_envelope(api_client: APIClient) -> None:
    owner = UserFactory.create()
    source = AccountFactory.create(owner=owner)
    destination = AccountFactory.create(owner=owner)
    fund_account(source, Decimal("10.00"))

    api_client.force_authenticate(user=owner)
    response = api_client.post(
        "/api/v1/transfers/",
        data={
            "source_account": str(source.pk),
            "destination_account": str(destination.pk),
            "amount": "50.00",
        },
        format="json",
    )

    assert response.status_code == 400
    body = response.json()
    assert set(body) == {"error"}
    assert body["error"]["code"] == "insufficient_funds"
    assert get_balance(source) == Decimal("10.0000")


@pytest.mark.django_db
def test_transfer_to_the_same_account_is_rejected(api_client: APIClient) -> None:
    owner = UserFactory.create()
    account = AccountFactory.create(owner=owner)
    fund_account(account, Decimal("100.00"))

    api_client.force_authenticate(user=owner)
    response = api_client.post(
        "/api/v1/transfers/",
        data={
            "source_account": str(account.pk),
            "destination_account": str(account.pk),
            "amount": "10.00",
        },
        format="json",
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "same_account"


@pytest.mark.django_db
def test_transfer_replay_returns_200_and_the_same_entry(api_client: APIClient) -> None:
    owner = UserFactory.create()
    source = AccountFactory.create(owner=owner)
    destination = AccountFactory.create(owner=owner)
    fund_account(source, Decimal("100.00"))

    api_client.force_authenticate(user=owner)
    payload = {
        "source_account": str(source.pk),
        "destination_account": str(destination.pk),
        "amount": "25.00",
        "idempotency_key": "client-key-1",
    }

    first = api_client.post("/api/v1/transfers/", data=payload, format="json")
    second = api_client.post("/api/v1/transfers/", data=payload, format="json")

    assert first.status_code == 201
    assert second.status_code == 200, "a retry is a replay, not an error"
    assert first.json()["id"] == second.json()["id"]
    assert get_balance(source) == Decimal("75.0000")


@pytest.mark.django_db
def test_transfer_from_an_unowned_account_is_a_404(api_client: APIClient) -> None:
    owner = UserFactory.create()
    stranger = UserFactory.create()
    theirs = AccountFactory.create(owner=stranger)
    mine = AccountFactory.create(owner=owner)
    fund_account(theirs, Decimal("100.00"))

    api_client.force_authenticate(user=owner)
    response = api_client.post(
        "/api/v1/transfers/",
        data={
            "source_account": str(theirs.pk),
            "destination_account": str(mine.pk),
            "amount": "10.00",
        },
        format="json",
    )

    assert response.status_code == 404
    assert get_balance(theirs) == Decimal("100.0000")


@pytest.mark.django_db
def test_transfer_to_an_unknown_destination_is_rejected(api_client: APIClient) -> None:
    owner = UserFactory.create()
    source = AccountFactory.create(owner=owner)
    fund_account(source, Decimal("100.00"))

    api_client.force_authenticate(user=owner)
    response = api_client.post(
        "/api/v1/transfers/",
        data={
            "source_account": str(source.pk),
            "destination_account": "00000000-0000-7000-8000-000000000000",
            "amount": "10.00",
        },
        format="json",
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "destination_not_found"


@pytest.mark.django_db
def test_service_rejection_becomes_an_envelope_not_a_500(
    api_client: APIClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The service is the authority on validity, so anything it rejects must map to a 4xx.

    The view pre-checks the cases it knows about, but a rule that lives only in the service —
    now or after a future change — must not reach the client as an unhandled 500.
    """
    owner = UserFactory.create()
    source = AccountFactory.create(owner=owner)
    destination = AccountFactory.create(owner=owner)
    fund_account(source, Decimal("100.00"))

    def reject(**kwargs: object) -> None:
        raise InvalidEntryError("service says no")

    monkeypatch.setattr(ledger_views, "transfer", reject)

    api_client.force_authenticate(user=owner)
    response = api_client.post(
        "/api/v1/transfers/",
        data={
            "source_account": str(source.pk),
            "destination_account": str(destination.pk),
            "amount": "10.00",
        },
        format="json",
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_transfer"


@pytest.mark.django_db
@pytest.mark.parametrize("amount", ["0.00", "-5.00"])
def test_transfer_rejects_non_positive_amounts(api_client: APIClient, amount: str) -> None:
    owner = UserFactory.create()
    source = AccountFactory.create(owner=owner)
    destination = AccountFactory.create(owner=owner)
    fund_account(source, Decimal("100.00"))

    api_client.force_authenticate(user=owner)
    response = api_client.post(
        "/api/v1/transfers/",
        data={
            "source_account": str(source.pk),
            "destination_account": str(destination.pk),
            "amount": amount,
        },
        format="json",
    )

    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "validation_error"
    assert "amount" in body["error"]["details"]
