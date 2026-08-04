"""Portfolio valuation (ADR-0020).

Every figure the portfolio reports is derived from ledger rows at read time. The headline test
here computes the total a second way — by summing account balances directly — rather than by
calling the code under test with different arguments, because a derivation that agrees with itself
proves nothing.
"""

from decimal import Decimal

import pytest
from django.contrib.auth.models import User
from django.urls import reverse
from rest_framework.test import APIClient

from accounts.models import Account, AccountType
from ledger.services import get_balance
from markets.models import Instrument
from tests.factories import AccountFactory, InstrumentFactory, fund_account, give_shares
from trading.portfolio import cash_balance_for, portfolio_for, realized_pnl_for
from trading.services import place_order

pytestmark = pytest.mark.django_db


def test_an_untouched_account_has_an_empty_portfolio(password_user: User) -> None:
    portfolio = portfolio_for(password_user)

    assert portfolio.cash == Decimal("0.0000")
    assert portfolio.holdings_value == Decimal("0.0000")
    assert portfolio.total_value == Decimal("0.0000")
    assert portfolio.realized_pnl == Decimal("0.0000")
    assert portfolio.positions == []


def test_cash_excludes_the_equity_and_income_accounts(
    password_user: User, funded_cash_account: Account
) -> None:
    """The whole reason ``cash_balance_for`` exists.

    ``fund_account`` posts against an opening-balances *equity* account holding −10,000. Summing
    every non-position account — which is what ``AccountQuerySet.cash()`` means — would report a
    portfolio of zero for a user with $10,000 in it: impeccable double-entry, useless answer.
    """
    assert Account.objects.filter(owner=password_user).cash().count() == 2
    assert cash_balance_for(password_user) == Decimal("10000.0000")


def test_holdings_value_and_unrealized_pnl_track_the_market(
    password_user: User, instrument: Instrument
) -> None:
    """Cost basis is what was paid; market value is what it is worth now; the gap is unrealized."""
    give_shares(password_user, instrument, Decimal("10"), Decimal("1000.0000"))

    at_cost = portfolio_for(password_user)
    assert at_cost.cost_basis == Decimal("1000.0000")
    assert at_cost.holdings_value == Decimal("1000.0000")
    assert at_cost.unrealized_pnl == Decimal("0.0000")

    instrument.last_price = Decimal("120.0000")
    instrument.save(update_fields=["last_price"])

    after = portfolio_for(password_user)
    assert after.holdings_value == Decimal("1200.0000")
    assert after.unrealized_pnl == Decimal("200.0000")
    assert after.positions[0].unrealized_pnl == Decimal("200.0000")
    assert after.positions[0].average_cost == Decimal("100.0000")


def test_a_fully_sold_position_is_not_a_holding(
    password_user: User, instrument: Instrument, funded_cash_account: Account
) -> None:
    """An account with zero shares is history. It stays on the books; it is not in the portfolio."""
    give_shares(password_user, instrument, Decimal("4"), Decimal("400.0000"))
    place_order(
        user=password_user,
        instrument=instrument,
        cash_account=funded_cash_account,
        side="sell",
        order_type="market",
        quantity=Decimal("4"),
    )

    portfolio = portfolio_for(password_user)
    assert portfolio.positions == []
    assert portfolio.holdings_value == Decimal("0.0000")
    # The position account is still there, holding nothing.
    assert Account.objects.filter(owner=password_user, instrument=instrument).exists()


def test_realized_pnl_is_reported_in_the_sign_a_human_expects(
    password_user: User, instrument: Instrument, funded_cash_account: Account
) -> None:
    """A gain reads positive here and negative in the ledger, and both are correct.

    ``_sell_lines`` posts the residual as ``amount=-gain``, so the income account carries −200 for
    a $200 profit. Getting this backwards would print a loss on every profitable statement.
    """
    give_shares(password_user, instrument, Decimal("4"), Decimal("400.0000"))
    instrument.last_price = Decimal("150.0000")
    instrument.save(update_fields=["last_price"])

    place_order(
        user=password_user,
        instrument=instrument,
        cash_account=funded_cash_account,
        side="sell",
        order_type="market",
        quantity=Decimal("4"),
    )

    pnl_account = Account.objects.get(
        owner=password_user, account_type=AccountType.INCOME, name="Realized P&L"
    )
    assert get_balance(pnl_account) == Decimal("-200.0000")
    assert realized_pnl_for(password_user) == Decimal("200.0000")
    assert portfolio_for(password_user).realized_pnl == Decimal("200.0000")


