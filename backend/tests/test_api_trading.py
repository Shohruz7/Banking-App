"""The brokerage over HTTP, with real Bearer tokens.

Owner scoping, the ADR-0006 error envelope, and the one behavioural change Week 5 makes to an
existing endpoint: position accounts no longer appear in ``GET /accounts/``.
"""

from decimal import Decimal
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.urls import reverse
from rest_framework.test import APIClient

from accounts.models import Account
from markets.models import Instrument
from tests.conftest import obtain_tokens
from tests.factories import AccountFactory, InstrumentFactory, UserFactory, give_shares

pytestmark = pytest.mark.django_db


def order_payload(instrument: Instrument, cash: Account, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "symbol": instrument.symbol,
        "cash_account": str(cash.pk),
        "side": "buy",
        "order_type": "market",
        "quantity": "2",
    }
    payload.update(overrides)
    return payload


def test_market_buy_over_http_returns_a_filled_order(
    auth_client: APIClient, instrument: Instrument, funded_cash_account: Account
) -> None:
    """201 with the order already filled — the client learns the outcome in the same response."""
    response = auth_client.post(
        reverse("order-list-create"),
        order_payload(instrument, funded_cash_account),
        format="json",
    )

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "filled"
    assert body["symbol"] == instrument.symbol
    # Money crosses the wire as a string, never a float (ADR-0009).
    assert body["filled_price"] == "100.0000"
    assert body["quantity"] == "2.00000000"
    assert body["entry_id"] is not None


def test_limit_order_over_http_returns_an_open_order(
    auth_client: APIClient, instrument: Instrument, funded_cash_account: Account
) -> None:
    """Also 201: the order was created either way, and `status` says whether money moved."""
    response = auth_client.post(
        reverse("order-list-create"),
        order_payload(instrument, funded_cash_account, order_type="limit", limit_price="90"),
        format="json",
    )

    assert response.status_code == 201
    assert response.json()["status"] == "open"
    assert response.json()["entry_id"] is None


