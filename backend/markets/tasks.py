"""Scheduled market simulation (ADR-0017, ADR-0019).

``advance_prices`` is the only thing in the system that writes a ``PriceTick``, which is what lets
``Instrument.last_price`` be a cache rather than a second source of truth.
"""

import logging
from decimal import Decimal
from typing import Any

from celery import shared_task
from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from realtime import events

from .models import Instrument, PriceTick
from .pricing import PriceSource, get_price_source

logger = logging.getLogger(__name__)

#: Cache key for the advance mutex. Beat fires on a fixed interval regardless of whether the last
#: run finished, so without this a slow run and its successor would both walk the same prices.
_ADVANCE_LOCK_KEY = "markets:advance_prices:lock"


@shared_task(name="markets.advance_prices")
def advance_prices(source: PriceSource | None = None) -> dict[str, Any]:
    """Advance every active instrument by one tick.

    Held under a cache mutex whose timeout is a multiple of the tick interval: long enough that a
    slow-but-healthy run is never interrupted, short enough that a worker killed mid-run does not
    wedge the market until someone notices. ``cache.add`` is the atomic primitive here — it sets
    only if the key is absent, so two workers racing produce exactly one winner.

    Each instrument commits on its own so one bad symbol cannot roll back the whole market. Matching
    is dispatched afterwards through ``on_commit``, so the worker that picks it up cannot read a
    tick that has not landed yet.
    """
    lock_timeout = settings.MARKET_TICK_SECONDS * 5
    if not cache.add(_ADVANCE_LOCK_KEY, "1", lock_timeout):
        logger.warning("advance_prices skipped: a previous run is still holding the lock")
        return {"skipped": True, "ticked": 0}

    try:
        price_source = source if source is not None else get_price_source()
        now = timezone.now()
        ticked = 0
        moved: list[tuple[str, Decimal]] = []

        for instrument in Instrument.objects.filter(is_active=True):
            price = price_source.next_price(instrument)
            with transaction.atomic():
                PriceTick.objects.create(instrument=instrument, price=price)
                instrument.last_price = price
                instrument.last_tick_at = now
                instrument.save(update_fields=["last_price", "last_tick_at"])
            moved.append((instrument.symbol, price))
            ticked += 1
    finally:
        cache.delete(_ADVANCE_LOCK_KEY)

    # Published once for the whole sweep rather than inside each instrument's transaction: a tick
    # is only interesting to a client that subscribed to that symbol, and a group nobody joined
    # costs a round trip and nothing more (ADR-0023).
    events.publish_prices(moved)

    if ticked:
        # Imported here, not at module scope: trading imports markets (for Instrument), so a
        # top-level import back would close the cycle the two-app split exists to avoid.
        from trading.tasks import match_resting_orders

        transaction.on_commit(lambda: match_resting_orders.delay())

    return {"skipped": False, "ticked": ticked}
