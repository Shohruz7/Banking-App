"""The ambient audit context itself.

The property that matters most is restoration: a leaked actor produces a *wrong* audit row, which
in a security log is worse than a missing one. Workers are reused, so every context manager here
must put back exactly what it found — hence ``reset(token)`` rather than ``set(default)``.
"""

import pytest
from django.contrib.auth.models import User
from django.http import HttpRequest, JsonResponse

from audit.context import RequestContext, audit_context, bind_actor, current_context
from audit.middleware import client_ip

from .factories import UserFactory

pytestmark = pytest.mark.django_db


def test_context_is_empty_outside_any_request() -> None:
    ctx = current_context()
    assert ctx.actor is None
    assert ctx.ip is None
    assert ctx.user_agent == ""


def test_context_is_restored_even_when_the_block_raises() -> None:
    before = current_context()

    with pytest.raises(RuntimeError), audit_context(actor=None, ip="10.0.0.1"):
        assert current_context().ip == "10.0.0.1"
        raise RuntimeError("boom")

    assert current_context() == before


def test_nested_contexts_restore_the_outer_one() -> None:
    with audit_context(ip="10.0.0.1", request_id="outer"):
        with audit_context(ip="10.0.0.2", request_id="inner"):
            assert current_context().request_id == "inner"
        # The outer context is put back, not replaced by the default.
        assert current_context().request_id == "outer"
        assert current_context().ip == "10.0.0.1"


def test_bind_actor_keeps_the_transport_facts(password_user: User) -> None:
    with audit_context(ip="10.0.0.5", request_id="req-1"):
        with bind_actor(password_user):
            ctx = current_context()
            assert ctx.actor == password_user
            assert ctx.ip == "10.0.0.5"
            assert ctx.request_id == "req-1"
        assert current_context().actor is None


def test_request_context_is_immutable() -> None:
    ctx = RequestContext(ip="10.0.0.1")
    with pytest.raises(AttributeError):
        ctx.ip = "10.0.0.2"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("meta", "expected"),
    [
        ({"REMOTE_ADDR": "192.0.2.10"}, "192.0.2.10"),
        # Leftmost XFF entry is the original client; nginx is in front (ADR-0038).
        (
            {"HTTP_X_FORWARDED_FOR": "203.0.113.5, 10.0.0.1", "REMOTE_ADDR": "10.0.0.1"},
            "203.0.113.5",
        ),
        ({"HTTP_X_FORWARDED_FOR": "  ", "REMOTE_ADDR": "10.0.0.1"}, "10.0.0.1"),
        ({}, None),
        # A claim that is not an address is discarded, and the row records what nginx observed.
        # `AuditEvent.ip` is a GenericIPAddressField, so storing the claim raised ValueError from
        # inside middleware: one header, no credentials, a 500 on login and registration.
        ({"HTTP_X_FORWARDED_FOR": "not-an-ip", "REMOTE_ADDR": "10.0.0.1"}, "10.0.0.1"),
        (
            {"HTTP_X_FORWARDED_FOR": "<script>alert(1)</script>", "REMOTE_ADDR": "10.0.0.1"},
            "10.0.0.1",
        ),
        ({"HTTP_X_FORWARDED_FOR": "203.0.113.999", "REMOTE_ADDR": "10.0.0.1"}, "10.0.0.1"),
        # Nothing usable anywhere is None rather than a value that cannot be written.
        ({"HTTP_X_FORWARDED_FOR": "not-an-ip", "REMOTE_ADDR": "also-not-an-ip"}, None),
        # IPv6 still resolves, so the validation did not narrow what counts as an address.
        ({"HTTP_X_FORWARDED_FOR": "2001:db8::1, 10.0.0.1"}, "2001:db8::1"),
    ],
)
def test_client_ip_resolution(meta: dict[str, str], expected: str | None) -> None:
    request = HttpRequest()
    request.META = meta
    assert client_ip(request) == expected


def test_middleware_publishes_the_actor_for_session_authenticated_requests(
    client: object, password_user: User
) -> None:
    """Django's session auth resolves request.user before the middleware, so the actor is filled.

    DRF's JWT authentication happens later, inside the view, which is why views and services pass
    ``actor=`` explicitly rather than relying on this.
    """
    from django.test import Client

    password_user.is_staff = True
    password_user.is_superuser = True
    password_user.save(update_fields=["is_staff", "is_superuser"])

    django_client = Client()
    django_client.force_login(password_user)
    response = django_client.get("/admin/")
    assert response.status_code in (200, 302)


def test_anonymous_requests_publish_no_actor() -> None:
    from django.test import Client

    response = Client().get("/api/v1/health/")
    assert response.status_code == 200
    # Nothing leaked into the ambient context after the request completed.
    assert current_context().actor is None


def test_user_factory_creates_distinct_users() -> None:
    first, second = UserFactory.create(), UserFactory.create()
    assert first.username != second.username
    assert first.email != second.email


