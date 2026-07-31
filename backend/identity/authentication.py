"""JWT authentication that honours session revocation (ADR-0013).

This is the single place every authenticated request passes through, and the only thing in the
system that makes an **access** token revocable before it expires — SimpleJWT's blacklist records
refresh tokens only. Cost is one indexed primary-key lookup per request; the obvious optimization
(cache revoked sids in Redis with a TTL of ``ACCESS_TOKEN_LIFETIME``, since a revoked session only
needs remembering for as long as a token could still be alive) is deferred to Weeks 5–6.

Tokens minted before Week 4 carry no ``sid`` and stop working at deploy. That is intended: a token
that cannot be revoked is exactly what this week set out to eliminate.
"""

from django.contrib.auth.base_user import AbstractBaseUser
from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.tokens import Token

from .services import active_session


class SessionAwareJWTAuthentication(JWTAuthentication):
    # Upstream annotates `get_user` as returning the bare TypeVar `AuthUser` on a class that is not
    # Generic, so no override can satisfy it as written; `AbstractBaseUser` is the constraint that
    # actually applies here.
    def get_user(self, validated_token: Token) -> AbstractBaseUser:  # type: ignore[override]
        user = super().get_user(validated_token)

        sid = validated_token.get("sid")
        if not sid:
            raise AuthenticationFailed("Token is not bound to a session.", code="session_missing")
        if active_session(str(sid)) is None:
            raise AuthenticationFailed("Session has been revoked.", code="session_revoked")

        return user
