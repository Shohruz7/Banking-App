"""Authenticating a WebSocket (ADR-0022).

The token check is not reimplemented here. It goes through the same
:class:`~identity.authentication.SessionAwareJWTAuthentication` instance every HTTP request uses,
because the ``sid`` session binding is the only mechanism that makes an access token revocable
(ADR-0013) — and a socket that validated tokens its own way would be the one place in the system
where revocation silently did not apply.

What *is* socket-specific is the lifetime problem. An access token lives fifteen minutes; a socket
can live for hours. So authentication returns the token's expiry, and the consumer closes the
connection when it passes unless the client re-authenticates in place.
"""

from dataclasses import dataclass
from datetime import UTC, datetime

from channels.db import database_sync_to_async
from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError

from identity.authentication import SessionAwareJWTAuthentication

_authenticator = SessionAwareJWTAuthentication()


@dataclass(frozen=True)
class SocketIdentity:
    """Who is on the other end of an authenticated socket, and until when."""

    user_id: int
    username: str
    sid: str
    expires_at: datetime


def authenticate_token(raw_token: str) -> SocketIdentity | None:
    """Validate an access token the way an HTTP request would. ``None`` means "close the socket".

    Every failure — forged signature, expired, wrong token class, missing ``sid``, revoked session —
    collapses to the same answer on purpose. A socket handshake is a fine oracle to build if you
    tell the client *why* their token was refused.
    """
    if not raw_token or not isinstance(raw_token, str):
        return None

    try:
        validated = _authenticator.get_validated_token(raw_token.encode())
        user = _authenticator.get_user(validated)
        # Both claims are read inside the try rather than guarded with a second check: ``sid`` is
        # already guaranteed by ``get_user`` (it raises without one) and ``exp`` by the token
        # class, so a missing one is a KeyError — which is the same "close the socket" answer.
        return SocketIdentity(
            user_id=user.pk,
            username=user.get_username(),
            sid=str(validated["sid"]),
            expires_at=datetime.fromtimestamp(int(validated["exp"]), tz=UTC),
        )
    except (InvalidToken, TokenError, AuthenticationFailed, KeyError):
        return None


authenticate_token_async = database_sync_to_async(authenticate_token)
