"""Shared fixtures.

The important addition in Week 4 is that authenticated fixtures now carry a **real** token,
obtained over HTTP through the actual login endpoint. Everything before this used
``force_authenticate``, which bypasses ``authenticate()`` and the authentication class entirely —
so the whole auth stack could have been broken and every test would still have passed.
"""

from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import pyotp
import pytest
from django.contrib.auth.models import User
from django.core.cache import cache
from django.urls import reverse
from rest_framework.test import APIClient

from accounts.models import Account
from identity.models import MfaDevice
from markets.models import Instrument

from .factories import (
    TEST_PASSWORD,
    AccountFactory,
    InstrumentFactory,
    MfaDeviceFactory,
    UserFactory,
    fund_account,
)


@pytest.fixture
def api_client() -> APIClient:
    return APIClient()


@pytest.fixture(autouse=True)
def _clear_cache() -> Iterator[None]:
    """Reset throttle history and pre-auth token state between tests.

    DRF keeps throttle counters in the default cache, and LocMemCache is a module-level dict that
    outlives a test. Every test client request also arrives from 127.0.0.1, so without this every
    anonymous test shares one bucket and test *order* starts deciding outcomes.
    """
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def password_user() -> User:
    """A user whose password is known, so tests can actually log in."""
    return UserFactory.create()


@pytest.fixture
def mfa_device(password_user: User) -> MfaDevice:
    """A confirmed authenticator for ``password_user``; read ``.secret`` to generate codes."""
    return MfaDeviceFactory.create(user=password_user, confirmed=True)


def obtain_tokens(
    client: APIClient, user: User, *, password: str = TEST_PASSWORD, code: str | None = None
) -> dict[str, Any]:
    """Log in over HTTP and return the response body.

    Returns ``{"access", "refresh"}``, or ``{"mfa_required", "mfa_token"}`` when the account has a
    confirmed device and no ``code`` was supplied. Passing ``code`` completes the second step.
    """
    response = client.post(
        reverse("token_obtain_pair"),
        {"username": user.username, "password": password},
        format="json",
    )
    body: dict[str, Any] = response.json()

    if body.get("mfa_required") and code is not None:
        verified = client.post(
            reverse("token_mfa_verify"),
            {"mfa_token": body["mfa_token"], "code": code},
            format="json",
        )
        return dict(verified.json())

    return body


@pytest.fixture
def token_pair(api_client: APIClient, password_user: User) -> dict[str, str]:
    """A real access/refresh pair for a user without MFA."""
    tokens = obtain_tokens(api_client, password_user)
    return {"access": tokens["access"], "refresh": tokens["refresh"]}


@pytest.fixture
def auth_client(token_pair: dict[str, str]) -> APIClient:
    """A client carrying a real Bearer access token — not ``force_authenticate``."""
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token_pair['access']}")
    return client


def totp_now(secret: str) -> str:
    """The code an authenticator app would show right now."""
    return pyotp.TOTP(secret).now()


@pytest.fixture
def instrument() -> Instrument:
    """A tradeable symbol sitting at exactly $100, with no tick history."""
    return InstrumentFactory.create(symbol="TEST", name="Test Corp.")


@pytest.fixture
def funded_cash_account(password_user: User) -> Account:
    """``password_user``'s cash account, holding $10,000 — enough to trade with."""
    account = AccountFactory.create(owner=password_user, name="Brokerage cash")
    fund_account(account, Decimal("10000.00"))
    return account
