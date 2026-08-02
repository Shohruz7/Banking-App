"""The scheduled market tasks (ADR-0019).

Celery runs eagerly in the test settings, so ``.delay()`` executes in-process and neither the suite
nor CI needs a broker. The one thing eager mode does *not* do is fire ``transaction.on_commit``
callbacks inside a ``django_db`` test — the enclosing transaction never commits — which is why the
chaining test below uses ``django_capture_on_commit_callbacks``. Without it that test would pass
while asserting nothing.
"""

from collections.abc import Callable
from decimal import Decimal
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.core.cache import cache

from accounts.models import Account
from audit.models import AuditAction, AuditEvent
from ledger.exceptions import InsufficientFundsError
from markets.models import Instrument, PriceTick
from markets.pricing import FixedPriceSource, ScriptedPriceSource
from markets.tasks import _ADVANCE_LOCK_KEY, advance_prices
from tests.factories import InstrumentFactory
from trading.models import OrderSide, OrderStatus, OrderType
from trading.services import place_order

pytestmark = pytest.mark.django_db


def test_advance_prices_writes_one_tick_per_active_instrument(instrument: Instrument) -> None:
    """Inactive instruments are skipped — delisted means it stops moving, not that it disappears."""
    delisted = InstrumentFactory.create(symbol="DEAD", is_active=False)

    result = advance_prices(source=FixedPriceSource(Decimal("123.45")))

    assert result == {"skipped": False, "ticked": 1}
    assert PriceTick.objects.filter(instrument=instrument).count() == 1
    assert PriceTick.objects.filter(instrument=delisted).count() == 0


def test_advance_prices_updates_the_cached_last_price(instrument: Instrument) -> None:
    """The denormalized field and the newest tick are written together, so they cannot disagree."""
    advance_prices(source=FixedPriceSource(Decimal("111.1100")))

    instrument.refresh_from_db()
    newest = PriceTick.objects.filter(instrument=instrument).latest("created_at")

    assert instrument.last_price == newest.price == Decimal("111.1100")
    assert instrument.last_tick_at is not None


def test_advance_prices_walks_the_series_forward(instrument: Instrument) -> None:
    """Three fires, three ticks, in order."""
    source = ScriptedPriceSource([Decimal("101"), Decimal("102"), Decimal("103")])
    for _ in range(3):
        advance_prices(source=source)

    prices = list(
        PriceTick.objects.filter(instrument=instrument)
        .order_by("id")
        .values_list("price", flat=True)
    )
    assert prices == [Decimal("101.0000"), Decimal("102.0000"), Decimal("103.0000")]


def test_advance_prices_is_guarded_against_overlap(instrument: Instrument) -> None:
    """Beat fires on a fixed interval whether or not the last run finished.

    The mutex is taken with ``cache.add``, which sets only if absent — so a second concurrent run
    is a no-op instead of a second walk of the same prices.
    """
    cache.add(_ADVANCE_LOCK_KEY, "1", 300)  # simulate a run already in flight

    result = advance_prices(source=FixedPriceSource(Decimal("999")))

    assert result == {"skipped": True, "ticked": 0}
    assert PriceTick.objects.count() == 0


def test_advance_prices_releases_its_lock(instrument: Instrument) -> None:
    """A held lock would wedge the market until it expired; the release is in a finally."""
    advance_prices(source=FixedPriceSource(Decimal("100")))
    assert cache.get(_ADVANCE_LOCK_KEY) is None

    # And the next fire works, which is the property that actually matters.
    assert advance_prices(source=FixedPriceSource(Decimal("100")))["ticked"] == 1


def test_advance_prices_chains_the_matching_sweep(
    django_capture_on_commit_callbacks: Callable[..., Any], instrument: Instrument
) -> None:
    """The tick task dispatches matching through on_commit, so no worker reads an unlanded tick."""
    with django_capture_on_commit_callbacks(execute=True) as callbacks:
        advance_prices(source=FixedPriceSource(Decimal("100")))

    assert len(callbacks) == 1, "advance_prices did not dispatch the matching sweep"