def test_realized_pnl_is_zero_when_the_account_was_never_created(password_user: User) -> None:
    """Reading a portfolio must not create an account for someone who has never sold anything."""
    assert realized_pnl_for(password_user) == Decimal("0.0000")
    assert not Account.objects.filter(owner=password_user, name="Realized P&L").exists()


def test_total_value_equals_the_sum_of_everything_owned(
    password_user: User, funded_cash_account: Account
) -> None:
    """The headline property, checked against an independent sum rather than the same code path."""
    apple = InstrumentFactory.create(symbol="AAPL", initial_price=Decimal("195.0000"))
    tesla = InstrumentFactory.create(symbol="TSLA", initial_price=Decimal("240.0000"))
    savings = AccountFactory.create(owner=password_user, name="Savings")
    fund_account(savings, Decimal("2500.0000"))

    place_order(
        user=password_user,
        instrument=apple,
        cash_account=funded_cash_account,
        side="buy",
        order_type="market",
        quantity=Decimal("3"),
    )
    place_order(
        user=password_user,
        instrument=tesla,
        cash_account=funded_cash_account,
        side="buy",
        order_type="market",
        quantity=Decimal("1.5"),
    )
    apple.last_price = Decimal("201.5000")
    apple.save(update_fields=["last_price"])

    expected_cash = sum(
        (
            get_balance(account)
            for account in Account.objects.filter(
                owner=password_user, account_type=AccountType.ASSET
            ).cash()
        ),
        start=Decimal("0.0000"),
    )
    expected_holdings = Decimal("3") * Decimal("201.5000") + Decimal("1.5") * Decimal("240.0000")

    portfolio = portfolio_for(password_user)
    assert portfolio.cash == expected_cash
    assert portfolio.holdings_value == expected_holdings
    assert portfolio.total_value == expected_cash + expected_holdings


def test_one_users_holdings_never_appear_in_anothers_portfolio(
    password_user: User, instrument: Instrument
) -> None:
    give_shares(password_user, instrument, Decimal("5"), Decimal("500.0000"))
    stranger = AccountFactory.create().owner

    assert portfolio_for(stranger).positions == []
    assert portfolio_for(stranger).total_value == Decimal("0.0000")


# --------------------------------------------------------------------------------------------
# Over HTTP
# --------------------------------------------------------------------------------------------


def test_portfolio_endpoint_serializes_money_as_strings(
    auth_client: APIClient, password_user: User, instrument: Instrument
) -> None:
    """ADR-0009 all the way to the wire: a JavaScript client cannot round these to floats."""
    give_shares(password_user, instrument, Decimal("2.5"), Decimal("250.0000"))

    response = auth_client.get(reverse("portfolio"))

    assert response.status_code == 200
    body = response.json()
    assert body["holdings_value"] == "250.0000"
    assert body["total_value"] == "250.0000"
    assert body["realized_pnl"] == "0.0000"
    assert body["positions"][0]["symbol"] == instrument.symbol
    assert body["positions"][0]["quantity"] == "2.50000000"
    assert body["positions"][0]["unrealized_pnl"] == "0.0000"


def test_portfolio_endpoint_requires_authentication(api_client: APIClient) -> None:
    assert api_client.get(reverse("portfolio")).status_code == 401


def test_holdings_endpoint_now_reports_unrealized_pnl(
    auth_client: APIClient, password_user: User, instrument: Instrument
) -> None:
    """The same rows the portfolio uses, through the endpoint that predates it."""
    give_shares(password_user, instrument, Decimal("2"), Decimal("200.0000"))
    instrument.last_price = Decimal("110.0000")
    instrument.save(update_fields=["last_price"])

    body = auth_client.get(reverse("holdings")).json()

    assert body[0]["market_value"] == "220.0000"
    assert body[0]["unrealized_pnl"] == "20.0000"
