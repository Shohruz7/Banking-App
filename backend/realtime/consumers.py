"""The one WebSocket endpoint: ``ws/v1/stream/`` (ADR-0022).

The protocol is accept-then-authenticate. A browser cannot set an ``Authorization`` header on a
WebSocket handshake, and the two usual workarounds both put a credential somewhere it does not
belong — a query string lands in access logs, proxy logs and ``Referer``; the ``Sec-WebSocket-
Protocol`` header is a smuggling trick that confuses every proxy in the path. So the socket is
accepted anonymously, must present an access token in its first frame, and is closed if it does
not.

    →  (connect)                                  accepted; a deadline starts
    ←  {"type": "auth", "token": "<access>"}
    →  {"type": "auth.ok", "user_id": …}          joined user.<id> and session.<sid>
    ←  {"type": "subscribe", "symbols": [...]}    joined prices.<SYMBOL>
    →  {"type": "price.tick" | "order.filled" | "balance.updated" | ...}

A socket dies three ways, and all three matter: the deadline passes without an auth frame, the
access token expires, or the session behind it is revoked. The last one is the point — without it,
logging out would leave a live stream of the user's fills running on a token nobody can withdraw.
"""

import asyncio
import logging
from typing import Any

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from django.conf import settings
from django.utils import timezone

from markets.models import Instrument

from .auth import SocketIdentity, authenticate_token_async
from .events import price_group, session_group, user_group

logger = logging.getLogger(__name__)

#: Close codes. 4000–4999 is the range reserved for applications; these mirror the HTTP statuses
#: their situations would produce, so a client can branch on them the same way.
CLOSE_UNAUTHENTICATED = 4401
CLOSE_SESSION_REVOKED = 4403
CLOSE_TOO_MANY_SUBSCRIPTIONS = 4429


@database_sync_to_async
def _known_symbols(symbols: list[str]) -> set[str]:
    """Which of these are real, tradeable symbols.

    Checked against the database rather than accepted as given: group names go to Redis, and
    letting a client mint arbitrary ones is how a socket endpoint becomes a way to probe or fill
    someone else's memory.
    """
    return set(
        Instrument.objects.filter(symbol__in=symbols, is_active=True).values_list(
            "symbol", flat=True
        )
    )


