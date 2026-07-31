from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView


class HealthView(APIView):
    """Liveness probe; checks nothing yet — dependency checks can be added later."""

    authentication_classes: list[type] = []
    permission_classes = (AllowAny,)
    # A liveness probe that 429s is worse than useless: it would take the service out of rotation
    # for being polled, which is the one thing a probe is supposed to do (ADR-0015).
    throttle_classes: list[type] = []

    def get(self, request: Request) -> Response:
        return Response({"status": "ok"})
