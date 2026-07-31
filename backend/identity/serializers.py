"""Identity serializers — registration, the profile, and the token lifecycle.

The token serializers are where Week 4's two guarantees are actually wired: every issued token
carries the ``sid`` of a revocable session, and a refresh token that comes back after having been
rotated away takes its whole family down with it.
"""

from contextlib import suppress
from typing import Any, cast

from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.core.cache import cache
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers
from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer, TokenRefreshSerializer
from rest_framework_simplejwt.tokens import RefreshToken

from audit.context import current_context
from audit.models import AuditAction
from audit.services import record_audit

from .api_exceptions import InvalidMFACode
from .models import MfaDevice, RevokeReason
from .services import (
    active_session,
    confirmed_device,
    handle_possible_reuse,
    require_mfa_for,
    revoke_session,
    start_session,
    touch_session,
    verify_totp,
)
from .tokens import MFAPendingToken

#: Marks an issued pre-auth token as unspent. Keyed by jti so a token can be burned on use.
_MFA_PENDING_CACHE_KEY = "mfa:pending:{jti}"


class UserSerializer(serializers.ModelSerializer[User]):
    """The requesting user's own profile. Never used to expose *another* user."""

    mfa_enabled = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ("id", "username", "email", "mfa_enabled", "date_joined")
        read_only_fields = fields

    def get_mfa_enabled(self, obj: User) -> bool:
        """True once a TOTP device has been confirmed — enrolled-but-unconfirmed does not count."""
        return MfaDevice.objects.filter(user=obj, confirmed_at__isnull=False).exists()


class RegisterSerializer(serializers.ModelSerializer[User]):
    """Create a user with a validated, hashed password.

    ``email`` is required and enforced unique case-insensitively here rather than on the model:
    ``django.contrib.auth.models.User.email`` carries no unique constraint, and ADR-0011 rules out
    swapping the user model to add one. Registration is the only path that creates users, so this
    is where the invariant belongs.
    """

    password = serializers.CharField(write_only=True, style={"input_type": "password"})
    email = serializers.EmailField(required=True)

    class Meta:
        model = User
        fields = ("id", "username", "email", "password")
        read_only_fields = ("id",)

    def validate_email(self, value: str) -> str:
        if User.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError("A user with that email already exists.")
        return value

    def validate_password(self, value: str) -> str:
        # Django's validators raise its own ValidationError; DRF only understands its own.
        try:
            validate_password(value)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(list(exc.messages)) from exc
        return value

    def create(self, validated_data: dict[str, Any]) -> User:
        return User.objects.create_user(**validated_data)


# ------------------------------------------------------------------------------------------------
# Token lifecycle
# ------------------------------------------------------------------------------------------------


def _issue_pair(user: User) -> dict[str, str]:
    """Open a session and mint a token pair bound to it.

    The ``sid`` claim is set on the refresh token only; the access token derived from it inherits
    the claim automatically, because SimpleJWT's ``no_copy_claims`` excludes just ``token_type``,
    ``exp``, ``jti`` and ``iat``. The same mechanism carries ``sid`` across every rotation, which
    is the hook the whole session design hangs on.

    The session's ip and user agent come from the ambient audit context rather than from a request
    argument — same transport facts, same single source, and it works identically off-request.
    """
    ambient = current_context()
    session = start_session(user, ip=ambient.ip, user_agent=ambient.user_agent)
    refresh = RefreshToken.for_user(user)
    refresh["sid"] = str(session.id)
    return {"access": str(refresh.access_token), "refresh": str(refresh)}


class TokenObtainPairWithMFASerializer(TokenObtainPairSerializer):
    """Password step of the login. Returns a token pair, or an MFA challenge (ADR-0012).

    The challenge branch is why this returns 200 rather than 401: the credentials *were* correct,
    the flow simply is not finished. A 401 would force clients to tell "wrong password" from "need
    a code" by parsing an error code, and would trip the generic SPA interceptor that redirects to
    login on any 401.
    """

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        try:
            # Sets self.user; raises AuthenticationFailed for bad credentials or an inactive user.
            super().validate(attrs)
        except AuthenticationFailed:
            attempted = str(attrs.get(self.username_field, ""))[:150]
            record_audit(
                action=AuditAction.LOGIN_FAILED,
                actor_label=attempted,
                context={"identifier": attempted},
            )
            raise

        # super().validate() either sets self.user or raises, so this is never None here.
        user = cast(User, self.user)

        if require_mfa_for("login", user):
            pending = MFAPendingToken.for_user(user)
            cache.set(
                _MFA_PENDING_CACHE_KEY.format(jti=pending["jti"]),
                True,
                int(MFAPendingToken.lifetime.total_seconds()),
            )
            record_audit(action=AuditAction.MFA_CHALLENGED, actor=user)
            return {"mfa_required": True, "mfa_token": str(pending)}

        data = _issue_pair(user)
        record_audit(action=AuditAction.LOGIN_SUCCEEDED, actor=user, context={"mfa": False})
        return data


