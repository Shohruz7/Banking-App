"""The price engine (ADR-0017).

Nothing here asserts against an unseeded random draw. A geometric Brownian motion test that says
"the price went up" is a coin flip that fails one CI run in two; the properties worth asserting are
determinism under a seed, and the invariants that must hold for *every* path.
"""

from decimal import Decimal

import pytest
from django.test import override_settings

from markets.pricing import (
    PRICE_FLOOR,
    FixedPriceSource,
    GBMPriceSource,
    ScriptedPriceSource,
    get_price_source,
)
from tests.factories import InstrumentFactory

pytestmark = pytest.mark.django_db


def test_gbm_is_deterministic_under_a_seed() -> None:
    """Same seed, same series — which is what makes any of this testable at all."""

    def walk(seed: int) -> list[Decimal]:
        # A fresh instrument per walk: next_price reads last_price, so reusing one would make the
        # second walk start where the first ended and this test would never compare like with like.
        instrument = InstrumentFactory.build(initial_price=Decimal("100.0000"))
        source = GBMPriceSource(seed=seed, interval_seconds=60)
        prices = []
        for _ in range(20):
            instrument.last_price = source.next_price(instrument)
            prices.append(instrument.last_price)
        return prices

    assert walk(1234) == walk(1234)
    assert walk(1234) != walk(9999), "different seeds produced identical paths"


def test_gbm_price_never_reaches_zero() -> None:
    """10k steps at absurd volatility stay above the floor.

    GBM is multiplicative and cannot mathematically reach zero, but quantizing to four decimal
    places can — and a zero price makes every notional zero and every average-cost division a
    ZeroDivisionError.
    """
    instrument = InstrumentFactory.build(
        initial_price=Decimal("1.0000"), drift=Decimal("-0.9000"), volatility=Decimal("5.0000")
    )
    source = GBMPriceSource(seed=7, interval_seconds=3600)

    for _ in range(10_000):
        instrument.last_price = source.next_price(instrument)
        assert instrument.last_price >= PRICE_FLOOR


def test_gbm_walks_from_the_last_price_not_the_seed_price() -> None:
    """Each step starts where the last ended; otherwise the series is noise around a constant."""
    instrument = InstrumentFactory.build(initial_price=Decimal("100.0000"))
    source = GBMPriceSource(seed=3, interval_seconds=60)

    first = source.next_price(instrument)
    instrument.last_price = Decimal("500.0000")
    second = source.next_price(instrument)

    # A 60-second step cannot move a price by 5x, so the second draw must have used the 500.
    assert first < Decimal("200")
    assert second > Decimal("300")


def test_fixed_source_always_returns_the_same_price() -> None:
    instrument = InstrumentFactory.build()
    source = FixedPriceSource(Decimal("42.5"))

    assert source.next_price(instrument) == Decimal("42.5000")
    assert source.next_price(instrument) == Decimal("42.5000")


def test_scripted_source_returns_prices_in_order_then_refuses() -> None:
    """Running past the script raises: a test that ticked more than it scripted is asserting
    something it did not mean to, and silently repeating the last price would hide that."""
    instrument = InstrumentFactory.build()
    source = ScriptedPriceSource([Decimal("10"), Decimal("20")])

    assert source.next_price(instrument) == Decimal("10.0000")
    assert source.next_price(instrument) == Decimal("20.0000")
    with pytest.raises(IndexError, match="exhausted"):
        source.next_price(instrument)


def test_current_price_falls_back_to_the_seed_price_before_any_tick() -> None:
    """A freshly seeded instrument is tradeable immediately, before Beat has ever run."""
    instrument = InstrumentFactory.build(initial_price=Decimal("77.0000"), last_price=None)
    assert instrument.current_price == Decimal("77.0000")

    instrument.last_price = Decimal("81.0000")
    assert instrument.current_price == Decimal("81.0000")


@override_settings(PRICE_SOURCE="markets.pricing.FixedPriceSource")
def test_get_price_source_resolves_the_configured_dotted_path() -> None:
    """Swapping the whole simulation for a live feed is one settings string (ADR-0017)."""
    with pytest.raises(TypeError):
        # FixedPriceSource needs a price; the point is that *this* class was the one constructed.
        get_price_source()


def test_get_price_source_builds_the_default_gbm_source() -> None:
    assert isinstance(get_price_source(), GBMPriceSource)
