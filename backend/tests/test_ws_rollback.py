"""Nothing is announced that did not happen (ADR-0023).

This is the file the real-time track exists to earn. A balance pushed from inside a transaction
that later aborts is a number the client keeps showing until it reloads, and a fill announced for
an order that rolled back is a lie the server told about someone's money.

Everything is asserted at ``realtime.events._send`` — the last function before the channel layer —
so what is being checked is what was *actually transmitted*, not what a service intended.

Each test here was confirmed to fail with ``transaction.on_commit`` removed from
``realtime.events.publish``; that is the only reason to trust any of them.
"""

from collections.abc import Iterator
from decimal import Decimal
from unittest import mock

import pytest
from django.contrib.auth.models import User
from django.db import transaction

from accounts.models import Account
from ledger.exceptions import InsufficientFundsError
from ledger.services import transfer
from markets.models import Instrument
from realtime import events
from tests.factories import AccountFactory, UserFactory, fund_account
from trading.services import place_order

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def sent() -> Iterator[mock.MagicMock]:
    """Every message that reached the channel layer, and nothing else."""
    with mock.patch.object(events, "_send") as send:
        yield send


def types_in(send: mock.MagicMock) -> list[str]:
    """The event type of each transmitted message."""
    return [
        call.args[1].get("payload", {}).get("type", call.args[1]["type"])
        for call in send.call_args_list
    ]


# --------------------------------------------------------------------------------------------
# The mechanism
# --------------------------------------------------------------------------------------------


def test_publishing_waits_for_the_commit(sent: mock.MagicMock, password_user: User) -> None:
    """Inside the transaction the message has not gone anywhere yet. That is the whole design."""
    account = AccountFactory.create(owner=password_user)

    with transaction.atomic():
        events.publish_balance(password_user.pk, account.pk, Decimal("1.0000"))
        sent.assert_not_called()

    assert types_in(sent) == ["balance.updated"]


def test_a_rolled_back_transaction_publishes_nothing(
    sent: mock.MagicMock, password_user: User
) -> None:
    account = AccountFactory.create(owner=password_user)

    with pytest.raises(RuntimeError), transaction.atomic():
        events.publish_balance(password_user.pk, account.pk, Decimal("1.0000"))
        raise RuntimeError("something went wrong after the publish")

    sent.assert_not_called()


# --------------------------------------------------------------------------------------------
# Real postings
# --------------------------------------------------------------------------------------------


def test_a_rejected_order_announces_the_rejection_and_no_balance(
    sent: mock.MagicMock, password_user: User, instrument: Instrument
) -> None:
    """Nothing moved, so there is no new balance to send — and the client still learns it failed.

    The rejection is written *outside* the failed transaction (ADR-0014), which is precisely why it
    survives to be published at all while everything the fill attempted does not.
    """
    broke = AccountFactory.create(owner=password_user, name="Empty")

    with pytest.raises(InsufficientFundsError):
        place_order(
            user=password_user,
            instrument=instrument,
            cash_account=broke,
            side="buy",
            order_type="market",
            quantity=Decimal("5"),
        )

    assert types_in(sent) == ["order.rejected"]


def test_a_failed_transfer_announces_nothing_at_all(
    sent: mock.MagicMock, password_user: User
) -> None:
    """The overdraft is caught under the lock, inside the atomic block, before anything commits."""
    source = AccountFactory.create(owner=password_user, name="Empty")
    destination = AccountFactory.create(owner=password_user, name="Savings")

    with pytest.raises(InsufficientFundsError):
        transfer(
            source=source,
            destination=destination,
            amount=Decimal("100.0000"),
            actor=password_user,
        )

    sent.assert_not_called()


def test_a_fill_whose_caller_later_fails_announces_nothing(
    sent: mock.MagicMock, password_user: User, instrument: Instrument
) -> None:
    """The fill's own transaction became a savepoint inside a larger one that then aborted.

    This is the scenario the ``on_commit`` rule is really for: the fill itself succeeded, its
    events were queued, and the money is still rolled back. Publishing eagerly would have told the
    client about a trade that never happened.
    """
    cash = AccountFactory.create(owner=password_user, name="Brokerage cash")
    fund_account(cash, Decimal("5000.0000"))

    with pytest.raises(RuntimeError), transaction.atomic():
        place_order(
            user=password_user,
            instrument=instrument,
            cash_account=cash,
            side="buy",
            order_type="market",
            quantity=Decimal("2"),
        )
        raise RuntimeError("the caller failed after the fill")

    sent.assert_not_called()