class MFAVerifySerializer(serializers.Serializer[dict[str, Any]]):
    """Second step: spend the pre-auth token plus a TOTP code for a real token pair."""

    mfa_token = serializers.CharField()
    code = serializers.CharField(min_length=6, max_length=6)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        try:
            pending = MFAPendingToken(attrs["mfa_token"])
        except TokenError as exc:
            raise InvalidToken(str(exc)) from exc

        # Single use: the key exists only until the first successful spend.
        cache_key = _MFA_PENDING_CACHE_KEY.format(jti=pending["jti"])
        if not cache.get(cache_key):
            raise InvalidToken("This challenge has already been used.")

        user = User.objects.filter(pk=pending["user_id"], is_active=True).first()
        device = confirmed_device(user) if user else None
        if user is None or device is None:
            raise InvalidToken("This challenge is no longer valid.")

        if not verify_totp(device, attrs["code"]):
            record_audit(action=AuditAction.MFA_FAILED, actor=user)
            raise InvalidMFACode

        cache.delete(cache_key)

        data = _issue_pair(user)
        record_audit(action=AuditAction.MFA_VERIFIED, actor=user)
        record_audit(action=AuditAction.LOGIN_SUCCEEDED, actor=user, context={"mfa": True})
        return data


class SessionRefreshSerializer(TokenRefreshSerializer):
    """Rotate a refresh token, detecting reuse and refusing revoked sessions (ADR-0013)."""

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        raw = attrs["refresh"]

        try:
            refresh = RefreshToken(raw)
        except TokenError as exc:
            # The token was rejected — possibly because it was already rotated away, which is the
            # signature of a replay. handle_possible_reuse verifies the signature before acting.
            handle_possible_reuse(raw)
            raise InvalidToken(str(exc)) from exc

        sid = refresh.get("sid")
        session = active_session(str(sid) if sid else None)
        if session is None:
            raise InvalidToken("Session has been revoked.")

        # Delegate the rotation itself: super() blacklists the old token, re-jtis, and outstands
        # the new one. Re-parsing costs one HMAC verification and buys never silently diverging
        # from the library on upgrade.
        data: dict[str, Any] = super().validate(attrs)

        touch_session(session)
        record_audit(
            action=AuditAction.TOKEN_REFRESHED,
            actor=session.user,
            target_type="auth_session",
            target_id=str(session.id),
        )
        return data


class LogoutSerializer(serializers.Serializer[dict[str, Any]]):
    """Blacklist the presented refresh token and revoke the session behind it.

    Stock ``TokenBlacklistView`` only does the first half, which leaves every access token from
    that login valid for up to its full lifetime. Revoking the session is what actually logs the
    user out.
    """

    refresh = serializers.CharField()

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        try:
            refresh = RefreshToken(attrs["refresh"])
        except TokenError as exc:
            raise InvalidToken(str(exc)) from exc
        attrs["token"] = refresh
        return attrs

    def logout(self) -> None:
        refresh: RefreshToken = self.validated_data["token"]
        user: User = self.context["request"].user

        # token_blacklist is installed unconditionally, so blacklist() always exists; the guard is
        # here because SimpleJWT gates the method on INSTALLED_APPS at class-definition time, and a
        # silent AttributeError would mean logout appears to work while revoking nothing.
        with suppress(AttributeError):  # pragma: no cover
            refresh.blacklist()

        sid = refresh.get("sid")
        if sid:
            revoke_session(str(sid), reason=RevokeReason.LOGOUT)
        record_audit(action=AuditAction.LOGOUT, actor=user)


class MfaCodeSerializer(serializers.Serializer[dict[str, Any]]):
    """A bare 6-digit TOTP code, for confirming or disabling a device."""

    code = serializers.CharField(min_length=6, max_length=6)


class MfaEnrollResponseSerializer(serializers.Serializer[dict[str, Any]]):
    """The one and only time the secret crosses the wire."""

    secret = serializers.CharField(read_only=True)
    otpauth_uri = serializers.CharField(read_only=True)
    qr_svg = serializers.CharField(read_only=True)