def test_insufficient_funds_uses_the_error_envelope(
    auth_client: APIClient, instrument: Instrument, funded_cash_account: Account
) -> None:
    response = auth_client.post(
        reverse("order-list-create"),
        order_payload(instrument, funded_cash_account, quantity="10000"),
        format="json",
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "insufficient_funds"


def test_insufficient_shares_uses_the_error_envelope(
    auth_client: APIClient, instrument: Instrument, funded_cash_account: Account
) -> None:
    response = auth_client.post(
        reverse("order-list-create"),
        order_payload(instrument, funded_cash_account, side="sell", quantity="5"),
        format="json",
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "insufficient_shares"


def test_unknown_symbol_is_refused(auth_client: APIClient, funded_cash_account: Account) -> None:
    response = auth_client.post(
        reverse("order-list-create"),
        {
            "symbol": "NOPE",
            "cash_account": str(funded_cash_account.pk),
            "side": "buy",
            "order_type": "market",
            "quantity": "1",
        },
        format="json",
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "instrument_not_found"


def test_a_delisted_instrument_cannot_be_traded(
    auth_client: APIClient, funded_cash_account: Account
) -> None:
    delisted = InstrumentFactory.create(symbol="GONE", is_active=False)

    response = auth_client.post(
        reverse("order-list-create"),
        order_payload(delisted, funded_cash_account, quantity="1"),
        format="json",
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "instrument_inactive"


def test_a_limit_order_without_a_price_is_a_field_error(
    auth_client: APIClient, instrument: Instrument, funded_cash_account: Account
) -> None:
    """Caught by the serializer, so the client gets a field-level 400 rather than a 500."""
    response = auth_client.post(
        reverse("order-list-create"),
        order_payload(instrument, funded_cash_account, order_type="limit"),
        format="json",
    )

    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "validation_error"
    assert "limit_price" in body["error"]["details"]


def test_someone_elses_cash_account_is_a_404_not_a_403(
    auth_client: APIClient, instrument: Instrument
) -> None:
    """A 403 would confirm the account exists — the accounts app's rule, applied to trading."""
    stranger = AccountFactory.create()

    response = auth_client.post(
        reverse("order-list-create"),
        order_payload(instrument, stranger),
        format="json",
    )

    assert response.status_code == 404


def test_orders_are_owner_scoped(
    api_client: APIClient, instrument: Instrument, password_user: User, funded_cash_account: Account
) -> None:
    """Two users, two tokens: each sees only its own orders, and cannot fetch the other's."""
    mine = obtain_tokens(APIClient(), password_user)
    my_client = APIClient()
    my_client.credentials(HTTP_AUTHORIZATION=f"Bearer {mine['access']}")
    created = my_client.post(
        reverse("order-list-create"),
        order_payload(instrument, funded_cash_account),
        format="json",
    ).json()

    other_user = UserFactory.create()
    theirs = obtain_tokens(APIClient(), other_user)
    their_client = APIClient()
    their_client.credentials(HTTP_AUTHORIZATION=f"Bearer {theirs['access']}")

    assert their_client.get(reverse("order-list-create")).json() == []
    assert their_client.get(reverse("order-detail", args=[created["id"]])).status_code == 404
    assert len(my_client.get(reverse("order-list-create")).json()) == 1


def test_cancelling_a_filled_order_is_a_conflict(
    auth_client: APIClient, instrument: Instrument, funded_cash_account: Account
) -> None:
    """409, not 400: the request is well-formed, the order's state has simply moved on."""
    created = auth_client.post(
        reverse("order-list-create"),
        order_payload(instrument, funded_cash_account),
        format="json",
    ).json()

    response = auth_client.post(reverse("order-cancel", args=[created["id"]]))

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "order_not_open"


def test_cancelling_a_resting_order_succeeds(
    auth_client: APIClient, instrument: Instrument, funded_cash_account: Account
) -> None:
    created = auth_client.post(
        reverse("order-list-create"),
        order_payload(instrument, funded_cash_account, order_type="limit", limit_price="50"),
        format="json",
    ).json()

    response = auth_client.post(reverse("order-cancel", args=[created["id"]]))

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"


def test_holdings_report_quantity_basis_and_market_value(
    auth_client: APIClient, password_user: User, instrument: Instrument
) -> None:
    """Everything derived: shares and basis from the ledger, the rest arithmetic on top."""
    give_shares(password_user, instrument, Decimal("4"), Decimal("400.00"))
    instrument.last_price = Decimal("150.0000")
    instrument.save(update_fields=["last_price"])

    response = auth_client.get(reverse("holdings"))

    assert response.status_code == 200
    holding = response.json()[0]
    assert holding["symbol"] == instrument.symbol
    assert holding["quantity"] == "4.00000000"
    assert holding["cost_basis"] == "400.0000"
    assert holding["average_cost"] == "100.0000"
    assert holding["last_price"] == "150.0000"
    assert holding["market_value"] == "600.0000"


def test_a_fully_sold_position_is_not_a_holding(
    auth_client: APIClient,
    password_user: User,
    instrument: Instrument,
    funded_cash_account: Account,
) -> None:
    """An account with zero shares is history, not a position."""
    give_shares(password_user, instrument, Decimal("2"), Decimal("200.00"))
    auth_client.post(
        reverse("order-list-create"),
        order_payload(instrument, funded_cash_account, side="sell", quantity="2"),
        format="json",
    )

    assert auth_client.get(reverse("holdings")).json() == []


def test_holdings_are_owner_scoped(auth_client: APIClient, instrument: Instrument) -> None:
    """Another user's position never appears, even though it is a real account row."""
    stranger = UserFactory.create()
    give_shares(stranger, instrument, Decimal("9"), Decimal("900.00"))

    assert auth_client.get(reverse("holdings")).json() == []


def test_position_accounts_are_absent_from_the_accounts_list(
    auth_client: APIClient,
    password_user: User,
    instrument: Instrument,
    funded_cash_account: Account,
) -> None:
    """A position's balance is a cost basis, not spendable cash (ADR-0016).

    No pre-Week-5 test can catch this, because no pre-Week-5 test has a position account.
    """
    give_shares(password_user, instrument, Decimal("3"), Decimal("300.00"))

    names = [row["name"] for row in auth_client.get(reverse("account-list")).json()["results"]]

    assert "Brokerage cash" in names
    assert f"{instrument.symbol} position" not in names


def test_instrument_list_and_detail(auth_client: APIClient, instrument: Instrument) -> None:
    """Symbol is the lookup key, and it is case-insensitive in the URL."""
    listed = auth_client.get(reverse("instrument-list")).json()["results"]
    assert [row["symbol"] for row in listed] == [instrument.symbol]
    # Simulation parameters are never published — that would be the house showing its cards.
    assert "drift" not in listed[0]
    assert "volatility" not in listed[0]

    assert auth_client.get(reverse("instrument-detail", args=["test"])).json()["symbol"] == "TEST"


def test_delisted_instruments_are_hidden_from_the_list_but_still_retrievable(
    auth_client: APIClient, instrument: Instrument
) -> None:
    """Orders and audit rows reference delisted symbols; a 404 on lookup would orphan them."""
    InstrumentFactory.create(symbol="GONE", is_active=False)

    listed = auth_client.get(reverse("instrument-list")).json()["results"]
    assert "GONE" not in [row["symbol"] for row in listed]
    assert auth_client.get(reverse("instrument-detail", args=["GONE"])).status_code == 200


def test_instrument_search_filters_by_symbol_and_name(auth_client: APIClient) -> None:
    InstrumentFactory.create(symbol="AAPL", name="Apple Inc.")
    InstrumentFactory.create(symbol="MSFT", name="Microsoft Corporation")

    found = auth_client.get(reverse("instrument-list"), {"q": "apple"}).json()["results"]
    assert [row["symbol"] for row in found] == ["AAPL"]


def test_price_history_is_newest_first(auth_client: APIClient, instrument: Instrument) -> None:
    from markets.pricing import ScriptedPriceSource
    from markets.tasks import advance_prices

    source = ScriptedPriceSource([Decimal("101"), Decimal("102"), Decimal("103")])
    for _ in range(3):
        advance_prices(source=source)

    prices = auth_client.get(reverse("instrument-prices", args=[instrument.symbol])).json()
    assert [row["price"] for row in prices["results"]] == ["103.0000", "102.0000", "101.0000"]


def test_trading_endpoints_require_authentication(
    api_client: APIClient, instrument: Instrument
) -> None:
    """Locked shut by default, like everything else (ADR-0006)."""
    for url in (reverse("order-list-create"), reverse("holdings"), reverse("instrument-list")):
        assert api_client.get(url).status_code == 401
