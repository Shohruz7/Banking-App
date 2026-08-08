"""Probes and the error-envelope holes (ADR-0028).

Two things this closes. ``/health/`` reported "ok" with Postgres and Redis both down, because it
checked nothing — so it could not be used for readiness, and using it that way would have kept a
broken container in rotation. And ``common.exceptions`` only wraps exceptions DRF raised, so an
unrouted path and an unhandled 500 returned Django's HTML pages: a client that always parsed
``response.json()["error"]`` broke on exactly the two responses it most needed to handle.
"""

import json
from unittest import mock

import pytest
from rest_framework.test import APIClient


def test_liveness_needs_no_database(client: APIClient) -> None:
    """No ``django_db`` marker on purpose.

    The absence of the marker *is* the assertion: if ``/health/`` touched the database this test
    would error rather than pass. A liveness probe that fails during a database blip asks the
    supervisor to restart every working container at once.
    """
    response = client.get("/api/v1/health/")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.django_db
def test_readiness_reports_each_dependency(client: APIClient) -> None:
    response = client.get("/api/v1/ready/")
    assert response.status_code == 200
    assert response.json() == {"status": "ready", "checks": {"database": True, "cache": True}}


@pytest.mark.django_db
def test_readiness_is_503_when_the_database_is_unreachable(client: APIClient) -> None:
    """And it names which dependency, so the probe is a diagnosis rather than a shrug.

    Capable of failing against the natural implementation that lets the exception escape: that
    returns 500, which reads as a broken probe rather than an honest "not ready".
    """
    with mock.patch("common.views.connection.cursor", side_effect=OSError("no route to host")):
        response = client.get("/api/v1/ready/")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not ready"
    assert body["checks"]["database"] is False
    assert body["checks"]["cache"] is True


def test_an_unrouted_path_returns_the_error_envelope(client: APIClient) -> None:
    """Capable of failing against today's Django default, which returns an HTML 404 page."""
    response = client.get("/api/v1/no-such-thing/")

    assert response.status_code == 404
    assert response["Content-Type"] == "application/json"
    assert response.json()["error"]["code"] == "not_found"


def test_the_500_handler_returns_the_envelope_and_no_detail() -> None:
    """The body carries a request id and nothing else — the traceback belongs in the log.

    Called directly rather than through a deliberately-broken view: ``raise_request_exception`` and
    ``DEBUG`` interact in ways that make "provoke a real 500 in a test client" prove less than it
    appears to. What matters is the shape, and that it discloses nothing.
    """
    from django.test import RequestFactory

    from common.views import server_error

    response = server_error(RequestFactory().get("/boom/"))

    assert response.status_code == 500
    # A bare JsonResponse, not a test-client response, so the body is decoded by hand.
    payload = json.loads(response.content)["error"]
    assert payload["code"] == "server_error"
    assert payload["message"] == "Something went wrong."
    assert set(payload["details"]) == {"request_id"}


def test_log_records_carry_the_request_id() -> None:
    """The id ``audit.middleware`` has generated since Week 4 finally reaches a log line.

    Capable of failing against a filter that returns the record untouched: the format string in
    ``LOGGING`` references ``%(request_id)s``, so a missing attribute is a formatting error rather
    than a silently absent field.
    """
    import logging

    from audit.context import audit_context
    from common.logging import RequestIDFilter

    record = logging.LogRecord("t", logging.INFO, __file__, 1, "msg", None, None)
    with audit_context(request_id="abc-123"):
        assert RequestIDFilter().filter(record) is True
    assert record.request_id == "abc-123"  # type: ignore[attr-defined]

    # Work with no request behind it — Beat, a management command — is labelled, not blank.
    outside = logging.LogRecord("t", logging.INFO, __file__, 1, "msg", None, None)
    RequestIDFilter().filter(outside)
    assert outside.request_id == "-"  # type: ignore[attr-defined]
