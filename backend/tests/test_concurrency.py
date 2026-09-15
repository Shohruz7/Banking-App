"""The concurrency claims that make ``transfer`` trustworthy (ADR-0010).

Three falsifiable properties, one test each: racing transfers cannot overdraw an account, opposing
transfers between the same pair cannot deadlock, and one idempotency key produces one posting no
matter how many callers race for it.

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

#: How many threads race for each contended resource.
#:
#: Two proves the property exists; it does not exercise it. At two, a lock queue is one waiter deep
#: and a serialization failure has one chance to happen — a writer that held the guarantee by luck
#: rather than by design would pass. Sixteen puts fifteen transactions on the wait queue behind the
#: winner, which is where an ordering mistake or a missed ``FOR UPDATE`` actually surfaces.
#:
#: The ceiling is Postgres connections: each thread opens its own (``django_db(transaction=True)``
#: requires it) and the default ``max_connections`` is 100, shared with the test runner's own
#: connection and any leaked from an earlier failure. Sixteen leaves the margin comfortable.
#:
#: Must equal ``ThreadPoolExecutor(max_workers=...)`` everywhere it is used: a pool smaller than the
#: barrier's party count can never release it, which is a hang, not a failure.
CONCURRENCY = 16


@pytest.mark.django_db(transaction=True)
def test_concurrent_transfers_cannot_overdraw() -> None:
    """No lost updates: 100 in the account, sixteen simultaneous 80s, exactly one survives."""
    source = AccountFactory.create()
    destination = AccountFactory.create()
    fund_account(source, Decimal("100.00"))

    barrier = threading.Barrier(CONCURRENCY, timeout=BARRIER_TIMEOUT)

    def attempt() -> str:
        try:
            barrier.wait(timeout=_TIMEOUT)
            transfer(source=source, destination=destination, amount=Decimal("80.00"))
            return "posted"
        except InsufficientFundsError:
            return "rejected"
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        futures = [pool.submit(attempt) for _ in range(CONCURRENCY)]
        outcomes = [future.result(timeout=_TIMEOUT) for future in futures]

    # Counted, not compared positionally: the interesting number is that exactly one of sixteen
    # got through. Without the lock every thread reads 100, every thread decides 80 is affordable,
    # and the account ends at 100 − 16×80 with the zero-sum trigger none the wiser, because each
    # entry balances perfectly on its own.
    assert outcomes.count("posted") == 1
    assert outcomes.count("rejected") == CONCURRENCY - 1
    assert get_balance(source) == Decimal("20.0000")
    assert get_balance(destination) == Decimal("80.0000")


@pytest.mark.django_db(transaction=True)
def test_opposite_transfers_do_not_deadlock() -> None:
    """Eight A→B against eight B→A at once. Ad-hoc lock order deadlocks; sorted by UUID it queues.

    Eight each way rather than one each way: a lock cycle needs two transactions to interleave
    between their two ``SELECT ... FOR UPDATE`` statements, and a single opposing pair gets one
    attempt at that window. Sixteen contending transactions in both directions generate the
    interleaving repeatedly, so an ordering bug shows up as a Postgres deadlock error rather than
    as a test that happened to serialize.
    """
    first = AccountFactory.create()
    second = AccountFactory.create()
    fund_account(first, Decimal("100.00"))
    fund_account(second, Decimal("100.00"))

    barrier = threading.Barrier(CONCURRENCY, timeout=BARRIER_TIMEOUT)

    def move(source: Account, destination: Account) -> None:
        try:
            barrier.wait(timeout=_TIMEOUT)
            transfer(source=source, destination=destination, amount=Decimal("10.00"))
        finally:
            connection.close()

    # Both balances stay solvent whatever order these land in: eight legs of 10 against an opening
    # 100 is 80 at worst, so nothing here can be rejected for funds and every leg must post.
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        futures = [
            pool.submit(move, first, second) if leg % 2 == 0 else pool.submit(move, second, first)
            for leg in range(CONCURRENCY)
        ]
        for future in futures:
            # A deadlock surfaces here — either as a Postgres error or as this timeout.
            future.result(timeout=_TIMEOUT)

    # Every transfer completed and they cancelled out exactly.
    assert get_balance(first) == Decimal("100.0000")
    assert get_balance(second) == Decimal("100.0000")
    assert JournalEntry.objects.filter(description="Transfer").count() == CONCURRENCY


@pytest.mark.django_db(transaction=True)
def test_idempotency_race_posts_one_entry() -> None:
    """Sixteen callers, one key, released together: one posting, and all get the same entry back."""
    source = AccountFactory.create()
    destination = AccountFactory.create()
    fund_account(source, Decimal("100.00"))

    barrier = threading.Barrier(CONCURRENCY, timeout=BARRIER_TIMEOUT)

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

    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        futures = [pool.submit(attempt) for _ in range(CONCURRENCY)]
        results = [future.result(timeout=_TIMEOUT) for future in futures]

    created_flags = [created for _, created in results]

    assert JournalEntry.objects.filter(idempotency_key="race-key").count() == 1
    assert len({entry_id for entry_id, _ in results}) == 1, "callers saw different entries"
    # Exactly one caller created it; the other fifteen replayed it. Both halves matter — fifteen
    # `False`s with no `True` would mean the entry came from somewhere else entirely.
    assert created_flags.count(True) == 1
    assert created_flags.count(False) == CONCURRENCY - 1
    # The money moved once, not sixteen times.
    assert get_balance(source) == Decimal("75.0000")
