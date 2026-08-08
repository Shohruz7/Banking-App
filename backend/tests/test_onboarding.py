"""What registration gives a new customer (ledger/onboarding.py).

Before Week 8 registration created a ``User`` row and nothing else, which no test noticed because
every test builds its own accounts through a factory. A browser noticed immediately: sign up, land
on an empty dashboard, and be unable to transfer or trade, because ``POST /orders/`` needs a
``cash_account`` that does not exist.
"""

from decimal import Decimal

import pytest
from django.contrib.auth.models import User
from django.urls import reverse
from rest_framework.test import APIClient

from accounts.models import Account, AccountType
from audit.models import AuditAction, AuditEvent
from ledger.onboarding import CHECKING_NAME, OPENING_BALANCES_NAME, SAVINGS_NAME
from ledger.services import get_balance

pytestmark = pytest.mark.django_db

REGISTRATION = {
    "username": "newcomer",
    "email": "newcomer@example.com",
    "password": "corr3ct-h0rse-batt",
}


def _register(client: APIClient) -> User:
    response = client.post(reverse("register"), REGISTRATION, format="json")
    assert response.status_code == 201
    return User.objects.get(username="newcomer")


def test_registration_opens_a_funded_checking_and_an_empty_savings(api_client: APIClient) -> None:
    user = _register(api_client)

    checking = Account.objects.get(owner=user, name=CHECKING_NAME)
    savings = Account.objects.get(owner=user, name=SAVINGS_NAME)

    assert checking.account_type == AccountType.ASSET
    assert savings.account_type == AccountType.ASSET
    assert get_balance(checking) == Decimal("1000.0000")
    # Empty on purpose: it makes "move some to Savings" a real first thing to do.
    assert get_balance(savings) == Decimal("0.0000")


def test_the_opening_deposit_is_a_balanced_entry(api_client: APIClient) -> None:
    """Money does not appear. It is debited from the customer's own equity account."""
    user = _register(api_client)

    opening = Account.objects.get(owner=user, name=OPENING_BALANCES_NAME)
    assert opening.account_type == AccountType.EQUITY
    assert get_balance(opening) == Decimal("-1000.0000")


def test_the_new_accounts_are_the_ones_the_api_lists(api_client: APIClient) -> None:
    """The dashboard shows two accounts, and the equity counterparty is not one of them."""
    _register(api_client)

    tokens = api_client.post(
        reverse("token_obtain_pair"),
        {"username": REGISTRATION["username"], "password": REGISTRATION["password"]},
        format="json",
    ).json()
    api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")

    listed = api_client.get(reverse("account-list")).json()["results"]

    assert {row["name"] for row in listed} == {CHECKING_NAME, SAVINGS_NAME}
    # Numbered, because these are accounts a customer holds (ADR-0027).
    assert all(row["number"] for row in listed)


def test_opening_accounts_is_audited(api_client: APIClient) -> None:
    user = _register(api_client)

    event = AuditEvent.objects.get(action=AuditAction.ACCOUNTS_OPENED, actor=user)

    assert event.context["opening_deposit"] == "1000.0000"
    assert len(event.context["accounts"]) == 2


def test_a_failed_registration_leaves_no_accounts_behind(api_client: APIClient) -> None:
    """One transaction: a user without accounts is the state onboarding exists to prevent."""
    api_client.post(reverse("register"), REGISTRATION, format="json")

    duplicate = api_client.post(
        reverse("register"),
        {**REGISTRATION, "email": "different@example.com"},
        format="json",
    )

    assert duplicate.status_code == 400
    assert Account.objects.filter(name=CHECKING_NAME).count() == 1


def test_a_zero_deposit_opens_the_accounts_empty(api_client: APIClient, settings) -> None:  # type: ignore[no-untyped-def]
    """The escape hatch Week 9's seed script needs: accounts, no deposit, no equity account."""
    settings.ONBOARDING_OPENING_DEPOSIT = Decimal("0.0000")

    user = _register(api_client)

    assert get_balance(Account.objects.get(owner=user, name=CHECKING_NAME)) == Decimal("0.0000")
    assert not Account.objects.filter(owner=user, name=OPENING_BALANCES_NAME).exists()
