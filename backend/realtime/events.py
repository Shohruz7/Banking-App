"""Publishing to connected sockets (ADR-0023).

**Every publish in this module is deferred to ``transaction.on_commit``.** That is the whole rule,
and it is not a stylistic preference: a fill that rolls back must never have been announced, and a
balance pushed from inside a transaction that later aborts is a number the client will keep showing
until it reloads. The ledger already refuses to let an audit row outlive the posting it describes
(ADR-0014); a socket message is the same claim made to a different audience.

Outside an atomic block ``on_commit`` runs the callback immediately, which is what makes the
rejection path — deliberately outside the failed transaction — publish straight away.

Two failure modes are swallowed on purpose. If no channel layer is configured, or Redis is
unreachable, publishing logs and returns. A notification is a courtesy; the money has already
moved, and taking down a committed request because a socket could not be told about it would be
strictly worse than the client finding out on its next poll.
"""

import json
import logging
from collections.abc import Iterable
from decimal import Decimal
from typing import Any

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.core.serializers.json import DjangoJSONEncoder
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

#: Channels message types. The dots become underscores when Channels dispatches to the consumer,
#: so these name ``StreamConsumer.stream_event`` and ``StreamConsumer.session_kill``.
STREAM_EVENT = "stream.event"
SESSION_KILL = "session.kill"


def user_group(user_id: int) -> str:
    return f"user.{user_id}"


def session_group(sid: str) -> str:
    return f"session.{sid}"


def price_group(symbol: str) -> str:
    return f"prices.{symbol.upper()}"


def _encode(payload: dict[str, Any]) -> dict[str, Any]:
    """Round-trip through ``DjangoJSONEncoder`` so a ``Decimal`` crosses as ``"195.2967"``.

    The same contract ADR-0009 puts on HTTP responses, applied to the socket: a client that parses
    a price as a float has silently lost the guarantee the whole ledger is built on. The channel
    layer serializes with msgpack, which would happily turn a Decimal into a float on the way out.
    """
    return dict(json.loads(json.dumps(payload, cls=DjangoJSONEncoder)))


def publish(group: str, payload: dict[str, Any]) -> None:
    """Send one event to a group, once the current transaction commits."""
    encoded = _encode(payload)
    transaction.on_commit(lambda: _send(group, {"type": STREAM_EVENT, "payload": encoded}))


def _send(group: str, message: dict[str, Any]) -> None:
    layer = get_channel_layer()
    if layer is None:
        return
    try:
        async_to_sync(layer.group_send)(group, message)
    # Blind, deliberately: see the module docstring — a dropped notification must never fail a
    # posting that already committed. BLE001 does not fire because the handler logs with
    # exception info, which is the shape that makes a broad except accountable.
    except Exception:
        logger.exception("could not publish %s to %s", message.get("type"), group)


# --------------------------------------------------------------------------------------------
# The events themselves
# --------------------------------------------------------------------------------------------


def publish_balance(user_id: int, account_id: Any, balance: Decimal) -> None:
    """An account's new derived balance."""
    publish(
        user_group(user_id),
        {
            "type": "balance.updated",
            "account_id": str(account_id),
            "balance": balance,
            "at": timezone.now(),
        },
    )


def publish_transfer(user_id: int, *, entry_id: Any, amount: Decimal, description: str) -> None:
    """A posted transfer, to one of its two sides."""
    publish(
        user_group(user_id),
        {
            "type": "transfer.posted",
            "entry_id": str(entry_id),
            "amount": amount,
            "description": description,
            "at": timezone.now(),
        },
    )


def publish_order(user_id: int, event: str, payload: dict[str, Any]) -> None:
    """An order outcome — ``order.filled``, ``order.rejected`` or ``order.cancelled``."""
    publish(user_group(user_id), {"type": event, **payload, "at": timezone.now()})


def publish_prices(ticks: Iterable[tuple[str, Decimal]]) -> None:
    """One message per symbol, to that symbol's group.

    Fan-out is bounded by *subscription*, not by client count: a group nobody has joined costs a
    round trip to Redis and nothing else, so a 57-symbol market is 57 sends whether one client is
    watching or a thousand are.
    """
    at = timezone.now()
    for symbol, price in ticks:
        publish(
            price_group(symbol),
            {"type": "price.tick", "symbol": symbol.upper(), "price": price, "at": at},
        )


def publish_session_revoked(sid: str) -> None:
    """Tell every socket authenticated on this session to close (ADR-0022).

    Not a ``stream.event``: this one does not reach the client as data, it ends the connection.
    Without it a socket authenticated a minute before logout would keep streaming a user's fills
    after their session was revoked — the exact hole ADR-0013 closed for HTTP.
    """
    transaction.on_commit(lambda: _send(session_group(sid), {"type": SESSION_KILL}))
