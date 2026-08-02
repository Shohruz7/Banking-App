"""Order validation, and the small pieces that hold the rest together.

Every rule here lives in ``trading.services`` rather than in the serializer, so it holds for the
Celery sweep and the admin as well as for HTTP — the serializer catches the same two shape errors
earlier only so the client gets a field-level 400 instead of a domain exception.
"""

from decimal import Decimal

import pytest
from django.contrib.auth.models import User
from django.urls import reverse
from rest_framework.test import APIClient

from accounts.models import Account
from markets.models import Instrument, PriceTick
from tests.factories import InstrumentFactory, OrderFactory, give_shares
from trading.exceptions import InvalidOrderError
from trading.models import Order, OrderSide, OrderStatus, OrderType
from trading.services import place_order

pytestmark = pytest.mark.django_db


def order(user: User, instrument: Instrument, cash: Account, **overrides: object) -> Order:
    kwargs: dict[str, object] = {
        "user": user,
        "instrument": instrument,
        "cash_account": cash,
        "side": OrderSide.BUY,
        "order_type": OrderType.MARKET,
        "quantity": Decimal("1"),
    }
    kwargs.update(overrides)
    return place_order(**kwargs)  # type: ignore[arg-type]


def test_a_non_positive_quantity_is_refused(
    password_user: User, instrument: Instrument, funded_cash_account: Account
) -> None:
    with pytest.raises(InvalidOrderError, match="must be positive"):
        order(password_user, instrument, funded_cash_account, quantity=Decimal("0"))

    with pytest.raises(InvalidOrderError, match="must be positive"):
        order(password_user, instrument, funded_cash_account, quantity=Decimal("-5"))


def test_a_quantity_that_rounds_away_is_refused(
    password_user: User, instrument: Instrument, funded_cash_account: Account
) -> None:
    """Below the share quantum there is nothing left to buy."""
    with pytest.raises(InvalidOrderError, match="must be positive"):
        order(password_user, instrument, funded_cash_account, quantity=Decimal("0.000000001"))


def test_a_limit_order_without_a_price_is_refused_by_the_service(
    password_user: User, instrument: Instrument, funded_cash_account: Account
) -> None:
    """The serializer catches this first over HTTP; the service is what makes it true everywhere."""
    with pytest.raises(InvalidOrderError, match="needs a limit price"):
        order(password_user, instrument, funded_cash_account, order_type=OrderType.LIMIT)


def test_a_non_positive_limit_price_is_refused(
    password_user: User, instrument: Instrument, funded_cash_account: Account
) -> None:
    with pytest.raises(InvalidOrderError, match="Limit price must be positive"):
        order(
            password_user,
            instrument,
            funded_cash_account,
            order_type=OrderType.LIMIT,
            limit_price=Decimal("0"),
        )


def test_a_limit_price_on_a_market_order_is_ignored(
    password_user: User, instrument: Instrument, funded_cash_account: Account
) -> None:
    """A market order fills at the market; the DB constraint requires the column be NULL."""
    placed = order(password_user, instrument, funded_cash_account, limit_price=Decimal("42"))

    assert placed.limit_price is None
    assert placed.status == OrderStatus.FILLED


def test_a_sell_whose_cost_basis_rounds_to_zero_is_refused(
    password_user: User, funded_cash_account: Account
) -> None:
    """A 4-decimal ledger cannot express removing this much basis, so the trade is refused.

    Rounding the cost up to the money quantum would invent basis; rounding it to zero would post a
    line the zero-amount rule rejects. Refusing is the only honest answer.
    """
    dust = InstrumentFactory.create(symbol="DUST", initial_price=Decimal("100.0000"))
    # A million shares that cost a hundredth of a cent in total.
    give_shares(password_user, dust, Decimal("1000000"), Decimal("0.0001"))

    with pytest.raises(InvalidOrderError, match="cost basis"):
        order(
            password_user,
            dust,
            funded_cash_account,
            side=OrderSide.SELL,
            quantity=Decimal("1"),
        )


def test_crosses_is_unconditional_without_a_limit() -> None:
    """A market order has no limit, so any price is acceptable to it."""
    unlimited = OrderFactory.build(order_type=OrderType.MARKET, limit_price=None)
    assert unlimited.crosses(Decimal("0.01"))
    assert unlimited.crosses(Decimal("999999"))


def test_crosses_respects_the_side() -> None:
    """The limit is the worst price the client accepts, in the direction their side cares about."""
    buy = OrderFactory.build(side=OrderSide.BUY, limit_price=Decimal("100"))
    assert buy.crosses(Decimal("99.9999"))
    assert buy.crosses(Decimal("100"))
    assert not buy.crosses(Decimal("100.0001"))

    sell = OrderFactory.build(side=OrderSide.SELL, limit_price=Decimal("100"))
    assert sell.crosses(Decimal("100.0001"))
    assert sell.crosses(Decimal("100"))
    assert not sell.crosses(Decimal("99.9999"))


def test_model_reprs_are_readable(instrument: Instrument) -> None:
    """These show up in the admin, in logs, and in every failed-assertion message."""
    assert str(instrument) == "TEST (Test Corp.)"
    assert "100.0000" in str(PriceTick(instrument=instrument, price=Decimal("100.0000")))
    assert "buy" in str(OrderFactory.build(side=OrderSide.BUY))


def test_market_order_with_a_limit_price_is_a_field_error_over_http(
    auth_client: APIClient, instrument: Instrument, funded_cash_account: Account
) -> None:
    """The serializer's other shape rule, so the client is told which field is wrong."""
    response = auth_client.post(
        reverse("order-list-create"),
        {
            "symbol": instrument.symbol,
            "cash_account": str(funded_cash_account.pk),
            "side": "buy",
            "order_type": "market",
            "quantity": "1",
            "limit_price": "90",
        },
        format="json",
    )

    assert response.status_code == 400
    assert "limit_price" in response.json()["error"]["details"]


def test_an_order_too_small_to_post_uses_the_error_envelope(
    auth_client: APIClient, funded_cash_account: Account
) -> None:
    """The InvalidOrder branch of the view, end to end."""
    penny = InstrumentFactory.create(symbol="PENY", initial_price=Decimal("0.0100"))

    response = auth_client.post(
        reverse("order-list-create"),
        {
            "symbol": penny.symbol,
            "cash_account": str(funded_cash_account.pk),
            "side": "buy",
            "order_type": "market",
            "quantity": "0.00000001",
        },
        format="json",
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_order"


def test_order_detail_returns_the_requesters_own_order(
    auth_client: APIClient, instrument: Instrument, funded_cash_account: Account
) -> None:
    created = auth_client.post(
        reverse("order-list-create"),
        {
            "symbol": instrument.symbol,
            "cash_account": str(funded_cash_account.pk),
            "side": "buy",
            "order_type": "market",
            "quantity": "1",
        },
        format="json",
    ).json()

    fetched = auth_client.get(reverse("order-detail", args=[created["id"]]))

    assert fetched.status_code == 200
    assert fetched.json()["id"] == created["id"]
    assert fetched.json()["status"] == "filled"
