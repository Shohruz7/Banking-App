"""What actually comes down the socket (ADR-0023).

Two properties carry this file. Personal events reach exactly one user's socket — a fill is a fact
about someone's money, and delivering it to the wrong group is a data breach with a websocket in
front of it. And market data is gated by *subscription*, so the fan-out of a 57-symbol market is
bounded by what clients asked for rather than by how many are connected.
"""

from decimal import Decimal
from typing import Any

import pytest
from channels.db import database_sync_to_async
from channels.testing import WebsocketCommunicator
from django.contrib.auth.models import User
from rest_framework.test import APIClient

from accounts.models import Account
from config.asgi import application
from ledger.services import transfer
from markets.models import Instrument
from markets.pricing import FixedPriceSource
from markets.tasks import advance_prices
from realtime.consumers import CLOSE_TOO_MANY_SUBSCRIPTIONS
from tests.conftest import obtain_tokens
from tests.factories import AccountFactory, InstrumentFactory, UserFactory, fund_account
from trading.services import cancel_order, place_order

pytestmark = [pytest.mark.asyncio, pytest.mark.django_db(transaction=True)]

STREAM_PATH = "/ws/v1/stream/"
LOCAL_ORIGIN = [(b"origin", b"http://localhost")]


@database_sync_to_async
def make_trader(symbol: str = "TEST") -> tuple[User, Account, Instrument]:
    user = UserFactory.create()
    cash = AccountFactory.create(owner=user, name="Brokerage cash")
    fund_account(cash, Decimal("10000.0000"))
    return user, cash, InstrumentFactory.create(symbol=symbol)


@database_sync_to_async
def access_token_for(user: User) -> str:
    return str(obtain_tokens(APIClient(), user)["access"])


async def authenticated_socket(user: User) -> WebsocketCommunicator:
    communicator = WebsocketCommunicator(application, STREAM_PATH, headers=LOCAL_ORIGIN)
    connected, _ = await communicator.connect()
    assert connected
    await communicator.send_json_to({"type": "auth", "token": await access_token_for(user)})
    assert (await communicator.receive_json_from())["type"] == "auth.ok"
    return communicator


async def collect(communicator: WebsocketCommunicator, count: int) -> list[dict[str, Any]]:
    return [dict(await communicator.receive_json_from(timeout=3)) for _ in range(count)]


# --------------------------------------------------------------------------------------------
# Personal events
# --------------------------------------------------------------------------------------------


async def test_a_fill_reaches_the_trader_with_its_new_balance() -> None:
    user, cash, instrument = await make_trader()
    socket = await authenticated_socket(user)

    await database_sync_to_async(place_order)(
        user=user,
        instrument=instrument,
        cash_account=cash,
        side="buy",
        order_type="market",
        quantity=Decimal("3"),
    )

    fill, balance = await collect(socket, 2)
    assert fill["type"] == "order.filled"
    assert fill["symbol"] == instrument.symbol
    assert fill["side"] == "buy"
    # ADR-0009 holds on the socket too: money is a string, never a float.
    assert fill["price"] == "100.0000"
    assert fill["quantity"] == "3.00000000"
    assert fill["notional"] == "300.0000"
    assert balance["type"] == "balance.updated"
    assert balance["account_id"] == str(cash.pk)
    assert balance["balance"] == "9700.0000"
    await socket.disconnect()


async def test_a_fill_never_reaches_another_users_socket() -> None:
    """One group per user. Getting this wrong is a data breach with a websocket in front of it."""
    user, cash, instrument = await make_trader()
    stranger = await database_sync_to_async(UserFactory.create)()
    eavesdropper = await authenticated_socket(stranger)

    await database_sync_to_async(place_order)(
        user=user,
        instrument=instrument,
        cash_account=cash,
        side="buy",
        order_type="market",
        quantity=Decimal("1"),
    )

    assert await eavesdropper.receive_nothing(timeout=0.5) is True
    await eavesdropper.disconnect()


async def test_a_cancelled_order_is_announced() -> None:
    user, cash, instrument = await make_trader()
    order = await database_sync_to_async(place_order)(
        user=user,
        instrument=instrument,
        cash_account=cash,
        side="buy",
        order_type="limit",
        quantity=Decimal("1"),
        limit_price=Decimal("1.0000"),
    )
    socket = await authenticated_socket(user)

    await database_sync_to_async(cancel_order)(order, actor=user)

    event = (await collect(socket, 1))[0]
    assert event["type"] == "order.cancelled"
    assert event["order_id"] == str(order.pk)
    await socket.disconnect()


async def test_both_sides_of_a_transfer_are_told() -> None:
    """Money arriving is the recipient's news as much as the sender's."""
    sender, source, _ = await make_trader()
    recipient = await database_sync_to_async(UserFactory.create)()
    destination = await database_sync_to_async(AccountFactory.create)(
        owner=recipient, name="Their account"
    )
    sender_socket = await authenticated_socket(sender)
    recipient_socket = await authenticated_socket(recipient)

    await database_sync_to_async(transfer)(
        source=source, destination=destination, amount=Decimal("25.0000"), actor=sender
    )

    sender_events = {event["type"] for event in await collect(sender_socket, 2)}
    recipient_events = {event["type"] for event in await collect(recipient_socket, 2)}
    assert sender_events == {"balance.updated", "transfer.posted"}
    assert recipient_events == {"balance.updated", "transfer.posted"}
    await sender_socket.disconnect()
    await recipient_socket.disconnect()


