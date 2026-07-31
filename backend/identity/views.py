"""Identity endpoints — registration and the requesting user's own profile.

Both sit at ``/api/v1/auth/`` alongside the token views. ``RegisterView`` is one of the very few
endpoints that opts out of the ``IsAuthenticated`` default from ADR-0006, so it says so explicitly
and carries the tightest throttle scope in the config (ADR-0015).
"""

from django.contrib.auth.models import User
from rest_framework import status
from rest_framework.generics import RetrieveAPIView
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView, TokenViewBase

from audit.models import AuditAction
from audit.services import record_audit
from common.auth import request_user

from .api_exceptions import InvalidMFACode, MFAAlreadyEnrolled, MFANotEnrolled
from .models import AuthSession, MfaDevice, RevokeReason
from .serializers import (
    LogoutSerializer,
    MfaCodeSerializer,
    MFAVerifySerializer,
    RegisterSerializer,
    UserSerializer,
)
from .services import (
    confirm_totp,
    confirmed_device,
    enroll_totp,
    provisioning_uri,
    qr_svg_data_uri,
    revoke_session,
    verify_totp,
)


class RegisterView(APIView):
    """Create an account. Returns the new user; never a token — registration is not a login."""

    permission_classes = (AllowAny,)
    authentication_classes = ()
    throttle_scope = "register"

    def post(self, request: Request) -> Response:
        serializer = RegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        # Read back through UserSerializer: the response carries mfa_enabled and date_joined, and
        # structurally cannot echo the password field.
        return Response(UserSerializer(user).data, status=status.HTTP_201_CREATED)


class MeView(RetrieveAPIView[User]):
    """The requesting user's own profile — never anyone else's, by construction."""

    serializer_class = UserSerializer

    def get_object(self) -> User:
        return request_user(self.request)


# ------------------------------------------------------------------------------------------------
# Token lifecycle
# ------------------------------------------------------------------------------------------------


class LoginView(TokenObtainPairView):
    """Password step. Returns a token pair, or an MFA challenge (ADR-0012).

    The serializer comes from ``SIMPLE_JWT["TOKEN_OBTAIN_SERIALIZER"]``; this subclass exists only
    to carry the throttle scope. ``TokenViewBase`` already opts out of authentication and the
    ``IsAuthenticated`` default.
    """

    throttle_scope = "login"


class MFAVerifyView(TokenViewBase):
    """Second step: spend the pre-auth token plus a TOTP code for a real token pair."""

    serializer_class = MFAVerifySerializer
    throttle_scope = "mfa"


class SessionRefreshView(TokenRefreshView):
    """Rotate a refresh token. Reuse of a rotated token revokes the whole family (ADR-0013)."""

    throttle_scope = "refresh"


class LogoutView(APIView):
    """Blacklist the presented refresh token and revoke its session.

    Not ``TokenBlacklistView``: that returns 200 with an empty body, never touches the session, and
    so leaves every access token from the login alive until it expires. 204 because there is
    nothing to say.
    """

    def post(self, request: Request) -> Response:
        serializer = LogoutSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        serializer.logout()
        return Response(status=status.HTTP_204_NO_CONTENT)


# ------------------------------------------------------------------------------------------------
# MFA management
# ------------------------------------------------------------------------------------------------


class MfaEnrollView(APIView):
    """Create an unconfirmed device and hand back its secret exactly once, with a QR to scan."""

    throttle_scope = "mfa"

    def post(self, request: Request) -> Response:
        user = request_user(request)
        if confirmed_device(user) is not None:
            raise MFAAlreadyEnrolled

        device = enroll_totp(user)
        uri = provisioning_uri(device, user)
        record_audit(
            action=AuditAction.MFA_ENROLLED,
            actor=user,
            target_type="mfa_device",
            target_id=str(device.id),
        )
        return Response(
            {"secret": device.secret, "otpauth_uri": uri, "qr_svg": qr_svg_data_uri(uri)},
            status=status.HTTP_201_CREATED,
        )


class MfaConfirmView(APIView):
    """Activate an enrolled device. Until this succeeds, MFA is not enforced at login."""

    throttle_scope = "mfa"

    def post(self, request: Request) -> Response:
        user = request_user(request)
        device = MfaDevice.objects.filter(user=user, confirmed_at__isnull=True).first()
        if device is None:
            raise MFANotEnrolled

        serializer = MfaCodeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        if not confirm_totp(device, serializer.validated_data["code"]):
            record_audit(action=AuditAction.MFA_FAILED, actor=user)
            raise InvalidMFACode

        record_audit(
            action=AuditAction.MFA_CONFIRMED,
            actor=user,
            target_type="mfa_device",
            target_id=str(device.id),
        )
        return Response({"mfa_enabled": True})


class MfaDisableView(APIView):
    """Turn MFA off, proving possession of the device first.

    Every session is revoked on the way out: changing the authentication requirements of an account
    is exactly when you want any session an attacker may hold to stop working.
    """

    throttle_scope = "mfa"

    def post(self, request: Request) -> Response:
        user = request_user(request)
        device = confirmed_device(user)
        if device is None:
            raise MFANotEnrolled

        serializer = MfaCodeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        if not verify_totp(device, serializer.validated_data["code"]):
            record_audit(action=AuditAction.MFA_FAILED, actor=user)
            raise InvalidMFACode

        device_id = str(device.id)
        device.delete()
        record_audit(
            action=AuditAction.MFA_DISABLED,
            actor=user,
            target_type="mfa_device",
            target_id=device_id,
        )
        for session in AuthSession.objects.filter(user=user, revoked_at__isnull=True):
            revoke_session(str(session.id), reason=RevokeReason.MFA_CHANGED)

        return Response(status=status.HTTP_204_NO_CONTENT)
