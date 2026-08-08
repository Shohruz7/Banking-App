"""Schema helpers that teach drf-spectacular about this project's own conventions (ADR-0028).

Generation is only worth doing if the result is accurate. Left alone, spectacular emitted 60 errors
and quietly omitted every plain ``APIView`` from the document — which would have been worse than the
hand-maintained README table it replaces, because a generated schema *looks* authoritative.
"""

from typing import Any

from drf_spectacular.extensions import OpenApiAuthenticationExtension
from drf_spectacular.utils import OpenApiExample, OpenApiResponse
from rest_framework import serializers


class SessionAwareJWTScheme(OpenApiAuthenticationExtension):
    """Describe ``identity.authentication.SessionAwareJWTAuthentication`` as plain bearer auth.

    Spectacular resolves authentication classes by exact type, and the project's is a subclass it
    has never seen, so every authenticated endpoint was documented as requiring nothing at all. From
    a client's point of view the scheme *is* ordinary JWT bearer auth — the session binding
    (ADR-0013) changes when a token stops working, not how it is presented.
    """

    target_class = "identity.authentication.SessionAwareJWTAuthentication"
    name = "jwtAuth"

    def get_security_definition(self, auto_schema: Any) -> dict[str, Any]:
        return {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "JWT",
            "description": (
                "Access token from `POST /api/v1/auth/token/`. Tokens are bound to a revocable "
                "session, so logging out invalidates the access token immediately rather than "
                "waiting for it to expire."
            ),
        }


class ErrorBodySerializer(serializers.Serializer[dict[str, object]]):
    code = serializers.CharField(help_text="Machine-readable code clients branch on.")
    message = serializers.CharField()
    details = serializers.DictField(required=False)


class ErrorEnvelopeSerializer(serializers.Serializer[dict[str, object]]):
    """The ADR-0006 error envelope, as one reusable schema component.

    One class rather than an ``inline_serializer`` per call site, and the reason is concrete:
    inline serializers mint a new class each time, so fifteen call sites produce fifteen components
    with the same name and spectacular resolves the collision by guessing. An error shape documented
    fifteen slightly different ways is a shape no client can rely on.
    """

    error = ErrorBodySerializer()


def error_response(code: str, message: str) -> Any:
    """The error envelope, annotated with the code this particular endpoint returns.

    ``code`` and ``message`` are for the human reading the docs; the schema component is shared.
    """
    return OpenApiResponse(
        response=ErrorEnvelopeSerializer,
        description=f"`{code}` — {message}",
        examples=[error_example(code, message)],
    )


def error_example(code: str, message: str) -> OpenApiExample:
    return OpenApiExample(
        code,
        value={"error": {"code": code, "message": message, "details": {}}},
        response_only=True,
    )
