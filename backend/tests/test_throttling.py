"""Scoped rate limits on the auth and transfer endpoints (ADR-0015).

Three mechanics to know before reading these, all of them import-time traps:

* **Rates cannot be lowered with the ``settings`` fixture.**
  ``SimpleRateThrottle.THROTTLE_RATES = api_settings.DEFAULT_THROTTLE_RATES`` is evaluated once,
  at class-definition time (``rest_framework/throttling.py:66``), and DRF's ``reload_api_settings``
  receiver rebuilds ``api_settings`` without rebinding it. Override ``DEFAULT_THROTTLE_RATES`` in a
  test and throttling quietly keeps using the real rates — the test then passes or fails for
  reasons unrelated to what it claims to check. The ``throttle_rates`` fixture below patches the
  class attribute instead, which is the thing actually read.
* **Throttle classes are likewise bound at import**, onto ``APIView.throttle_classes``. These tests
  never try to swap them.
* The autouse ``_clear_cache`` fixture in ``conftest.py`` is what keeps this file from poisoning
  every other test in the suite: throttle history lives in the default cache, and every test client
  request arrives from the same address.
"""

from collections.abc import Callable, Iterator

import pytest
from django.contrib.auth.models import User
from django.urls import reverse
from rest_framework.test import APIClient
from rest_framework.throttling import SimpleRateThrottle

pytestmark = pytest.mark.django_db


@pytest.fixture
def throttle_rates() -> Iterator[Callable[..., None]]:
    """Temporarily lower throttle rates by patching the attribute throttles actually read."""
    original = SimpleRateThrottle.THROTTLE_RATES
    SimpleRateThrottle.THROTTLE_RATES = dict(original)

    def _set(**rates: str) -> None:
        SimpleRateThrottle.THROTTLE_RATES.update(rates)

    yield _set
    SimpleRateThrottle.THROTTLE_RATES = original


def test_login_endpoint_throttles_with_the_standard_envelope(
    api_client: APIClient, password_user: User, throttle_rates: Callable[..., None]
) -> None:
    throttle_rates(login="2/min")
    payload = {"username": password_user.username, "password": "wrong-password"}

    for _ in range(2):
        assert (
            api_client.post(reverse("token_obtain_pair"), payload, format="json").status_code == 401
        )

    throttled = api_client.post(reverse("token_obtain_pair"), payload, format="json")
    assert throttled.status_code == 429
    # A 429 is an error like any other and wears the ADR-0006 envelope.
    body = throttled.json()
    assert body["error"]["code"] == "throttled"
    assert body["error"]["message"]
    assert "Retry-After" in throttled.headers


def test_register_endpoint_is_throttled(
    api_client: APIClient, throttle_rates: Callable[..., None]
) -> None:
    throttle_rates(register="1/hour")

    first = api_client.post(
        reverse("register"),
        {"username": "a1", "email": "a1@example.com", "password": "corr3ct-h0rse-batt"},
        format="json",
    )
    assert first.status_code == 201

    second = api_client.post(
        reverse("register"),
        {"username": "a2", "email": "a2@example.com", "password": "corr3ct-h0rse-batt"},
        format="json",
    )
    assert second.status_code == 429
    assert not User.objects.filter(username="a2").exists()


def test_scopes_are_independent(
    api_client: APIClient, password_user: User, throttle_rates: Callable[..., None]
) -> None:
    """Exhausting the MFA scope must not lock out login — different attacks, different budgets."""
    throttle_rates(mfa="1/min", login="10/min")

    for _ in range(2):
        api_client.post(
            reverse("token_mfa_verify"),
            {"mfa_token": "nonsense", "code": "000000"},
            format="json",
        )

    exhausted = api_client.post(
        reverse("token_mfa_verify"), {"mfa_token": "nonsense", "code": "000000"}, format="json"
    )
    assert exhausted.status_code == 429

    login = api_client.post(
        reverse("token_obtain_pair"),
        {"username": password_user.username, "password": "sw0rdf1sh-test-pw"},
        format="json",
    )
    assert login.status_code == 200


def test_health_is_never_throttled(
    api_client: APIClient, throttle_rates: Callable[..., None]
) -> None:
    """A liveness probe that 429s takes the service out of rotation for being polled."""
    throttle_rates(anon="1/min")

    for _ in range(20):
        assert api_client.get(reverse("health")).status_code == 200


def test_transfer_endpoint_is_scoped(
    auth_client: APIClient, throttle_rates: Callable[..., None]
) -> None:
    throttle_rates(transfer="1/min")
    payload = {"source_account": "not-a-uuid", "destination_account": "x", "amount": "1.0000"}

    assert auth_client.post(reverse("transfer-create"), payload, format="json").status_code == 400
    assert auth_client.post(reverse("transfer-create"), payload, format="json").status_code == 429


def test_unscoped_authenticated_endpoints_still_have_a_ceiling(
    auth_client: APIClient, throttle_rates: Callable[..., None]
) -> None:
    """Forgetting a ``throttle_scope`` degrades to "still limited", never to "unlimited"."""
    throttle_rates(user="2/min")

    for _ in range(2):
        assert auth_client.get(reverse("me")).status_code == 200

    assert auth_client.get(reverse("me")).status_code == 429