# --------------------------------------------------------------------------------------------
# Market data
# --------------------------------------------------------------------------------------------


async def test_price_ticks_arrive_only_for_subscribed_symbols() -> None:
    user, _, watched = await make_trader(symbol="WATCH")
    ignored = await database_sync_to_async(InstrumentFactory.create)(symbol="IGNORE")
    socket = await authenticated_socket(user)

    await socket.send_json_to({"type": "subscribe", "symbols": ["watch"]})
    confirmation = await socket.receive_json_from()
    assert confirmation == {"type": "subscribed", "symbols": ["WATCH"], "unknown": []}

    await database_sync_to_async(advance_prices)(source=FixedPriceSource(Decimal("123.4500")))

    tick = await socket.receive_json_from(timeout=3)
    assert tick["type"] == "price.tick"
    assert tick["symbol"] == watched.symbol
    assert tick["price"] == "123.4500"
    # The other instrument ticked too; this socket never joined its group.
    assert await socket.receive_nothing(timeout=0.5) is True
    assert ignored.symbol != watched.symbol
    await socket.disconnect()


async def test_unsubscribing_stops_the_ticks() -> None:
    user, _, instrument = await make_trader(symbol="WATCH")
    socket = await authenticated_socket(user)
    await socket.send_json_to({"type": "subscribe", "symbols": [instrument.symbol]})
    await socket.receive_json_from()

    await socket.send_json_to({"type": "unsubscribe", "symbols": [instrument.symbol]})
    assert (await socket.receive_json_from())["symbols"] == []

    await database_sync_to_async(advance_prices)(source=FixedPriceSource(Decimal("101.0000")))

    assert await socket.receive_nothing(timeout=0.5) is True
    await socket.disconnect()


async def test_an_unknown_symbol_is_reported_rather_than_silently_dropped() -> None:
    """A client watching a symbol delisted this morning should be told, not left waiting."""
    user, _, instrument = await make_trader(symbol="REAL")
    socket = await authenticated_socket(user)

    await socket.send_json_to({"type": "subscribe", "symbols": [instrument.symbol, "NOPE"]})

    confirmation = await socket.receive_json_from()
    assert confirmation["symbols"] == ["REAL"]
    assert confirmation["unknown"] == ["NOPE"]
    await socket.disconnect()


async def test_subscribing_beyond_the_ceiling_closes_the_socket(settings: Any) -> None:
    settings.WS_MAX_SUBSCRIPTIONS = 2
    user, _, _ = await make_trader(symbol="AAA")
    await database_sync_to_async(InstrumentFactory.create)(symbol="BBB")
    await database_sync_to_async(InstrumentFactory.create)(symbol="CCC")
    socket = await authenticated_socket(user)

    await socket.send_json_to({"type": "subscribe", "symbols": ["AAA", "BBB", "CCC"]})

    message = await socket.receive_output(timeout=3)
    assert message["type"] == "websocket.close"
    assert message["code"] == CLOSE_TOO_MANY_SUBSCRIPTIONS


async def test_a_delisted_symbol_cannot_be_subscribed_to() -> None:
    """Group names go to Redis; letting a client mint arbitrary ones is not a thing to allow."""
    user, _, instrument = await make_trader(symbol="GONE")
    await database_sync_to_async(
        lambda: Instrument.objects.filter(pk=instrument.pk).update(is_active=False)
    )()
    socket = await authenticated_socket(user)

    await socket.send_json_to({"type": "subscribe", "symbols": ["GONE"]})

    confirmation = await socket.receive_json_from()
    assert confirmation["symbols"] == []
    assert confirmation["unknown"] == ["GONE"]
    await socket.disconnect()


# --------------------------------------------------------------------------------------------
# Protocol housekeeping
# --------------------------------------------------------------------------------------------


async def test_ping_is_answered() -> None:
    user, _, _ = await make_trader()
    socket = await authenticated_socket(user)

    await socket.send_json_to({"type": "ping"})

    assert (await socket.receive_json_from())["type"] == "pong"
    await socket.disconnect()


async def test_an_unknown_message_is_an_error_not_a_disconnect() -> None:
    """A client sending nonsense on an authenticated socket has a bug, not a stolen token."""
    user, _, _ = await make_trader()
    socket = await authenticated_socket(user)

    await socket.send_json_to({"type": "teleport"})

    reply = await socket.receive_json_from()
    assert reply["type"] == "error"
    assert reply["code"] == "unknown_message"
    await socket.disconnect()


async def test_a_malformed_symbol_list_subscribes_to_nothing() -> None:
    user, _, _ = await make_trader()
    socket = await authenticated_socket(user)

    await socket.send_json_to({"type": "subscribe", "symbols": "AAPL"})

    assert (await socket.receive_json_from())["symbols"] == []
    await socket.disconnect()
