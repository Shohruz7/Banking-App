"""Retention for machine-generated market data (ADR-0041).

The only purge this system ships. The two other candidates — ``AuthSession`` and ``AuditEvent`` —
hold personal data, which makes a retention rule for them an erasure policy, and this system has an
append-only audit log that Postgres refuses to let anyone delete from. That tension resolves by
pseudonymising ``actor_label``, not by deleting rows, and it deserves its own week.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.test import override_settings
from django.utils import timezone

from markets.models import Instrument, PriceTick
from markets.tasks import purge_price_ticks

pytestmark = pytest.mark.django_db


@pytest.fixture
def instrument() -> Instrument:
    return Instrument.objects.create(
        symbol="AAA",
        name="AAA Corp",
        sector="Technology",
        initial_price=Decimal("100.0000"),
        last_price=Decimal("100.0000"),
        drift=Decimal("0.05"),
        volatility=Decimal("0.20"),
    )


def _tick_aged(instrument: Instrument, days: int) -> PriceTick:
    """A tick stamped `days` in the past.

    `created_at` is `auto_now_add`, so the stamp has to be written back afterwards — the same
    mechanism `seed_instruments._backfill` and `seed_demo` both rely on.
    """
    tick = PriceTick.objects.create(instrument=instrument, price=Decimal("100.0000"))
    PriceTick.objects.filter(pk=tick.pk).update(created_at=timezone.now() - timedelta(days=days))
    return tick


@override_settings(PRICE_TICK_RETENTION_DAYS=400)
def test_it_deletes_only_what_is_past_the_horizon(instrument: Instrument) -> None:
    old = _tick_aged(instrument, days=401)
    kept = _tick_aged(instrument, days=399)

    result = purge_price_ticks()

    assert result["deleted"] == 1
    assert not PriceTick.objects.filter(pk=old.pk).exists()
    assert PriceTick.objects.filter(pk=kept.pk).exists()


@override_settings(PRICE_TICK_RETENTION_DAYS=400)
def test_it_keeps_a_year_of_history_for_statement_regeneration(instrument: Instrument) -> None:
    """The coupling that sets the number, and the reason it is not 90 days.

    `statements.services.period_end_prices` falls back to an instrument's `initial_price` when a
    period contains no tick. So purging inside a plausible regeneration window would make a
    regenerated statement disagree with the PDF that was issued — silently, and only for periods old
    enough that nobody would think to check.
    """
    a_year_ago = _tick_aged(instrument, days=365)

    purge_price_ticks()

    assert PriceTick.objects.filter(pk=a_year_ago.pk).exists()


@override_settings(PRICE_TICK_RETENTION_DAYS=0)
def test_zero_disables_it(instrument: Instrument) -> None:
    """A deploy that wants to keep everything should not have to unschedule a Beat entry."""
    ancient = _tick_aged(instrument, days=5_000)

    result = purge_price_ticks()

    assert result["deleted"] == 0
    assert PriceTick.objects.filter(pk=ancient.pk).exists()