def test_the_same_fill_does_announce_when_it_commits(
    sent: mock.MagicMock, password_user: User, instrument: Instrument
) -> None:
    """The control. Without this, the test above would pass on code that never publishes at all."""
    cash = AccountFactory.create(owner=password_user, name="Brokerage cash")
    fund_account(cash, Decimal("5000.0000"))

    place_order(
        user=password_user,
        instrument=instrument,
        cash_account=cash,
        side="buy",
        order_type="market",
        quantity=Decimal("2"),
    )

    assert types_in(sent) == ["order.filled", "balance.updated"]


def test_a_session_revocation_kill_is_deferred_too(
    sent: mock.MagicMock, password_user: User
) -> None:
    """Closing a live socket for a revocation that rolls back would log the user out for nothing."""
    with pytest.raises(RuntimeError), transaction.atomic():
        events.publish_session_revoked("00000000-0000-0000-0000-000000000000")
        raise RuntimeError("revocation failed")

    sent.assert_not_called()


def test_an_unreachable_channel_layer_never_fails_a_committed_write(
    password_user: User, instrument: Instrument
) -> None:
    """The money has already moved. Taking down the request because Redis is down is strictly worse.

    Patched at ``group_send`` rather than at ``_send``, so the real error handling runs.
    """
    cash = AccountFactory.create(owner=password_user, name="Brokerage cash")
    fund_account(cash, Decimal("5000.0000"))

    with mock.patch.object(events, "get_channel_layer") as layer:
        layer.return_value.group_send.side_effect = ConnectionError("redis is gone")
        order = place_order(
            user=password_user,
            instrument=instrument,
            cash_account=cash,
            side="buy",
            order_type="market",
            quantity=Decimal("1"),
        )

    assert order.status == "filled"


def test_no_channel_layer_at_all_is_survivable(password_user: User) -> None:
    """A deployment without Redis configured is a degraded one, not a broken one."""
    account = AccountFactory.create(owner=password_user)

    with mock.patch.object(events, "get_channel_layer", return_value=None):
        events.publish_balance(password_user.pk, account.pk, Decimal("5.0000"))


def test_events_never_reach_a_group_belonging_to_someone_else(
    sent: mock.MagicMock, password_user: User, instrument: Instrument
) -> None:
    """Group naming is the whole access-control model on the socket; it is worth asserting once."""
    stranger: User = UserFactory.create()
    cash = AccountFactory.create(owner=password_user, name="Brokerage cash")
    fund_account(cash, Decimal("5000.0000"))

    place_order(
        user=password_user,
        instrument=instrument,
        cash_account=cash,
        side="buy",
        order_type="market",
        quantity=Decimal("1"),
    )

    groups = {call.args[0] for call in sent.call_args_list}
    assert groups == {f"user.{password_user.pk}"}
    assert f"user.{stranger.pk}" not in groups


def test_a_price_tick_reaches_only_its_own_symbol_group(sent: mock.MagicMock) -> None:
    events.publish_prices([("aapl", Decimal("195.0000")), ("TSLA", Decimal("240.0000"))])

    groups = [call.args[0] for call in sent.call_args_list]
    assert groups == ["prices.AAPL", "prices.TSLA"]


def test_decimals_cross_the_layer_as_strings(sent: mock.MagicMock, password_user: User) -> None:
    """msgpack would happily turn a Decimal into a float on the way to Redis (ADR-0009)."""
    account: Account = AccountFactory.create(owner=password_user)

    events.publish_balance(password_user.pk, account.pk, Decimal("1952.9670"))

    payload = sent.call_args.args[1]["payload"]
    assert payload["balance"] == "1952.9670"
    assert isinstance(payload["balance"], str)