class StreamConsumer(AsyncJsonWebsocketConsumer):
    """One client's live view of their own money and the market."""

    identity: SocketIdentity | None
    subscriptions: set[str]

    async def connect(self) -> None:
        self.identity = None
        self.subscriptions = set()
        self._deadline_task: asyncio.Task[None] | None = None
        self._expiry_task: asyncio.Task[None] | None = None

        await self.accept()
        self._deadline_task = asyncio.create_task(self._close_when_deadline_passes())

    async def disconnect(self, code: int) -> None:
        self._cancel(self._deadline_task)
        self._cancel(self._expiry_task)
        for symbol in self.subscriptions:
            await self.channel_layer.group_discard(price_group(symbol), self.channel_name)
        if self.identity is not None:
            await self.channel_layer.group_discard(
                user_group(self.identity.user_id), self.channel_name
            )
            await self.channel_layer.group_discard(
                session_group(self.identity.sid), self.channel_name
            )

    async def receive_json(self, content: dict[str, Any], **kwargs: Any) -> None:
        message_type = content.get("type")

        if message_type == "auth":
            await self._handle_auth(content)
            return

        # Everything else requires an authenticated socket. An anonymous client sending anything
        # other than auth has misunderstood the protocol badly enough to close on.
        if self.identity is None:
            await self.close(code=CLOSE_UNAUTHENTICATED)
            return

        if message_type == "subscribe":
            await self._handle_subscribe(content)
        elif message_type == "unsubscribe":
            await self._handle_unsubscribe(content)
        elif message_type == "ping":
            await self.send_json({"type": "pong", "at": timezone.now().isoformat()})
        else:
            await self.send_json(
                {"type": "error", "code": "unknown_message", "message": f"{message_type!r}"}
            )

    # ------------------------------------------------------------------ client → server

    async def _handle_auth(self, content: dict[str, Any]) -> None:
        identity = await authenticate_token_async(content.get("token", ""))
        if identity is None:
            await self.close(code=CLOSE_UNAUTHENTICATED)
            return

        # Re-authentication keeps the socket alive across a token refresh, but only for the same
        # user: handing a second person's token to an existing connection would inherit its
        # subscriptions and its group memberships.
        if self.identity is not None and identity.user_id != self.identity.user_id:
            await self.close(code=CLOSE_UNAUTHENTICATED)
            return

        first_auth = self.identity is None
        self.identity = identity
        self._cancel(self._deadline_task)
        self._cancel(self._expiry_task)

        if first_auth:
            await self.channel_layer.group_add(user_group(identity.user_id), self.channel_name)
        # The session group is rejoined on every auth: a refreshed token carries the same sid, but
        # re-authenticating after a *new* login would move this socket to the new session, and the
        # old session's revocation must then no longer close it.
        await self.channel_layer.group_add(session_group(identity.sid), self.channel_name)

        self._expiry_task = asyncio.create_task(self._close_when_token_expires(identity))
        await self.send_json(
            {
                "type": "auth.ok",
                "user_id": identity.user_id,
                "username": identity.username,
                "expires_at": identity.expires_at.isoformat(),
            }
        )

    async def _handle_subscribe(self, content: dict[str, Any]) -> None:
        requested = _clean_symbols(content.get("symbols"))
        known = await _known_symbols(requested)

        if len(self.subscriptions | known) > settings.WS_MAX_SUBSCRIPTIONS:
            await self.close(code=CLOSE_TOO_MANY_SUBSCRIPTIONS)
            return

        for symbol in known - self.subscriptions:
            await self.channel_layer.group_add(price_group(symbol), self.channel_name)
        self.subscriptions |= known

        await self.send_json(
            {
                "type": "subscribed",
                "symbols": sorted(self.subscriptions),
                # Named explicitly rather than silently dropped: a client watching a symbol that
                # was delisted this morning should be told, not left waiting for ticks.
                "unknown": sorted(set(requested) - known),
            }
        )

    async def _handle_unsubscribe(self, content: dict[str, Any]) -> None:
        for symbol in _clean_symbols(content.get("symbols")) & self.subscriptions:
            await self.channel_layer.group_discard(price_group(symbol), self.channel_name)
            self.subscriptions.discard(symbol)
        await self.send_json({"type": "subscribed", "symbols": sorted(self.subscriptions)})

    # ------------------------------------------------------------------ server → client

    async def stream_event(self, message: dict[str, Any]) -> None:
        """Deliver one published event. Named for ``events.STREAM_EVENT``."""
        await self.send_json(message["payload"])

    async def session_kill(self, message: dict[str, Any]) -> None:
        """The session behind this socket was revoked. Say so, then close (ADR-0022)."""
        await self.send_json({"type": "session.revoked"})
        await self.close(code=CLOSE_SESSION_REVOKED)

    # ------------------------------------------------------------------ lifetime

    async def _close_when_deadline_passes(self) -> None:
        await asyncio.sleep(settings.WS_AUTH_DEADLINE_SECONDS)
        logger.info("closing socket %s: no auth frame", self.channel_name)
        await self.close(code=CLOSE_UNAUTHENTICATED)

    async def _close_when_token_expires(self, identity: SocketIdentity) -> None:
        """Close the socket when the access token that opened it expires.

        Authenticating once and streaming forever would make the socket the longest-lived
        credential in the system — fifteen-minute access tokens would buy nothing. The client's
        answer is to send a fresh ``auth`` frame before this fires, which re-arms it.
        """
        remaining = (identity.expires_at - timezone.now()).total_seconds()
        if remaining > 0:
            await asyncio.sleep(remaining)
        await self.close(code=CLOSE_UNAUTHENTICATED)

    @staticmethod
    def _cancel(task: "asyncio.Task[None] | None") -> None:
        if task is not None and not task.done():
            task.cancel()


def _clean_symbols(raw: Any) -> set[str]:
    """Normalize a client's symbol list. Anything that is not a list of strings is empty."""
    if not isinstance(raw, list):
        return set()
    return {item.upper() for item in raw if isinstance(item, str) and item.strip()}
