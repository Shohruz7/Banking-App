"""Which accounts a customer may see, spend from, and receive into.

``AccountQuerySet.cash()`` means "not instrument-backed", which is a narrower claim than it reads
like: it admits the *equity* opening-balances account that funding posts against and the *income*
realized-P&L account a sell creates. Both are the other side of a user's own money, not money.

``trading.portfolio.cash_balance_for`` and ``statements.services`` had always spelled the extra
``account_type=ASSET`` filter out by hand. The four endpoints had not, and the gap was not cosmetic:
a realized *loss* leaves the income account with a **positive** balance (``_sell_lines`` posts the
residual as ``amount=-gain``), and the transfer endpoint scoped its source by owner alone. Selling
at a loss and then transferring out of the P&L account turned that loss into spendable cash.
"""

from decimal import Decimal

import pytest
from django.contrib.auth.models import User
from django.urls import reverse
from rest_framework.test import APIClient

from accounts.models import Account, AccountType
from ledger.services import get_balance
from markets.models import Instrument
from trading.services import REALIZED_PNL_ACCOUNT_NAME, place_order

from .factories import AccountFactory, give_shares

pytestmark = pytest.mark.django_db


def _sell_at_a_loss(user: User, instrument: Instrument, cash: Account) -> Account:
    """Ten shares bought at 200, sold at the fixture's flat 100 — a realized loss of 1,000."""
    give_shares(user, instrument, Decimal("10.00000000"), Decimal("2000.0000"))
    place_order(
        user=user,
        instrument=instrument,
        cash_account=cash,
        side="sell",
        order_type="market",
        quantity=Decimal("10.00000000"),
    )
    return Account.objects.get(
        owner=user, name=REALIZED_PNL_ACCOUNT_NAME, account_type=AccountType.INCOME
    )


def test_a_realized_loss_cannot_be_transferred_out_as_cash(
    auth_client: APIClient,
    password_user: User,
    instrument: Instrument,
    funded_cash_account: Account,
) -> None:
    """The bug this module exists for.

    Without the ``spendable()`` scope the transfer posts 201 and the user has minted $1,000 out of
    a loss.
    """
    pnl = _sell_at_a_loss(password_user, instrument, funded_cash_account)
    assert get_balance(pnl) == Decimal("1000.0000"), "a loss sits positive in income"

    response = auth_client.post(
        reverse("transfer-create"),
        {
            "source_account": str(pnl.pk),
            "destination_account": str(funded_cash_account.pk),
            "amount": "1000.0000",
        },
        format="json",
    )

    assert response.status_code == 404
    assert get_balance(pnl) == Decimal("1000.0000")


def test_bookkeeping_accounts_are_not_listed(
    auth_client: APIClient,
    password_user: User,
    instrument: Instrument,
    funded_cash_account: Account,
) -> None:
    """The dashboard lists money, not the double-entry counterparties that fund it."""
    _sell_at_a_loss(password_user, instrument, funded_cash_account)

    listed = {row["name"] for row in auth_client.get(reverse("account-list")).json()["results"]}

    assert "Brokerage cash" in listed
    assert REALIZED_PNL_ACCOUNT_NAME not in listed
    assert "Opening balances" not in listed


def test_a_bookkeeping_account_has_no_readable_history(
    auth_client: APIClient, password_user: User
) -> None:
    """Its id is no longer discoverable through the list, and it 404s if guessed."""
    equity = AccountFactory.create(
        owner=password_user, name="Opening balances", account_type=AccountType.EQUITY
    )

    response = auth_client.get(reverse("account-transactions", args=[equity.pk]))

    assert response.status_code == 404


def test_a_bookkeeping_account_cannot_fund_an_order(
    auth_client: APIClient, password_user: User, instrument: Instrument
) -> None:
    """The same hole, through the brokerage door rather than the transfer one."""
    equity = AccountFactory.create(
        owner=password_user, name="Opening balances", account_type=AccountType.EQUITY
    )

    response = auth_client.post(
        reverse("order-list-create"),
        {
            "symbol": instrument.symbol,
            "cash_account": str(equity.pk),
            "side": "buy",
            "order_type": "market",
            "quantity": "1.00000000",
        },
        format="json",
    )

    assert response.status_code == 404


def test_money_cannot_be_transferred_into_a_bookkeeping_account(
    auth_client: APIClient, password_user: User, funded_cash_account: Account
) -> None:
    """Destination is not owner-scoped — you can pay another user — but it is still money-scoped.

    400 rather than the 404 the *source* gets, and the asymmetry is pre-existing: a source you
    cannot see is indistinguishable from one that does not exist, so it 404s, while a destination
    has always answered ``DestinationNotFound``. Asserted here as the existing contract.
    """
    equity = AccountFactory.create(
        owner=password_user, name="Opening balances", account_type=AccountType.EQUITY
    )

    response = auth_client.post(
        reverse("transfer-create"),
        {
            "source_account": str(funded_cash_account.pk),
            "destination_account": str(equity.pk),
            "amount": "10.0000",
        },
        format="json",
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "destination_not_found"
