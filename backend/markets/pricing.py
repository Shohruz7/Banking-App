"""Price sources: the seam between "where a price comes from" and everything that uses one.

Prices in v1 are simulated with geometric Brownian motion (ADR-0017). Nothing downstream knows
that: the fill path and the tick task both depend on the :class:`PriceSource` protocol, so a live
market-data feed becomes a new class and one settings string, not a rewrite.

GBM is the standard model for an equity price under Black-Scholes assumptions::

    S(t+Δt) = S(t) · exp( (μ − σ²/2)·Δt + σ·√Δt·Z ),   Z ~ N(0, 1)

The ``−σ²/2`` correction is what makes ``μ`` the expected *log* return; drop it and the simulated
series drifts upward faster than its stated drift, which is the classic way to accidentally build
an instrument that only goes up.
"""

import math
import random
from decimal import Decimal
from typing import Protocol

from django.conf import settings
from django.utils.module_loading import import_string

from common.money import quantize_money

from .models import Instrument

#: Calendar seconds per year. Calendar, not trading, because this market ticks 24/7 (ADR-0018) —
#: annualized parameters have to be scaled by the clock the simulation actually runs on.
SECONDS_PER_YEAR = 365 * 24 * 60 * 60

#: Prices are floored here rather than allowed to reach zero. GBM is multiplicative and cannot
#: mathematically reach zero, but quantizing to four decimal places can, and a zero price would
#: make a fill notional of zero — which ``post_entry`` rejects — or a division by zero downstream.
PRICE_FLOOR = Decimal("0.01")


class PriceSource(Protocol):
    """Anything that can say what an instrument's next price is."""

    def next_price(self, instrument: Instrument) -> Decimal:
        """Return the instrument's next price, quantized to the money precision (ADR-0009)."""
        ...


class GBMPriceSource:
    """Geometric Brownian motion over each instrument's own drift and volatility.

    The random number generator is an instance attribute, not the ``random`` module's global, so a
    test can seed one source without perturbing anything else that draws random numbers — and so
    two sources with the same seed produce the same series, which is what makes the price path
    assertable at all.
    """

    def __init__(self, *, interval_seconds: int | None = None, seed: int | None = None) -> None:
        self.interval_seconds = (
            interval_seconds if interval_seconds is not None else settings.MARKET_TICK_SECONDS
        )
        self._rng = random.Random(seed)

    def next_price(self, instrument: Instrument) -> Decimal:
        spot = float(instrument.current_price)
        mu = float(instrument.drift)
        sigma = float(instrument.volatility)
        dt = self.interval_seconds / SECONDS_PER_YEAR

        drift_term = (mu - 0.5 * sigma**2) * dt
        shock_term = sigma * math.sqrt(dt) * self._rng.gauss(0.0, 1.0)
        nxt = spot * math.exp(drift_term + shock_term)

        # str() rather than Decimal(float): the binary float is converted through its shortest
        # repr, so the value that gets quantized is the one the arithmetic meant.
        return max(quantize_money(Decimal(str(nxt))), PRICE_FLOOR)


class FixedPriceSource:
    """Always returns the same price. For tests, and for a demo that must not move."""

    def __init__(self, price: Decimal) -> None:
        self.price = quantize_money(price)

    def next_price(self, instrument: Instrument) -> Decimal:
        return self.price


class ScriptedPriceSource:
    """Returns a predetermined series, one price per call.

    Exists so a limit-order test can say "the price ticks to 149, then 151" and assert that the
    order filled on the second tick — an assertion no seeded random walk can express directly.
    Running past the end of the script raises rather than repeating: a test that ticks more often
    than it scripted is asserting something it did not mean to.
    """

    def __init__(self, prices: list[Decimal]) -> None:
        self._prices = [quantize_money(price) for price in prices]
        self._index = 0

    def next_price(self, instrument: Instrument) -> Decimal:
        if self._index >= len(self._prices):
            raise IndexError(
                f"ScriptedPriceSource exhausted after {len(self._prices)} prices; "
                f"something advanced the market more often than the test scripted."
            )
        price = self._prices[self._index]
        self._index += 1
        return price


def get_price_source() -> PriceSource:
    """Build the configured price source.

    Resolved by dotted path the way Django resolves its own backends, so switching the whole
    simulation to a live feed is one environment variable. Callers that need determinism pass a
    source explicitly instead of reaching for this.
    """
    source_class = import_string(settings.PRICE_SOURCE)
    return source_class()
