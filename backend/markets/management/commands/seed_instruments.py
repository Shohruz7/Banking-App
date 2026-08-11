"""Seed the tradeable universe — 57 instruments across ten sectors.

A management command rather than a data migration, deliberately: migrations are permanent and
awkward to revise, and this list *will* be revised. ``update_or_create`` on the symbol makes reruns
idempotent, so it is safe in a fresh checkout, a demo reset, and `seed_demo` alike.

Starting prices and volatilities are plausible rather than real — a utility walks quieter than a
growth name, which is what makes a seeded portfolio look like a portfolio.
"""

from datetime import timedelta
from decimal import Decimal
from typing import Any

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from markets.models import Instrument, PriceTick
from markets.pricing import GBMPriceSource

#: symbol, name, sector, starting price, annual drift, annual volatility
INSTRUMENTS: list[tuple[str, str, str, str, str, str]] = [
    # Technology
    ("AAPL", "Apple Inc.", "Technology", "195.50", "0.12", "0.26"),
    ("MSFT", "Microsoft Corporation", "Technology", "412.30", "0.14", "0.24"),
    ("NVDA", "NVIDIA Corporation", "Technology", "875.20", "0.28", "0.52"),
    ("AVGO", "Broadcom Inc.", "Technology", "1320.75", "0.18", "0.34"),
    ("ORCL", "Oracle Corporation", "Technology", "128.40", "0.10", "0.28"),
    ("CRM", "Salesforce, Inc.", "Technology", "298.60", "0.11", "0.32"),
    ("ADBE", "Adobe Inc.", "Technology", "512.85", "0.09", "0.31"),
    ("AMD", "Advanced Micro Devices, Inc.", "Technology", "168.90", "0.16", "0.48"),
    ("INTC", "Intel Corporation", "Technology", "42.15", "0.02", "0.36"),
    ("CSCO", "Cisco Systems, Inc.", "Technology", "49.80", "0.06", "0.22"),
    ("IBM", "International Business Machines", "Technology", "186.25", "0.05", "0.21"),
    ("TXN", "Texas Instruments Incorporated", "Technology", "172.40", "0.08", "0.25"),
    # Communication services
    ("GOOGL", "Alphabet Inc. Class A", "Communication Services", "152.75", "0.13", "0.28"),
    ("META", "Meta Platforms, Inc.", "Communication Services", "487.90", "0.17", "0.38"),
    ("NFLX", "Netflix, Inc.", "Communication Services", "610.45", "0.15", "0.36"),
    ("DIS", "The Walt Disney Company", "Communication Services", "98.30", "0.04", "0.29"),
    ("VZ", "Verizon Communications Inc.", "Communication Services", "41.20", "0.02", "0.18"),
    ("T", "AT&T Inc.", "Communication Services", "18.65", "0.01", "0.20"),
    # Consumer discretionary
    ("AMZN", "Amazon.com, Inc.", "Consumer Discretionary", "178.40", "0.14", "0.31"),
    ("TSLA", "Tesla, Inc.", "Consumer Discretionary", "245.80", "0.10", "0.58"),
    ("HD", "The Home Depot, Inc.", "Consumer Discretionary", "355.60", "0.07", "0.23"),
    ("MCD", "McDonald's Corporation", "Consumer Discretionary", "289.15", "0.06", "0.17"),
    ("NKE", "NIKE, Inc.", "Consumer Discretionary", "94.70", "0.05", "0.28"),
    ("SBUX", "Starbucks Corporation", "Consumer Discretionary", "88.25", "0.05", "0.26"),
    ("BKNG", "Booking Holdings Inc.", "Consumer Discretionary", "3720.00", "0.11", "0.30"),
    # Financials
    ("BRK.B", "Berkshire Hathaway Inc. Class B", "Financials", "412.90", "0.09", "0.16"),
    ("JPM", "JPMorgan Chase & Co.", "Financials", "198.35", "0.08", "0.22"),
    ("V", "Visa Inc.", "Financials", "276.50", "0.10", "0.19"),
    ("MA", "Mastercard Incorporated", "Financials", "455.20", "0.11", "0.20"),
    ("BAC", "Bank of America Corporation", "Financials", "38.90", "0.06", "0.27"),
    ("WFC", "Wells Fargo & Company", "Financials", "58.45", "0.06", "0.26"),
    ("GS", "The Goldman Sachs Group, Inc.", "Financials", "445.70", "0.07", "0.25"),
    ("MS", "Morgan Stanley", "Financials", "96.80", "0.07", "0.26"),
    ("AXP", "American Express Company", "Financials", "232.15", "0.09", "0.24"),
    # Health care
    ("LLY", "Eli Lilly and Company", "Health Care", "785.40", "0.20", "0.30"),
    ("UNH", "UnitedHealth Group Incorporated", "Health Care", "512.60", "0.09", "0.22"),
    ("JNJ", "Johnson & Johnson", "Health Care", "156.80", "0.04", "0.15"),
    ("ABBV", "AbbVie Inc.", "Health Care", "172.35", "0.07", "0.20"),
    ("MRK", "Merck & Co., Inc.", "Health Care", "128.90", "0.06", "0.19"),
    ("PFE", "Pfizer Inc.", "Health Care", "28.45", "0.01", "0.24"),
    ("TMO", "Thermo Fisher Scientific Inc.", "Health Care", "578.30", "0.08", "0.23"),
    ("ABT", "Abbott Laboratories", "Health Care", "112.70", "0.06", "0.18"),
    # Consumer staples
    ("WMT", "Walmart Inc.", "Consumer Staples", "68.90", "0.08", "0.17"),
    ("COST", "Costco Wholesale Corporation", "Consumer Staples", "845.25", "0.12", "0.20"),
    ("PG", "The Procter & Gamble Company", "Consumer Staples", "167.40", "0.05", "0.14"),
    ("KO", "The Coca-Cola Company", "Consumer Staples", "62.85", "0.04", "0.15"),
    ("PEP", "PepsiCo, Inc.", "Consumer Staples", "172.60", "0.04", "0.16"),
    # Energy
    ("XOM", "Exxon Mobil Corporation", "Energy", "115.30", "0.05", "0.28"),
    ("CVX", "Chevron Corporation", "Energy", "156.75", "0.05", "0.27"),
    ("COP", "ConocoPhillips", "Energy", "112.40", "0.06", "0.31"),
    # Industrials
    ("CAT", "Caterpillar Inc.", "Industrials", "342.80", "0.08", "0.26"),
    ("BA", "The Boeing Company", "Industrials", "182.45", "0.03", "0.38"),
    ("GE", "GE Aerospace", "Industrials", "168.20", "0.10", "0.29"),
    ("UPS", "United Parcel Service, Inc.", "Industrials", "138.60", "0.04", "0.24"),
    # Utilities & real estate — the low-volatility end of the universe
    ("NEE", "NextEra Energy, Inc.", "Utilities", "72.35", "0.05", "0.21"),
    ("DUK", "Duke Energy Corporation", "Utilities", "102.80", "0.03", "0.16"),
    ("AMT", "American Tower Corporation", "Real Estate", "196.50", "0.04", "0.23"),
]