def test_a_fill_from_a_celery_task_still_writes_an_audit_row(
    password_user: User, instrument: Instrument, funded_cash_account: Account
) -> None:
    """The reason ADR-0014 chose contextvars: there is no request here, and no middleware.

    A resting order filled by the sweep must be attributed to its owner just as a fill placed over
    HTTP is — the actor is a domain fact the task knows, not something read off a request.
    """
    place_order(
        user=password_user,
        instrument=instrument,
        cash_account=funded_cash_account,
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Decimal("2"),
        limit_price=Decimal("95"),
    )

    from trading.tasks import match_resting_orders

    advance_prices(source=FixedPriceSource(Decimal("90")))
    match_resting_orders()

    filled = AuditEvent.objects.filter(action=AuditAction.ORDER_FILLED)
    assert filled.count() == 1
    row = filled.get()
    assert row.actor == password_user
    assert row.actor_label == password_user.username
    # No request means no transport facts — and that is fine, not an error.
    assert row.ip is None
    assert row.context["symbol"] == instrument.symbol


def test_order_lifecycle_is_fully_audited(
    password_user: User, instrument: Instrument, funded_cash_account: Account
) -> None:
    """Placed, rested, filled, cancelled and rejected each leave a row."""
    from trading.services import cancel_order

    # A market buy: placed + filled.
    place_order(
        user=password_user,
        instrument=instrument,
        cash_account=funded_cash_account,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal("1"),
    )
    # A limit order that is then cancelled: placed + rested + cancelled.
    resting = place_order(
        user=password_user,
        instrument=instrument,
        cash_account=funded_cash_account,
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Decimal("1"),
        limit_price=Decimal("50"),
    )
    cancel_order(resting, actor=password_user)

    # A market buy that cannot be afforded: placed + rejected.
    with pytest.raises(InsufficientFundsError):
        place_order(
            user=password_user,
            instrument=instrument,
            cash_account=funded_cash_account,
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=Decimal("10000"),
        )

    actions = list(AuditEvent.objects.values_list("action", flat=True))
    assert actions.count(AuditAction.ORDER_PLACED) == 3
    assert actions.count(AuditAction.ORDER_RESTED) == 1
    assert actions.count(AuditAction.ORDER_FILLED) == 1
    assert actions.count(AuditAction.ORDER_CANCELLED) == 1
    assert actions.count(AuditAction.ORDER_REJECTED) == 1


def test_a_rejected_order_row_survives_the_failed_transaction(
    password_user: User, instrument: Instrument, funded_cash_account: Account
) -> None:
    """The ADR-0014 split, on the trading path.

    The fill raises inside ``execute_fill``'s atomic block. A rejection written there would be
    rolled back by the very exception it records; written after the block exits, it sticks.
    """
    from ledger.exceptions import InsufficientFundsError

    with pytest.raises(InsufficientFundsError):
        place_order(
            user=password_user,
            instrument=instrument,
            cash_account=funded_cash_account,
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=Decimal("999999"),
        )

    assert AuditEvent.objects.filter(action=AuditAction.ORDER_REJECTED).count() == 1
    # And no financial trace at all — only the fixture's opening balance.
    assert AuditEvent.objects.filter(action=AuditAction.ORDER_FILLED).count() == 0

    from trading.models import Order

    assert Order.objects.get().status == OrderStatus.REJECTED


def test_order_audit_context_never_records_a_credential(
    password_user: User, instrument: Instrument, funded_cash_account: Account
) -> None:
    """Order context uses symbol/side/quantity/price — none of which the scrubber redacts."""
    place_order(
        user=password_user,
        instrument=instrument,
        cash_account=funded_cash_account,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal("1"),
    )

    for row in AuditEvent.objects.all():
        assert "[redacted]" not in str(row.context), f"{row.action} lost a field to the scrubber"
