"""The instrument seed command.

Idempotence is the property that matters: this runs in a fresh checkout, on every demo reset, and
from Week 8's Faker seed, and none of those may produce duplicates or fail on a second pass.
"""

from decimal import Decimal
from io import StringIO

import pytest
from django.core.management import call_command

from markets.management.commands.seed_instruments import INSTRUMENTS
from markets.models import Instrument, PriceTick

pytestmark = pytest.mark.django_db


def seed(**options: object) -> str:
    out = StringIO()
    call_command("seed_instruments", stdout=out, **options)
    return out.getvalue()


def test_seeds_the_whole_universe() -> None:
    """The headline number the design doc promises: 50+ tickers."""
    seed()

    assert Instrument.objects.count() == len(INSTRUMENTS)
    assert len(INSTRUMENTS) >= 50
    assert Instrument.objects.filter(symbol="AAPL").exists()


def test_symbols_are_unique_in_the_source_list() -> None:
    """A duplicate here would be silently absorbed by update_or_create and lose an instrument."""
    symbols = [row[0] for row in INSTRUMENTS]
    assert len(symbols) == len(set(symbols))


def test_running_twice_creates_nothing_new() -> None:
    """Idempotent by symbol — the second pass updates in place."""
    seed()
    output = seed()

    assert Instrument.objects.count() == len(INSTRUMENTS)
    assert f"0 created, {len(INSTRUMENTS)} updated" in output


def test_reseeding_restores_edited_parameters() -> None:
    """update_or_create, not get_or_create: the source list stays the source of truth."""
    seed()
    Instrument.objects.filter(symbol="AAPL").update(volatility=Decimal("9.9999"), is_active=False)

    seed()

    apple = Instrument.objects.get(symbol="AAPL")
    assert apple.volatility != Decimal("9.9999")
    assert apple.is_active


def test_backfill_writes_a_dated_price_series() -> None:
    """Charts need history before Beat has ever run, and it has to end at 'now', not start there."""
    seed(ticks=5, seed=42)

    apple = Instrument.objects.get(symbol="AAPL")
    ticks = list(PriceTick.objects.filter(instrument=apple).order_by("created_at"))

    assert len(ticks) == 5
    # auto_now_add would have stamped all five identically; the bulk_update pass is what spreads
    # them, and without it a chart draws five points on top of each other.
    assert len({tick.created_at for tick in ticks}) == 5
    assert ticks[0].created_at < ticks[-1].created_at
    assert apple.last_price == ticks[-1].price


def test_backfill_skips_instruments_that_already_have_history() -> None:
    """So a re-seed does not staple a second series onto the first."""
    seed(ticks=3, seed=1)
    seed(ticks=3, seed=1)

    apple = Instrument.objects.get(symbol="AAPL")
    assert PriceTick.objects.filter(instrument=apple).count() == 3


def test_seeding_without_ticks_writes_no_history() -> None:
    seed()
    assert PriceTick.objects.count() == 0
