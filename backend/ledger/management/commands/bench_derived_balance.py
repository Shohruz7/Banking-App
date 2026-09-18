"""Measure what a derived balance costs as an account's history grows.

    python manage.py bench_derived_balance --depths 100 1000 10000 --explain

Balances are not stored anywhere. ``AccountQuerySet.with_balance`` annotates a single
``SUM(lines.amount)`` and every read path goes through it (ADR-0008), which is the design decision
this project is most often asked to defend. ``tests/test_query_counts.py`` already proves the
*query count* stays flat as the number of accounts grows, and that is a different claim: it rules
out an N+1, it says nothing about what one of those queries costs. The cost of a sum grows with the
number of rows summed, and nothing here has ever measured how fast.

**What this command produces is the seeded fixture and the plan, not the client-side latency.**
``deploy/loadtest/growth_curve.py`` measures the endpoints through nginx against what this creates,
for the same reason the load harness measures through nginx: the number that matters is the one a
client experiences.

Two things worth knowing before reading the output.

**The write path is on the same curve, and that is not an artefact of the benchmark.**
``transfer`` checks the overdraft by deriving the source balance (ADR-0010), so posting the
100,000th transfer first sums 99,999 lines. That cost is measured separately from seeding, in a
short timed burst at each finished depth, for two reasons. Seeding one account to depth D walks
through every shallower depth on the way, so its average rate describes no depth in particular.
And seeding drives writes far harder than the shipped throttles ever permit, which turns out to
have its own consequence: every posting publishes a realtime event (ADR-0023), ``async_to_sync``
builds a fresh event loop per call, and ``RedisChannelLayer`` keys its connection pool on the
running loop. So a sustained write burst opens a Redis connection per transfer and exhausts the
ephemeral port range. The ledger is unharmed, exactly as designed: the publish is best-effort and
the write has already committed. But a rate measured through that is measuring the failure, so the
burst below neutralises the channel layer and says so.

**The customers this creates cannot be deleted.** ``AuditEvent`` is append-only by trigger and
``AuditEvent.actor`` is ``PROTECT``, so anyone who has moved money is permanent. Run this on a
stack you are willing to throw away, and see ``deploy/README.md`` on re-seeding.
"""

from __future__ import annotations

import time
from argparse import ArgumentParser
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.core.signals import setting_changed
from django.db import connection, transaction

from accounts.models import Account
from ledger.models import JournalLine
from ledger.onboarding import CHECKING_NAME, SAVINGS_NAME, open_starter_accounts
from ledger.services import transfer

#: Reserved by RFC 2606, and distinct from ``seed_demo``'s ``demo.invalid`` so the benchmark's
#: customers can never be mistaken for the demo dataset or picked up by the load harness, which
#: selects on that domain.
BENCH_EMAIL_DOMAIN = "bench.invalid"

#: Each transfer moves this, alternating direction, so neither account drifts toward zero. Small
#: relative to the opening deposit below, which is what keeps the overdraft check passing rather
#: than becoming the thing being measured.
STEP = Decimal("1.0000")

#: Large enough that no account in the pair can be driven negative by the alternating walk.
OPENING_DEPOSIT = Decimal("100000.0000")