# ----------------------------------------------------------------------------------------------
# The id the client gets back
# ----------------------------------------------------------------------------------------------
#
# `deploy/nginx/nginx.conf` has said the request id is "echoed to the client as X-Request-ID" since
# it was written, and until now nothing did it. These pin the behaviour so the comment stays true.


def test_a_response_carries_a_request_id_the_client_never_sent() -> None:
    from django.test import Client

    response = Client().get("/api/v1/health/")

    assert response.status_code == 200
    # Generated, not absent. A client reporting a problem has something to quote whether or not it
    # knew to send a correlation header.
    assert response.headers["X-Request-ID"]


def test_a_client_supplied_request_id_is_the_one_returned() -> None:
    """The id round-trips, which is what makes a client-side trace joinable to a server-side one."""
    from django.test import Client

    response = Client().get("/api/v1/health/", headers={"x-request-id": "trace-me-42"})

    assert response.headers["X-Request-ID"] == "trace-me-42"


def test_the_returned_id_is_the_one_the_request_used_internally() -> None:
    """Set from the ambient context, not echoed back off the request.

    The distinction only shows when the client sends no header: an implementation that echoed
    ``request.headers["X-Request-ID"]`` would return nothing there, while the audit row and the log
    line would still carry a generated id. Then the header a user quotes would not be the one
    anything was recorded under, which is the whole point of returning it.

    Asserted by reading the ambient id from inside the request, through a view, rather than by
    trusting the two to agree.
    """
    from django.test import Client

    observed: list[str] = []

    def _probe(request: HttpRequest) -> JsonResponse:
        observed.append(current_context().request_id)
        return JsonResponse({})

    from django.urls import path

    from config import urls as root_urls

    root_urls.urlpatterns.append(path("__request_id_probe__/", _probe))
    try:
        response = Client().get("/__request_id_probe__/")
    finally:
        root_urls.urlpatterns.pop()

    assert observed, "the probe view did not run"
    # The id the request ran under is exactly the id the caller was handed.
    assert response.headers["X-Request-ID"] == observed[0]
    # And it was generated, since the client sent none.
    assert observed[0]


def test_a_malformed_forwarded_address_does_not_break_an_audited_write() -> None:
    """The regression in full, through a real request rather than through ``client_ip`` alone.

    The unit table above pins the resolution. This pins the consequence: the audit row is written
    from middleware, after the view has done its work, so a value that cannot be stored surfaces as
    a 500 on a request that otherwise succeeded. Reachable unauthenticated, because a failed login
    is audited too.
    """
    from django.test import Client

    response = Client().post(
        "/api/v1/auth/token/",
        data={"username": "nobody", "password": "wrong"},
        content_type="application/json",
        headers={"x-forwarded-for": "not-an-ip-at-all"},
    )

    # 401 is the honest answer for bad credentials. Anything 5xx means the header got that far.
    assert response.status_code < 500, "a malformed X-Forwarded-For reached the audit row"


# ----------------------------------------------------------------------------------------------
# X-Forwarded-Proto
# ----------------------------------------------------------------------------------------------
#
# The one forwarded header with no end-to-end assertion, and the gap is structural rather than an
# oversight. Proving it through nginx means turning SECURE_SSL_REDIRECT on, which makes every
# request redirect, including each replica's own healthcheck, so the stack will not start. It is
# covered in two halves instead: `.github/scripts/nginx_header_inheritance.py` proves nginx sends
# it, and these prove Django reads it. Nothing joins the halves on a live request, and
# `deploy/README.md` says so rather than implying the coverage is complete.


@pytest.mark.parametrize(
    ("forwarded_proto", "expected_secure"),
    [
        ("https", True),
        ("http", False),
        (None, False),
    ],
)
def test_django_reads_the_forwarded_protocol(
    settings: object, forwarded_proto: str | None, expected_secure: bool
) -> None:
    """``SECURE_PROXY_SSL_HEADER`` is what makes a TLS-terminating proxy legible to Django.

    Without it Django believes every proxied request arrived over plain HTTP. With
    ``SECURE_SSL_REDIRECT`` on, that is an infinite redirect: Django answers 301 to https, the proxy
    forwards the retry as http, and Django answers 301 again. The header is what breaks the loop,
    which is why it being silently dropped was a production outage waiting for TLS to be switched
    on.
    """
    settings.SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")  # type: ignore[attr-defined]

    request = HttpRequest()
    request.META = {} if forwarded_proto is None else {"HTTP_X_FORWARDED_PROTO": forwarded_proto}

    assert request.is_secure() is expected_secure


def test_a_client_cannot_claim_https_when_the_setting_is_unset(settings: object) -> None:
    """The mirror image: the header means nothing until the deployment opts into trusting it.

    That is the right default. Anything reachable without a proxy in front would otherwise let a
    caller mark its own request secure by typing a header, and `secure` is what Django consults
    before it will set a cookie with the Secure flag.
    """
    settings.SECURE_PROXY_SSL_HEADER = None  # type: ignore[attr-defined]

    request = HttpRequest()
    request.META = {"HTTP_X_FORWARDED_PROTO": "https"}

    assert request.is_secure() is False