class Command(BaseCommand):
    help = "Seed or refresh the tradeable instrument universe (idempotent)."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--ticks",
            type=int,
            default=0,
            help=(
                "Backfill this many synthetic price ticks per instrument so charts have history "
                "before Beat has run. Skipped for instruments that already have ticks."
            ),
        )
        parser.add_argument(
            "--seed",
            type=int,
            default=None,
            help="Seed the backfill's random walk, for a reproducible demo dataset.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        created_count = 0

        with transaction.atomic():
            for symbol, name, sector, price, drift, volatility in INSTRUMENTS:
                _, created = Instrument.objects.update_or_create(
                    symbol=symbol,
                    defaults={
                        "name": name,
                        "sector": sector,
                        "initial_price": Decimal(price),
                        "drift": Decimal(drift),
                        "volatility": Decimal(volatility),
                        "is_active": True,
                    },
                )
                created_count += int(created)

        self.stdout.write(
            self.style.SUCCESS(
                f"{len(INSTRUMENTS)} instruments seeded "
                f"({created_count} created, {len(INSTRUMENTS) - created_count} updated)."
            )
        )

        ticks: int = options["ticks"]
        if ticks:
            self._backfill(ticks, options["seed"])

    def _backfill(self, ticks: int, seed: int | None) -> None:
        """Walk each instrument forward ``ticks`` times and bulk-insert the history.

        Timestamps are backdated so the series ends at "now" — a chart whose history starts in the
        future is worse than no chart. ``created_at`` is ``auto_now_add``, and its ``pre_save``
        overwrites whatever the instance carried, including during ``bulk_create``. So the intended
        stamps are held in a parallel list (not on the instances, which the INSERT mutates in
        place) and written back with one ``bulk_update``.
        """
        source = GBMPriceSource(seed=seed)
        now = timezone.now()
        interval = timedelta(seconds=source.interval_seconds)
        total = 0

        for instrument in Instrument.objects.filter(is_active=True):
            if instrument.ticks.exists():
                continue

            price = instrument.initial_price
            rows: list[PriceTick] = []
            stamps: list[Any] = []
            for step in range(ticks):
                instrument.last_price = price
                price = source.next_price(instrument)
                rows.append(PriceTick(instrument=instrument, price=price))
                stamps.append(now - interval * (ticks - step))

            PriceTick.objects.bulk_create(rows)
            for row, stamp in zip(rows, stamps, strict=True):
                row.created_at = stamp
            PriceTick.objects.bulk_update(rows, ["created_at"])

            instrument.last_price = price
            instrument.last_tick_at = now
            instrument.save(update_fields=["last_price", "last_tick_at"])
            total += len(rows)

        self.stdout.write(self.style.SUCCESS(f"Backfilled {total} price ticks."))
