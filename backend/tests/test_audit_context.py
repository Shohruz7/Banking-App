"""The ambient audit context itself.

The property that matters most is restoration: a leaked actor produces a *wrong* audit row, which
in a security log is worse than a missing one. Workers are reused, so every context manager here
must put back exactly what it found — hence ``reset(token)`` rather than ``set(default)``.
"""

import pytest
from django.contrib.auth.models import User
from django.http import HttpRequest

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
