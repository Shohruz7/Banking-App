"""The demo dataset: customers, accounts, six months of transfers, and a book of trades.

**Everything here goes through the public services** — ``open_starter_accounts``, ``transfer``,
``place_order``. This module never imports ``post_entry``, which is why the TID251 fence in
``pyproject.toml`` needs no new exemption for it (ADR-0037).

That is the whole value of the dataset rather than a stylistic preference. Because every dollar
moved through the same overdraft check, the same ascending-UUID lock protocol, the same idempotency
keys, the same share-conservation contras and the same audit writes the application uses,
``check_ledger_invariants`` passing against this data *means something*. A seed that posted entries
directly would be a second, untested ledger writer, and any invariant it broke would surface as an
unattributable deferred-trigger error at COMMIT — a day of debugging to discover the fixture lied.

Lives in ``accounts/`` rather than ``ledger/`` because its primary output is customers and their
accounts, and because ``ledger`` importing ``trading`` would invert the app dependency order.
Management commands are leaves of the import graph, so ``accounts → trading`` binds nothing.

**Timestamps.** Every model here stamps ``created_at`` with ``auto_now_add``, which ``pre_save``
overwrites and which ``bulk_update`` does not call — the same mechanism that
``seed_instruments._backfill`` already documents, and the pattern this follows rather than
reinventing.

The ordering rule that makes backdating honest: the services check balances *as of now*, not as of
a date. So the whole timeline is built first, sorted, and then **executed in chronological order**,
recording each row's intended stamp as it goes. Rewriting the stamps afterwards is therefore not a
lie — nothing else writes to the ledger during the run, so the balance each service actually checked
is the balance that existed at the historical instant being claimed.

``AuditEvent`` is deliberately **not** backdated. The log records when the system did something, and
the system really did write these rows today. Backdating an append-only log so that it looks like
history is precisely the thing an append-only log exists to make impossible.
"""

from __future__ import annotations

import random
from argparse import ArgumentParser
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.contrib.auth.hashers import make_password
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.core.signals import setting_changed
from django.db import connection
from django.utils import timezone
from faker import Faker

from accounts.models import Account
from audit.models import AuditEvent
from ledger.exceptions import InsufficientFundsError, InvalidEntryError
from ledger.models import JournalEntry, JournalLine
from ledger.onboarding import open_starter_accounts
from ledger.services import transfer
from markets.models import Instrument
from trading.exceptions import (
    InstrumentInactiveError,
    InsufficientSharesError,
    InvalidOrderError,
)
from trading.models import Order, OrderSide, OrderStatus, OrderType
from trading.portfolio import holdings_for
from trading.services import place_order

#: Everything this command creates is identifiable by email domain, which is how a second run
#: recognises that it has already been here. `.invalid` is reserved by RFC 2606 and can never be a
#: real domain, so this cannot collide with a genuine address.
SEED_EMAIL_DOMAIN = "demo.invalid"

#: The login the README and the demo recording use. Given holdings, a resting limit order, and
#: deliberately no MFA device, so the walkthrough can show enrolment.
DEMO_USERNAME = "demo"
DEMO_PASSWORD = "demo-password-1234"  # noqa: S105 — a demo credential, published in the README.

TRANSFER_DESCRIPTIONS = [
    "Rent",
    "Groceries",
    "Savings top-up",
    "Utilities",
    "Card payment",
    "Payday",
    "Coffee",
    "Transport",
    "Subscriptions",
    "Emergency fund",
]

_ZERO = Decimal("0.0000")


@dataclass
class Plan:
    """One intended event, before it has happened."""

    at: datetime
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)


