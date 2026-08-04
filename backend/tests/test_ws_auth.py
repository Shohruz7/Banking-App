"""How a socket is authenticated, and the three ways it dies (ADR-0022).

Every test here drives the real ``config.asgi.application``, origin validator included, through
Channels' communicator — so what is exercised is the deployed path, not a consumer instantiated by
hand.

The two liveness properties are the ones worth the async machinery. An access token lives fifteen
minutes and a socket can live for hours, so authenticating once and streaming forever would make
the socket the longest-lived credential in the system; and a session revoked over HTTP has to take
its sockets with it, or logging out leaves a live stream of the user's fills running.
"""

from datetime import timedelta
from typing import Any

import pytest
from channels.db import database_sync_to_async
from channels.testing import WebsocketCommunicator
from django.contrib.auth.models import User
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from config.asgi import application
from identity.models import AuthSession, RevokeReason
from identity.services import revoke_session
from realtime.consumers import CLOSE_SESSION_REVOKED, CLOSE_UNAUTHENTICATED
from tests.conftest import obtain_tokens
from tests.factories import UserFactory

pytestmark = [pytest.mark.asyncio, pytest.mark.django_db(transaction=True)]

STREAM_PATH = "/ws/v1/stream/"
LOCAL_ORIGIN = [(b"origin", b"http://localhost")]


async def open_socket(**kwargs: Any) -> WebsocketCommunicator:
    """An accepted, still-anonymous socket."""
    communicator = WebsocketCommunicator(application, STREAM_PATH, headers=LOCAL_ORIGIN, **kwargs)
    connected, _ = await communicator.connect()
    assert connected
    return communicator


@database_sync_to_async
def make_user() -> User:
    return UserFactory.create()


@database_sync_to_async
def access_token_for(user: User) -> str:
    """A real access token, obtained through the login endpoint like any client's."""
    return str(obtain_tokens(APIClient(), user)["access"])


@database_sync_to_async
def short_lived_token_for(user: User) -> str:
    """A valid token that expires in about a second, so the expiry path is testable in real time."""
    session = AuthSession.objects.create(user=user, ip="127.0.0.1")
    token = AccessToken.for_user(user)
    token["sid"] = str(session.id)
    token.set_exp(lifetime=timedelta(seconds=1))
    return str(token)


@database_sync_to_async
def revoke(sid: str) -> None:
    revoke_session(sid, reason=RevokeReason.LOGOUT)


@database_sync_to_async
def sid_of(user: User) -> str:
    return str(AuthSession.objects.filter(user=user).latest("created_at").id)


async def authenticate(communicator: WebsocketCommunicator, token: str) -> dict[str, Any]:
    await communicator.send_json_to({"type": "auth", "token": token})
    return dict(await communicator.receive_json_from())


async def assert_closed_with(communicator: WebsocketCommunicator, code: int) -> None:
    message = await communicator.receive_output(timeout=3)
    assert message["type"] == "websocket.close"
    assert message["code"] == code


# --------------------------------------------------------------------------------------------
# Getting in
# --------------------------------------------------------------------------------------------


async def test_a_valid_token_authenticates_the_socket() -> None:
    user = await make_user()
    communicator = await open_socket()

    reply = await authenticate(communicator, await access_token_for(user))

    assert reply["type"] == "auth.ok"
    assert reply["user_id"] == user.pk
    assert reply["username"] == user.get_username()
    assert reply["expires_at"]
    await communicator.disconnect()


async def test_a_socket_that_never_authenticates_is_closed(settings: Any) -> None:
    """The deadline. An accepted socket that stays anonymous is a resource nobody is entitled to."""
    settings.WS_AUTH_DEADLINE_SECONDS = 0.1
    communicator = await open_socket()

    await assert_closed_with(communicator, CLOSE_UNAUTHENTICATED)


@pytest.mark.parametrize(
    "token",
    ["", "not-a-jwt", "eyJhbGciOiJIUzI1NiJ9.eyJ1c2VyX2lkIjoxfQ.bogus-signature"],
    ids=["empty", "malformed", "forged"],
)
async def test_a_token_that_does_not_validate_closes_the_socket(token: str) -> None:
    """Every failure collapses to one close code: a handshake is a poor place to build an oracle."""
    communicator = await open_socket()

    await communicator.send_json_to({"type": "auth", "token": token})

    await assert_closed_with(communicator, CLOSE_UNAUTHENTICATED)


async def test_a_token_whose_session_was_revoked_cannot_authenticate() -> None:
    """The socket goes through ``SessionAwareJWTAuthentication``, so ADR-0013 applies unchanged."""
    user = await make_user()
    token = await access_token_for(user)
    await revoke(await sid_of(user))

    communicator = await open_socket()
    await communicator.send_json_to({"type": "auth", "token": token})

    await assert_closed_with(communicator, CLOSE_UNAUTHENTICATED)


async def test_anything_before_an_auth_frame_closes_the_socket() -> None:
    communicator = await open_socket()

    await communicator.send_json_to({"type": "subscribe", "symbols": ["AAPL"]})

    await assert_closed_with(communicator, CLOSE_UNAUTHENTICATED)


# --------------------------------------------------------------------------------------------
# Staying in
# --------------------------------------------------------------------------------------------


async def test_re_authenticating_keeps_the_socket_and_re_arms_its_expiry() -> None:
    """A client refreshing its token should not have to reconnect and re-subscribe."""
    user = await make_user()
    communicator = await open_socket()
    await authenticate(communicator, await access_token_for(user))

    second = await authenticate(communicator, await access_token_for(user))

    assert second["type"] == "auth.ok"
    assert second["user_id"] == user.pk
    await communicator.disconnect()


async def test_another_users_token_cannot_take_over_a_live_socket() -> None:
    """Otherwise the second user inherits the first one's subscriptions and group memberships."""
    user, stranger = await make_user(), await make_user()
    communicator = await open_socket()
    await authenticate(communicator, await access_token_for(user))

    await communicator.send_json_to({"type": "auth", "token": await access_token_for(stranger)})

    await assert_closed_with(communicator, CLOSE_UNAUTHENTICATED)


async def test_the_socket_closes_when_its_access_token_expires() -> None:
    """Fifteen-minute access tokens buy nothing if one of them opens an unbounded stream."""
    user = await make_user()
    communicator = await open_socket()

    reply = await authenticate(communicator, await short_lived_token_for(user))
    assert reply["type"] == "auth.ok"

    await assert_closed_with(communicator, CLOSE_UNAUTHENTICATED)


async def test_revoking_the_session_closes_the_socket_it_authenticated() -> None:
    """The headline: logging out on one device drops its live stream (ADR-0013, ADR-0022).

    Without this a socket authenticated a minute before logout keeps pushing the user's fills on a
    credential nobody can withdraw — the exact hole session binding closed for HTTP.
    """
    user = await make_user()
    communicator = await open_socket()
    await authenticate(communicator, await access_token_for(user))

    await revoke(await sid_of(user))

    notice = await communicator.receive_json_from(timeout=3)
    assert notice["type"] == "session.revoked"
    await assert_closed_with(communicator, CLOSE_SESSION_REVOKED)


async def test_a_foreign_origin_never_gets_a_socket() -> None:
    """``AllowedHostsOriginValidator``: without it, any page could open an authenticated socket
    from a visitor's browser — the WebSocket equivalent of having no CSRF protection."""
    communicator = WebsocketCommunicator(
        application, STREAM_PATH, headers=[(b"origin", b"http://evil.example.com")]
    )

    connected, _ = await communicator.connect()

    assert connected is False
    await communicator.disconnect()
