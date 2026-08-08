"""Probes and the last-resort error handlers.

The two probes answer different questions and must not be merged. *Liveness* asks "is this process
wedged, should the supervisor restart it"; *readiness* asks "can this process serve a request right
now, should the load balancer send it one". Answering the first by checking Postgres is how a brief
database blip turns into every container being killed and restarted at once, which is a database
blip plus a cold start.
"""

from django.core.cache import cache
from django.db import connection
from django.http import HttpRequest, JsonResponse
from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import serializers, status
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView


class HealthView(APIView):
    """Liveness probe. Checks nothing, on purpose.

    If this view runs at all, the process is alive and serving — which is the entire question. It
    deliberately does not touch Postgres or Redis: a liveness probe that fails on a dependency
    outage asks the supervisor to restart a process that is working fine, turning a recoverable
    outage into a restart storm on top of one. Readiness is where dependencies belong.
    """

    authentication_classes: list[type] = []
    permission_classes = (AllowAny,)
    # A liveness probe that 429s is worse than useless: it would take the service out of rotation
    # for being polled, which is the one thing a probe is supposed to do (ADR-0015).
    throttle_classes: list[type] = []

    @extend_schema(
        responses=inline_serializer("Liveness", {"status": serializers.CharField()}),
        summary="Liveness probe",
        auth=[],
    )
    def get(self, request: Request) -> Response:
        return Response({"status": "ok"})


class ReadinessView(APIView):
    """Readiness probe — can this process actually serve a request?

    Checks the two dependencies whose absence makes every meaningful endpoint fail: Postgres, which
    every read touches, and the cache, which ``prod.py`` refuses to boot without because throttle
    state lives there (ADR-0015). Returns 503 with a per-dependency breakdown, so a failing probe
    says *which* dependency rather than only that something is wrong.

    The channel layer is deliberately not checked. A socket outage does not make the HTTP API
    unserveable, and taking the whole service out of rotation for it would be a strictly worse
    outcome than the degraded one.
    """

    authentication_classes: list[type] = []
    permission_classes = (AllowAny,)
    throttle_classes: list[type] = []

    @extend_schema(
        responses=inline_serializer(
            "Readiness",
            {"status": serializers.CharField(), "checks": serializers.DictField()},
        ),
        summary="Readiness probe",
        description="503 with a per-dependency breakdown when something it needs is unreachable.",
        auth=[],
    )
    def get(self, request: Request) -> Response:
        checks = {"database": self._database(), "cache": self._cache()}
        ready = all(checks.values())
        return Response(
            {"status": "ready" if ready else "not ready", "checks": checks},
            status=status.HTTP_200_OK if ready else status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    @staticmethod
    def _database() -> bool:
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                return bool(cursor.fetchone() == (1,))
        except Exception:  # noqa: BLE001
            # Any failure is "not ready", whatever it was. Re-raising would make the probe a 500,
            # which reads as a broken probe rather than an honest negative answer — and a probe that
            # only handles the failures someone anticipated is not a probe.
            return False

    @staticmethod
    def _cache() -> bool:
        try:
            cache.set("readiness", "1", timeout=5)
            return bool(cache.get("readiness") == "1")
        except Exception:  # noqa: BLE001 - as above: any failure means not ready
            return False


def not_found(request: HttpRequest, exception: Exception) -> JsonResponse:
    """404s that never reached DRF — an unrouted path — still get the ADR-0006 envelope.

    ``common.exceptions.api_exception_handler`` only wraps exceptions DRF raised, so before this a
    client that always parsed ``response.json()["error"]`` broke on exactly the responses it most
    needed to handle: a typo'd URL and a server error.
    """
    return JsonResponse(
        {"error": {"code": "not_found", "message": "No such endpoint.", "details": {}}},
        status=404,
    )


def server_error(request: HttpRequest) -> JsonResponse:
    """The same for an unhandled 500.

    Says nothing about what broke. The traceback goes to the log, where the ``request_id`` filter
    ties it to this request; the response says only that the request id exists, so a user can quote
    it in a support conversation without the response body becoming a debugging surface.
    """
    from audit.context import current_context

    return JsonResponse(
        {
            "error": {
                "code": "server_error",
                "message": "Something went wrong.",
                "details": {"request_id": current_context().request_id or ""},
            }
        },
        status=500,
    )