class Command(BaseCommand):
    help = "Seed accounts at several history depths and report what a derived balance costs."

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument(
            "--depths",
            type=int,
            nargs="+",
            default=[100, 1000, 10000],
            help="Journal lines to put on each benchmark account.",
        )
        parser.add_argument(
            "--explain",
            action="store_true",
            help="Also print EXPLAIN (ANALYZE, BUFFERS) for the balance aggregate at each depth.",
        )
        parser.add_argument(
            "--write-samples",
            type=int,
            default=200,
            help="Timed transfers per depth, to price the overdraft check at that depth.",
        )
        parser.add_argument(
            "--progress-every",
            type=int,
            default=2000,
            help="Transfers between progress lines. 0 to stay quiet.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        depths: list[int] = sorted(options["depths"])
        rows: list[dict[str, Any]] = []

        for depth in depths:
            rows.append(self._one_depth(depth, options["progress_every"]))

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("Seeded fixtures"))
        self.stdout.write(f"{'depth':>8}  {'lines':>8}  {'seed s':>9}  {'writes/s':>9}  username")
        for row in rows:
            self.stdout.write(
                f"{row['depth']:>8}  {row['lines']:>8}  {row['seconds']:>9.1f}  "
                f"{row['rate']:>9.1f}  {row['username']}"
            )

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("Write cost at depth"))
        self.stdout.write(f"{'depth':>8}  {'ms/transfer':>12}  {'writes/s':>9}")
        for row in rows:
            write_cost = self._time_writes(row["username"], samples=options["write_samples"])
            self.stdout.write(
                f"{row['depth']:>8}  {write_cost['ms']:>12.2f}  {write_cost['rate']:>9.1f}"
            )

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("Server-side balance aggregate"))
        self.stdout.write(f"{'depth':>8}  {'ms':>8}  {'shared buffers hit+read':>24}")
        for row in rows:
            timing = self._time_aggregate(row["account_id"], explain=options["explain"])
            self.stdout.write(f"{row['depth']:>8}  {timing['ms']:>8.2f}  {timing['buffers']:>24}")
            if options["explain"]:
                for line in timing["plan"]:
                    self.stdout.write(f"          {line}")

        self.stdout.write("")
        self.stdout.write(
            "Client-side latency is not measured here. Run deploy/loadtest/growth_curve.py "
            "against these customers to get the number through nginx."
        )

    # ------------------------------------------------------------------------------------------

    def _one_depth(self, depth: int, progress_every: int) -> dict[str, Any]:
        """Create (or top up) one customer whose Checking carries ``depth`` journal lines."""
        username = f"bench-{depth}"
        user = User.objects.filter(username=username).first()

        if user is None:
            with transaction.atomic():
                # No usable password. These customers exist to own journal lines; the measuring
                # script mints their tokens the way the load harness does, so nothing ever logs in
                # as them and a password would only be one more credential lying around.
                user = User.objects.create_user(
                    username=username,
                    email=f"{username}@{BENCH_EMAIL_DOMAIN}",
                )
                user.set_unusable_password()
                user.save(update_fields=["password"])
                open_starter_accounts(user, deposit=OPENING_DEPOSIT)

        checking = Account.objects.get(owner=user, name=CHECKING_NAME)
        savings = Account.objects.get(owner=user, name=SAVINGS_NAME)

        existing = JournalLine.objects.filter(account=checking).count()
        needed = max(0, depth - existing)

        self.stdout.write(
            self.style.MIGRATE_HEADING(f"depth {depth}: {existing} lines present, {needed} to post")
        )

        started = time.monotonic()
        for index in range(needed):
            # Alternating, so neither side walks toward zero over a long run.
            source, destination = (checking, savings) if index % 2 == 0 else (savings, checking)
            transfer(
                source=source,
                destination=destination,
                amount=STEP,
                description="bench",
                actor=user,
            )
            if progress_every and index and index % progress_every == 0:
                rate = index / (time.monotonic() - started)
                self.stdout.write(f"  {index}/{needed}  {rate:.0f}/s")
        seconds = time.monotonic() - started

        lines = JournalLine.objects.filter(account=checking).count()
        return {
            "depth": depth,
            "lines": lines,
            "seconds": seconds,
            "rate": (needed / seconds) if seconds > 0 and needed else 0.0,
            "username": username,
            "account_id": checking.pk,
        }

    def _time_writes(self, username: str, *, samples: int) -> dict[str, float]:
        """Price one transfer at this account's current depth.

        The channel layer is dropped for the duration. Not to flatter the
        number: at this rate the Redis layer fails on ephemeral ports (see the module docstring),
        and a transfer that spends its time raising and logging a ConnectionError is not measuring
        the overdraft check. The publish is best-effort and out of the transaction either way, so
        removing it leaves the ledger write path itself intact.
        """
        user = User.objects.get(username=username)
        checking = Account.objects.get(owner=user, name=CHECKING_NAME)
        savings = Account.objects.get(owner=user, name=SAVINGS_NAME)

        with self._without_broadcasts():
            started = time.monotonic()
            for index in range(samples):
                source, destination = (checking, savings) if index % 2 == 0 else (savings, checking)
                transfer(
                    source=source,
                    destination=destination,
                    amount=STEP,
                    description="bench-write",
                    actor=user,
                )
            seconds = time.monotonic() - started

        return {
            "ms": (seconds / samples) * 1000 if samples else float("nan"),
            "rate": samples / seconds if seconds > 0 else float("nan"),
        }

    @contextmanager
    def _without_broadcasts(self) -> Iterator[None]:
        """Drop the channel layer for the duration, the way ``seed_demo`` does.

        The same pattern and the same reasoning, including the part that matters most: scoped and
        restored. Assigning ``settings.CHANNEL_LAYERS`` and walking away is harmless in a command
        whose process exits a moment later, and quietly catastrophic under pytest, where it would
        leave every later WebSocket test running against a layer that is ``None``.

        ``setting_changed`` is the supported invalidation hook: ``ChannelLayerManager`` caches its
        backends and connects to that signal specifically to drop them.
        """
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

    def _time_aggregate(self, account_id: Any, *, explain: bool) -> dict[str, Any]:
        """Time the balance aggregate in the database, with the planner's own numbers.

        Asked of Postgres rather than timed around the ORM so the figure excludes Python object
        construction and serialisation. Those are real costs a client pays, which is exactly why
        they are measured separately and through nginx; mixing them in here would blur the one
        question this function answers, which is how the *sum* scales.
        """
        sql = "SELECT COALESCE(SUM(l.amount), 0) FROM ledger_journalline l WHERE l.account_id = %s"
        with connection.cursor() as cursor:
            cursor.execute(f"EXPLAIN (ANALYZE, BUFFERS) {sql}", [account_id])
            plan = [row[0] for row in cursor.fetchall()]

        execution = next((line for line in plan if "Execution Time" in line), "")
        ms = float(execution.split(":")[1].strip().split(" ")[0]) if execution else float("nan")

        hit = read = 0
        for line in plan:
            if "shared hit=" in line or "shared read=" in line:
                for token in line.replace("shared ", "").split():
                    if token.startswith("hit="):
                        hit += int(token[4:])
                    elif token.startswith("read="):
                        read += int(token[5:])

        return {"ms": ms, "buffers": f"{hit + read}", "plan": plan if explain else []}