class Command(BaseCommand):
    help = "Generate the demo dataset: customers, accounts, transfer history and a book of trades."

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument("--users", type=int, default=260, help="How many customers to create.")
        parser.add_argument("--days", type=int, default=180, help="How far back history reaches.")
        parser.add_argument(
            "--seed",
            type=int,
            default=None,
            help=(
                "Make the run reproducible. Two honest limits: account numbers come from "
                "`secrets.choice` (a security control — making it seedable would be strictly "
                "worse) and every primary key is a time-based uuid7. So the same seed gives the "
                "same customers, shape, amounts and schedule — not the same identifiers."
            ),
        )
        # Deliberately no `--reset`. See the refusal in `handle` — seeded customers cannot be
        # deleted once they have acted, and the reason is a guarantee this project is built on.
        parser.add_argument(
            "--broadcast",
            action="store_true",
            help=(
                "Publish realtime events while seeding. Off by default: ~24k `group_send` round "
                "trips to Redis, every one of them to a group nobody has joined."
            ),
        )

    def handle(self, *args: Any, **options: Any) -> None:
        # S311: a demo dataset wants reproducible pseudo-randomness, not entropy — the same
        # reasoning as `markets.pricing.GBMPriceSource`. Nothing drawn here is a secret; the one
        # value that is (an account number) comes from `secrets` inside `accounts.numbers`.
        rng = random.Random(options["seed"])  # noqa: S311
        fake = Faker()
        if options["seed"] is not None:
            fake.seed_instance(options["seed"])

        existing = User.objects.filter(email__endswith=f"@{SEED_EMAIL_DOMAIN}")
        if existing.exists():
            raise CommandError(
                f"{existing.count()} seeded customers already exist, and this command cannot "
                "remove them.\n\n"
                "That is not an oversight — it is three of this system's guarantees meeting. "
                "`AuditEvent` is append-only, enforced by a Postgres trigger on UPDATE *and* "
                "DELETE, so the rows recording what these customers did cannot be deleted or even "
                "have their actor nulled. And `AuditEvent.actor` is PROTECT, so the customers "
                "themselves cannot be deleted while those rows exist. A ledger you can quietly "
                "un-write is not a ledger.\n\n"
                "So re-seeding means starting from an empty database, which is one command:\n\n"
                "    docker compose -f deploy/compose.yml down -v && \\\n"
                "      docker compose -f deploy/compose.yml up -d\n\n"
                "or, without containers, `manage.py flush` — which uses TRUNCATE, the one thing "
                "the append-only trigger deliberately does not block (see migration "
                "audit/0002)."
            )

        instruments = list(Instrument.objects.filter(is_active=True))
        if not instruments:
            raise CommandError(
                "No instruments. Run `manage.py seed_instruments --ticks 180` first — trades need "
                "something to trade and a price history to trade at."
            )

        now = timezone.now()
        window_start = now - timedelta(days=options["days"])

        with self._muted_events(broadcast=options["broadcast"]):
            users = self._create_users(options["users"], fake, rng, window_start)
            self.stdout.write(f"Opened accounts for {len(users)} customers.")

            plans = self._plan(users, instruments, rng, window_start, now)
            # Chronological execution is what makes the backdating honest rather than cosmetic.
            plans.sort(key=lambda plan: plan.at)
            self.stdout.write(f"Executing {len(plans)} events in chronological order…")

            stamps = self._execute(plans, rng)
            self._backdate(stamps)

        self._report()

    # ---------------------------------------------------------------------------------------------
    # Setup
    # ---------------------------------------------------------------------------------------------

    @contextmanager
    def _muted_events(self, *, broadcast: bool) -> Iterator[None]:
        """Point the channel layer at nothing for the duration of the run.

        ``channels.layers.get_channel_layer`` returns ``None`` on a missing config and
        ``realtime.events`` returns immediately on a ``None`` layer, so this is a supported no-op
        rather than a monkeypatch. Without it the seed spends minutes publishing balance updates
        and fills to groups nobody has joined, because nobody is connected during a seed.

        **Scoped, and restored, because the first version was not.** Assigning
        ``settings.CHANNEL_LAYERS`` and walking away is harmless in a command — the process exits a
        moment later — and quietly catastrophic under pytest, where the command runs in the same
        process as everything after it. It left eighteen WebSocket tests failing on a layer that had
        been ``None`` since some earlier test called the seed. A management command has no licence
        to permanently reconfigure the process that hosts it.

        ``setting_changed`` is the supported invalidation hook: ``ChannelLayerManager`` caches its
        backends and connects to that signal specifically to drop them, so mutating the setting
        without sending it would leave the previous layer live in the cache.
        """
        if broadcast:
            yield
            return

        previous = settings.CHANNEL_LAYERS
        settings.CHANNEL_LAYERS = {}
        setting_changed.send(sender=self.__class__, setting="CHANNEL_LAYERS", value={}, enter=True)
        try:
            yield
        finally:
            settings.CHANNEL_LAYERS = previous
            setting_changed.send(
                sender=self.__class__, setting="CHANNEL_LAYERS", value=previous, enter=False
            )

    def _create_users(
        self, count: int, fake: Faker, rng: random.Random, window_start: datetime
    ) -> list[dict[str, Any]]:
        """Create customers and open their accounts, funded per-customer.

        The password is hashed **once** and reused. Django 5.2 runs PBKDF2 at ~1.2M iterations, so
        hashing 260 times is around ninety seconds of pure waste for 260 identical results.
        """
        shared_hash = make_password(DEMO_PASSWORD)
        records: list[dict[str, Any]] = []
        seen: set[str] = set()

        for index in range(count):
            if index == 0:
                username = DEMO_USERNAME
            else:
                base = fake.user_name()[:20]
                username = base
                suffix = 0
                while username in seen or User.objects.filter(username=username).exists():
                    suffix += 1
                    username = f"{base[:16]}{suffix}"
            seen.add(username)

            user = User.objects.create(
                username=username,
                email=f"{username}@{SEED_EMAIL_DOMAIN}",
                first_name=fake.first_name(),
                last_name=fake.last_name(),
                password=shared_hash,
            )

            # Lognormal-ish: most customers hold a few thousand, a few hold a lot. A uniform
            # distribution makes every screenshot look the same.
            opening = Decimal(rng.choice([4_800, 7_200, 11_500, 18_000, 26_000, 41_000, 78_000]))
            checking, savings = open_starter_accounts(user, deposit=opening)

            records.append(
                {"user": user, "checking": checking, "savings": savings, "opening": opening}
            )

        # The opening deposits are the oldest thing in the ledger, so they are stamped at the start
        # of the window rather than left at "now" — otherwise every account appears to have been
        # funded today and six months of transfers precede their own money.
        opening_entries = JournalEntry.objects.filter(description="Opening deposit")
        self._rewrite(list(opening_entries), [window_start - timedelta(minutes=1)] * len(records))
        return records

    # ---------------------------------------------------------------------------------------------
    # Planning
    # ---------------------------------------------------------------------------------------------

    def _plan(
        self,
        users: list[dict[str, Any]],
        instruments: list[Instrument],
        rng: random.Random,
        start: datetime,
        end: datetime,
    ) -> list[Plan]:
        """Decide what happens, and when, before anything happens."""
        span = (end - start).total_seconds()
        plans: list[Plan] = []

        def moment() -> datetime:
            return start + timedelta(seconds=rng.uniform(0, span))

        for record in users:
            for _ in range(rng.randint(20, 45)):
                # 85% between the customer's own accounts, 15% to somebody else — the second case
                # is what exercises the two-owner branch in the transfer event publisher.
                peer = rng.random() < 0.15
                destination = (
                    rng.choice([r for r in users if r is not record])["checking"]
                    if peer and len(users) > 1
                    else rng.choice([record["savings"], record["checking"]])
                )
                source = (
                    record["checking"] if destination != record["checking"] else record["savings"]
                )
                plans.append(
                    Plan(
                        at=moment(),
                        kind="transfer",
                        payload={
                            "source": source,
                            "destination": destination,
                            # Weighted small, with a thin tail that outruns a drained Checking.
                            # Those tail entries are *meant* to fail: a demo audit log containing
                            # some rejections is more convincing than one where nobody ever
                            # mistyped an amount, and `transfer_rejected` rows are worth having on
                            # screen. Tuned against the observed rate rather than guessed — the
                            # first draft bounced 19% of events, which reads as a broken bank; this
                            # ladder, with the larger opening balances and smaller share parcels
                            # below, settles around 10%.
                            "amount": Decimal(
                                rng.choices(
                                    [12, 25, 40, 75, 120, 200, 340, 900],
                                    weights=[14, 18, 18, 16, 12, 10, 8, 4],
                                )[0]
                            ),
                            "description": rng.choice(TRANSFER_DESCRIPTIONS),
                            "actor": record["user"],
                        },
                    )
                )

        # Roughly a third of customers trade, so the portfolio screens are not uniformly busy.
        traders = rng.sample(users, k=max(1, len(users) // 3))
        for record in traders:
            for _ in range(rng.randint(8, 22)):
                roll = rng.random()
                if roll < 0.65:
                    side, order_type = OrderSide.BUY, OrderType.MARKET
                elif roll < 0.85:
                    side, order_type = OrderSide.SELL, OrderType.MARKET
                else:
                    side, order_type = OrderSide.BUY, OrderType.LIMIT

                plans.append(
                    Plan(
                        at=moment(),
                        kind="order",
                        payload={
                            "user": record["user"],
                            "cash_account": record["checking"],
                            "instrument": rng.choice(instruments),
                            "side": side,
                            "order_type": order_type,
                            # Small parcels. A market buy of twelve shares at a
                            # three-figure price is most of a Checking balance, and a
                            # seed whose orders mostly bounce produces a demo of a
                            # bank that does not work.
                            "quantity": Decimal(rng.choice(["1", "2", "3", "4", "6"])),
                        },
                    )
                )
        return plans

    # ---------------------------------------------------------------------------------------------
    # Execution
    # ---------------------------------------------------------------------------------------------

    def _execute(self, plans: list[Plan], rng: random.Random) -> list[tuple[Any, datetime]]:
        """Run the timeline, collecting `(row, intended stamp)` pairs.

        **No outer transaction, deliberately.** Each service call commits on its own, which is what
        lets the deferred constraint triggers check one entry at a time as they were designed to,
        and means one impossible event fails one event rather than discarding the whole run.
        """
        stamps: list[tuple[Any, datetime]] = []
        rejected = {"funds": 0, "shares": 0, "other": 0}

        for index, plan in enumerate(plans, start=1):
            if index % 500 == 0:
                self.stdout.write(f"  … {index}/{len(plans)}")

            row: JournalEntry | Order | None = (
                self._run_transfer(plan, rejected)
                if plan.kind == "transfer"
                else self._run_order(plan, rng, rejected)
            )

            if row is not None:
                stamps.append((row, plan.at))

        self.stdout.write(
            f"Rejections (kept, on purpose): {rejected['funds']} insufficient funds, "
            f"{rejected['shares']} insufficient shares, {rejected['other']} invalid."
        )
        return stamps

    def _run_transfer(self, plan: Plan, rejected: dict[str, int]) -> JournalEntry | None:
        try:
            entry, _created = transfer(
                source=plan.payload["source"],
                destination=plan.payload["destination"],
                amount=plan.payload["amount"],
                description=plan.payload["description"],
                actor=plan.payload["actor"],
            )
        except InsufficientFundsError:
            rejected["funds"] += 1
            return None
        except InvalidEntryError:
            rejected["other"] += 1
            return None
        return entry

    def _run_order(self, plan: Plan, rng: random.Random, rejected: dict[str, int]) -> Order | None:
        payload = plan.payload
        instrument: Instrument = payload["instrument"]
        quantity: Decimal = payload["quantity"]

        if payload["side"] == OrderSide.SELL:
            # Only sell something actually held, and never more than is held — the service would
            # refuse, and a seed that spends most of its time being refused produces no data.
            held = {h.instrument.symbol: h.quantity for h in holdings_for(payload["user"])}
            available = held.get(instrument.symbol, Decimal("0"))
            if available <= 0:
                return None
            quantity = min(quantity, available)

        limit_price = None
        if payload["order_type"] == OrderType.LIMIT:
            # Deliberately far below the market, so these stay `open` and the demo has resting
            # orders to show. Beat's matching sweep resolves any that the market later crosses.
            # `current_price` rather than `last_price`: the latter is null until the first tick,
            # and the model already knows to fall back to the initial price.
            limit_price = (instrument.current_price * Decimal("0.80")).quantize(Decimal("0.0001"))

        try:
            return place_order(
                user=payload["user"],
                instrument=instrument,
                cash_account=payload["cash_account"],
                side=payload["side"],
                order_type=payload["order_type"],
                quantity=quantity,
                limit_price=limit_price,
            )
        except InsufficientFundsError:
            rejected["funds"] += 1
            return None
        except InsufficientSharesError:
            rejected["shares"] += 1
            return None
        except (InvalidOrderError, InstrumentInactiveError):
            rejected["other"] += 1
            return None

    # ---------------------------------------------------------------------------------------------
    # Backdating
    # ---------------------------------------------------------------------------------------------

    def _backdate(self, stamps: list[tuple[Any, datetime]]) -> None:
        """Rewrite `created_at` on the rows the run produced.

        `auto_now_add` fires in `Model.pre_save`, which `bulk_update` does not call — the mechanism
        `seed_instruments._backfill` already relies on.

        `JournalLine.created_at` must match its entry rather than merely being close to it:
        `statements.services._period_lines` and `Account.with_balance(as_of=...)` both filter on the
        *line*, so a line outside its entry's period is money that vanishes from one statement and
        appears in no other. `Order.resolved_at` matters for the same reason —
        `build_brokerage_statement` filters fills by it, and leaving it at "now" empties every
        brokerage statement in the dataset.
        """
        entries = [(row, at) for row, at in stamps if isinstance(row, JournalEntry)]
        orders = [(row, at) for row, at in stamps if isinstance(row, Order)]

        self._rewrite([row for row, _ in entries], [at for _, at in entries])
        self._rewrite_lines([row for row, _ in entries])
        self._rewrite_orders(orders)

    @staticmethod
    def _rewrite(rows: list[Any], stamps: list[datetime]) -> None:
        if not rows:
            return
        for row, stamp in zip(rows, stamps, strict=True):
            row.created_at = stamp
        type(rows[0]).objects.bulk_update(rows, ["created_at"], batch_size=500)

    @staticmethod
    def _rewrite_lines(entries: list[JournalEntry]) -> None:
        """Pull every line onto its entry's timestamp, in one statement.

        A `bulk_update` over ~22k lines compiles to a CASE arm per row; this is one UPDATE with a
        join, and it is the difference between the seed finishing in minutes and in tens of minutes.

        Raw SQL here is not a ledger write and does not need the TID251 exemption: it moves a
        timestamp onto rows this command has already posted through `transfer` and `place_order`,
        and touches no amount, no account and no entry membership. The zero-sum and
        share-conservation triggers are on those columns and are unaffected — nothing this statement
        does could make an entry stop balancing.
        """
        if not entries:
            return
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE ledger_journalline AS line
                   SET created_at = entry.created_at
                  FROM ledger_journalentry AS entry
                 WHERE line.entry_id = entry.id
                   AND line.created_at <> entry.created_at
                """
            )

    def _rewrite_orders(self, orders: list[tuple[Order, datetime]]) -> None:
        if not orders:
            return
        rows = []
        for order, at in orders:
            order.created_at = at
            if order.resolved_at is not None:
                # A market order resolves inside the request that places it, so its resolution is
                # the same instant as its creation.
                order.resolved_at = at
            rows.append(order)
        Order.objects.bulk_update(rows, ["created_at", "resolved_at"], batch_size=500)

    # ---------------------------------------------------------------------------------------------
    # Reporting
    # ---------------------------------------------------------------------------------------------

    def _report(self) -> None:
        counts = {
            "customers": User.objects.filter(email__endswith=f"@{SEED_EMAIL_DOMAIN}").count(),
            "accounts": Account.objects.count(),
            "journal entries": JournalEntry.objects.count(),
            "journal lines": JournalLine.objects.count(),
            "orders": Order.objects.count(),
            "filled orders": Order.objects.filter(status=OrderStatus.FILLED).count(),
            "resting orders": Order.objects.filter(status=OrderStatus.OPEN).count(),
            "audit events": AuditEvent.objects.count(),
        }
        self.stdout.write(self.style.SUCCESS("\nSeeded:"))
        for label, value in counts.items():
            self.stdout.write(f"  {value:>7,}  {label}")
        self.stdout.write(
            f"\nSign in as `{DEMO_USERNAME}` / `{DEMO_PASSWORD}`.\n"
            "Run `manage.py check_ledger_invariants` to confirm the dataset is ledger-legal."
        )
