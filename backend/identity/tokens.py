"""The pre-authentication token for the two-step MFA login (ADR-0012)."""

from datetime import timedelta

from rest_framework_simplejwt.tokens import Token


class MFAPendingToken(Token):
    """Proves the password step passed, and nothing more.

    It cannot authenticate a request *by construction*, not by convention:
    ``SIMPLE_JWT["AUTH_TOKEN_CLASSES"]`` lists ``AccessToken`` only, so
    ``JWTAuthentication.get_validated_token`` rejects this token type outright. Five minutes is
    long enough to read a code off a phone and short enough that an intercepted one is near
    worthless; it is additionally single-use, tracked in the cache by ``jti``.
    """

    token_type = "mfa_pending"  # noqa: S105 — a token *type* name, not a credential
    lifetime = timedelta(minutes=5)
