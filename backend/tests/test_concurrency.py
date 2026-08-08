"""The concurrency claims that make ``transfer`` trustworthy (ADR-0010).

Three falsifiable properties, one test each: two racing transfers cannot overdraw an account,
opposing transfers between the same pair cannot deadlock, and one idempotency key produces one
posting no matter how many callers race for it.

Mechanics that are easy to get wrong and make these tests lie:

- ``django_db(transaction=True)`` is mandatory. Worker threads open their own connections and
  issue real COMMITs; under the default rollback-wrapped ``django_db`` they could not see the
  test's data or each other's writes, and every assertion would pass vacuously.
- Each thread must ``connection.close()`` in a ``finally``, or the run leaks Postgres connections
  until a later test starves.
- The ``Barrier`` is not decoration. Without it the calls usually serialize and the test proves
  nothing about contention.
- Futures are harvested with a timeout so a real deadlock fails the test instead of hanging CI.
"""

import threading
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest
from django.db import connection

from accounts.models import Account
from ledger.exceptions import InsufficientFundsError
from ledger.models import JournalEntry
from ledger.services import get_balance, transfer
from tests.factories import AccountFactory, fund_account

_TIMEOUT = 15

#: Seconds a thread will wait at the barrier before giving up. Without a timeout a barrier that
#: one thread never reaches blocks forever — and with no `timeout-minutes` in CI that used to mean
#: a hung job until GitHub's six-hour default killed it. Generous enough never to fire on a slow
#: machine, short enough that a real deadlock fails the test instead of the build.
BARRIER_TIMEOUT = 10


@pytest.mark.django_db(transaction=True)
def test_concurrent_transfers_cannot_overdraw() -> None:
    """No lost updates: 100 in the account, two simultaneous 80s, exactly one survives."""
    source = AccountFactory.create()
    destination = AccountFactory.create()
    fund_account(source, Decimal("100.00"))

    barrier = threading.Barrier(2, timeout=BARRIER_TIMEOUT)

    def attempt() -> str:
        try:
            barrier.wait(timeout=_TIMEOUT)
            transfer(source=source, destination=destination, amount=Decimal("80.00"))
            return "posted"
        except InsufficientFundsError:
            return "rejected"
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(attempt), pool.submit(attempt)]
        outcomes = sorted(future.result(timeout=_TIMEOUT) for future in futures)

    # Both legs asserted: without the lock both would read 100 and both would post.
    assert outcomes == ["posted", "rejected"]
    assert get_balance(source) == Decimal("20.0000")
    assert get_balance(destination) == Decimal("80.0000")


@pytest.mark.django_db(transaction=True)
def test_opposite_transfers_do_not_deadlock() -> None:
    """A→B and B→A at once. With ad-hoc lock order this deadlocks; sorted by UUID it queues."""
    first = AccountFactory.create()
    second = AccountFactory.create()
    fund_account(first, Decimal("100.00"))
    fund_account(second, Decimal("100.00"))

    barrier = threading.Barrier(2, timeout=BARRIER_TIMEOUT)

    def move(source: Account, destination: Account) -> None:
        try:
            barrier.wait(timeout=_TIMEOUT)
            transfer(source=source, destination=destination, amount=Decimal("10.00"))
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(move, first, second), pool.submit(move, second, first)]
        for future in futures:
            # A deadlock surfaces here — either as a Postgres error or as this timeout.
            future.result(timeout=_TIMEOUT)

    # Both transfers completed and cancelled out.
    assert get_balance(first) == Decimal("100.0000")
    assert get_balance(second) == Decimal("100.0000")
    assert JournalEntry.objects.filter(description="Transfer").count() == 2


@pytest.mark.django_db(transaction=True)
def test_idempotency_race_posts_one_entry() -> None:
    """Two callers, one key, released together: one posting, and both get the same entry back."""
    source = AccountFactory.create()
    destination = AccountFactory.create()
    fund_account(source, Decimal("100.00"))

    barrier = threading.Barrier(2, timeout=BARRIER_TIMEOUT)

    def attempt() -> tuple[str, bool]:
        try:
            barrier.wait(timeout=_TIMEOUT)
            entry, created = transfer(
                source=source,
                destination=destination,
                amount=Decimal("25.00"),
                idempotency_key="race-key",
            )
            return str(entry.pk), created
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(attempt), pool.submit(attempt)]
        results = [future.result(timeout=_TIMEOUT) for future in futures]

    assert JournalEntry.objects.filter(idempotency_key="race-key").count() == 1
    assert len({entry_id for entry_id, _ in results}) == 1, "callers saw different entries"
    assert sorted(created for _, created in results) == [False, True]
    # The money moved once, not twice.
    assert get_balance(source) == Decimal("75.0000")
