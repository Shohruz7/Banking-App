"""Limit orders: rest until the market crosses, then fill on their own (ADR-0018).

Prices are always scripted or set by hand here. Asserting "the order filled" against a live random
walk would be a coin flip dressed up as a test.
"""

from decimal import Decimal

import pytest
from django.contrib.auth.models import User

from accounts.models import Account
from ledger.models import JournalEntry
from ledger.services import get_balance, get_quantity, transfer
from markets.models import Instrument
from tests.factories import AccountFactory, give_shares
from trading.models import Order, OrderSide, OrderStatus, OrderType
from trading.services import cancel_order, place_order, position_account_for
from trading.tasks import match_resting_orders

pytestmark = pytest.mark.django_db


def rest_buy(user: User, instrument: Instrument, cash: Account, quantity: str, limit: str) -> Order:
    return place_order(
        user=user,
        instrument=instrument,
        cash_account=cash,
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Decimal(quantity),
        limit_price=Decimal(limit),
    )


def move_market_to(instrument: Instrument, price: str) -> None:
    instrument.last_price = Decimal(price)
    instrument.save(update_fields=["last_price"])


def test_limit_buy_rests_and_posts_nothing(
    password_user: User, instrument: Instrument, funded_cash_account: Account
) -> None:
    """A limit order is an intention, not a movement: no entry until the market cooperates."""
    order = rest_buy(password_user, instrument, funded_cash_account, "5", "90")

    assert order.status == OrderStatus.OPEN
    assert order.entry is None
    # Only the opening-balance entry from the fixture exists.
    assert JournalEntry.objects.count() == 1
    assert get_balance(funded_cash_account) == Decimal("10000.0000")


def test_limit_buy_fills_when_the_price_crosses(
    password_user: User, instrument: Instrument, funded_cash_account: Account
) -> None:
    """$90 buy against a $100 market: nothing, then the market drops and it fills unattended."""
    order = rest_buy(password_user, instrument, funded_cash_account, "5", "90")

    move_market_to(instrument, "95")
    assert match_resting_orders() == {"filled": 0, "rejected": 0, "skipped": 1}
    order.refresh_from_db()
    assert order.status == OrderStatus.OPEN

    move_market_to(instrument, "88")
    assert match_resting_orders() == {"filled": 1, "rejected": 0, "skipped": 0}

    order.refresh_from_db()
    assert order.status == OrderStatus.FILLED
    # Filled at the market, not at the limit — the limit is the worst price accepted, not the price.
    assert order.filled_price == Decimal("88.0000")
    assert get_quantity(position_account_for(password_user, instrument)) == Decimal("5.00000000")
    assert get_balance(funded_cash_account) == Decimal("9560.0000")


def test_limit_sell_fills_only_above_its_limit(
    password_user: User, instrument: Instrument, funded_cash_account: Account
) -> None:
    """The mirror direction: a sell wants the price at or *above* its limit."""
    give_shares(password_user, instrument, Decimal("4"), Decimal("400.00"))
    order = place_order(
        user=password_user,
        instrument=instrument,
        cash_account=funded_cash_account,
        side=OrderSide.SELL,
        order_type=OrderType.LIMIT,
        quantity=Decimal("4"),
        limit_price=Decimal("120"),
    )

    move_market_to(instrument, "119.9999")
    match_resting_orders()
    order.refresh_from_db()
    assert order.status == OrderStatus.OPEN

    move_market_to(instrument, "120")
    match_resting_orders()
    order.refresh_from_db()
    assert order.status == OrderStatus.FILLED


def test_resting_order_is_rejected_when_the_cash_was_spent(
    password_user: User, instrument: Instrument, funded_cash_account: Account
) -> None:
    """A resting order reserves nothing (ADR-0018), so it can become unaffordable while it waits.

    The honest consequence of not implementing held funds, asserted rather than left implicit.
    """
    rest_buy(password_user, instrument, funded_cash_account, "50", "90")

    # Spend the money elsewhere before the market ever crosses.
    elsewhere = AccountFactory.create(owner=password_user)
    transfer(source=funded_cash_account, destination=elsewhere, amount=Decimal("9999.00"))

    move_market_to(instrument, "88")
    assert match_resting_orders() == {"filled": 0, "rejected": 1, "skipped": 0}

    order = Order.objects.get(user=password_user)
    assert order.status == OrderStatus.REJECTED
    assert order.reject_reason == "insufficient_funds"
    assert order.entry is None


def test_cancelled_order_never_fills(
    password_user: User, instrument: Instrument, funded_cash_account: Account
) -> None:
    """Cancellation is final: the sweep skips it even once the market crosses."""
    order = rest_buy(password_user, instrument, funded_cash_account, "5", "90")
    cancel_order(order, actor=password_user)

    move_market_to(instrument, "50")
    assert match_resting_orders() == {"filled": 0, "rejected": 0, "skipped": 0}

    order.refresh_from_db()
    assert order.status == OrderStatus.CANCELLED
    assert order.entry is None


def test_cancelling_a_filled_order_is_refused(
    password_user: User, instrument: Instrument, funded_cash_account: Account
) -> None:
    """Only an open order can be withdrawn; a filled one has already moved money."""
    from trading.exceptions import OrderNotOpenError

    order = place_order(
        user=password_user,
        instrument=instrument,
        cash_account=funded_cash_account,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal("1"),
    )

    with pytest.raises(OrderNotOpenError):
        cancel_order(order, actor=password_user)


def test_a_sweep_with_nothing_resting_is_a_no_op(instrument: Instrument) -> None:
    """The common case in production: Beat fires, no orders are open, nothing happens."""
    assert match_resting_orders() == {"filled": 0, "rejected": 0, "skipped": 0}
