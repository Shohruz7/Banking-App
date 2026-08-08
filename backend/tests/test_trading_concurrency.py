"""The concurrency claims that make trade fills trustworthy (ADR-0010, ADR-0016).

Same mechanics as ``test_concurrency.py``, and the same traps: ``django_db(transaction=True)`` so
worker threads see each other's COMMITs, ``connection.close()`` in a ``finally`` so the run does not
leak connections, a ``Barrier`` so the calls genuinely contend, and timeouts so a real deadlock
fails the test instead of hanging CI.

One deliberate departure: the lock-ordering guarantee is asserted by inspecting the SQL rather than
by racing for a deadlock. A deadlock window is microseconds wide, so a test that hunts for one
passes or fails by luck — see that test's docstring for the reasoning.
"""

import threading
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from typing import cast

import pytest
from django.contrib.auth.models import User
from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext

from accounts.models import Account
from ledger.exceptions import InsufficientFundsError
from ledger.services import get_balance, get_quantity, lock_accounts, transfer
from tests.factories import (
    AccountFactory,
    InstrumentFactory,
    UserFactory,
    fund_account,
    give_shares,
)
from trading.exceptions import InsufficientSharesError
from trading.models import OrderSide, OrderType
from trading.services import place_order, position_account_for

_TIMEOUT = 20

#: Seconds a thread will wait at the barrier before giving up. Without a timeout a barrier that
#: one thread never reaches blocks forever — and with no `timeout-minutes` in CI that used to mean
#: a hung job until GitHub's six-hour default killed it. Generous enough never to fire on a slow
#: machine, short enough that a real deadlock fails the test instead of the build.
BARRIER_TIMEOUT = 10


def market_order(user: User, instrument: object, cash: object, side: str, quantity: str) -> None:
    place_order(
        user=user,
        instrument=instrument,  # type: ignore[arg-type]
        cash_account=cash,  # type: ignore[arg-type]
        side=side,
        order_type=OrderType.MARKET,
        quantity=Decimal(quantity),
    )


@pytest.mark.django_db(transaction=True)
def test_concurrent_buys_cannot_overdraw_cash() -> None:
    """$1,000 of cash, two simultaneous $800 buys. Exactly one can succeed.

    Without the lock both threads read $1,000, both decide they can afford it, and the account ends
    up at −$600 — money created out of nothing, which the zero-sum trigger cannot catch because
    each entry balances perfectly on its own.
    """
    user = UserFactory.create()
    instrument = InstrumentFactory.create(initial_price=Decimal("100.0000"))
    cash = AccountFactory.create(owner=user)
    fund_account(cash, Decimal("1000.00"))

    barrier = threading.Barrier(2, timeout=BARRIER_TIMEOUT)

    def attempt() -> str:
        try:
            barrier.wait(timeout=_TIMEOUT)
            market_order(user, instrument, cash, OrderSide.BUY, "8")
            return "filled"
        except InsufficientFundsError:
            return "rejected"
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(attempt), pool.submit(attempt)]
        outcomes = sorted(future.result(timeout=_TIMEOUT) for future in futures)

    assert outcomes == ["filled", "rejected"]
    assert get_balance(cash) == Decimal("200.0000")
    assert get_quantity(position_account_for(user, instrument)) == Decimal("8.00000000")


@pytest.mark.django_db(transaction=True)
def test_concurrent_sells_cannot_oversell_shares() -> None:
    """The same race on the quantity side: 8 shares held, two simultaneous sells of 5."""
    user = UserFactory.create()
    instrument = InstrumentFactory.create(initial_price=Decimal("100.0000"))
    cash = AccountFactory.create(owner=user)
    fund_account(cash, Decimal("100.00"))
    give_shares(user, instrument, Decimal("8"), Decimal("800.00"))

    barrier = threading.Barrier(2, timeout=BARRIER_TIMEOUT)

    def attempt() -> str:
        try:
            barrier.wait(timeout=_TIMEOUT)
            market_order(user, instrument, cash, OrderSide.SELL, "5")
            return "filled"
        except InsufficientSharesError:
            return "rejected"
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(attempt), pool.submit(attempt)]
        outcomes = sorted(future.result(timeout=_TIMEOUT) for future in futures)

    assert outcomes == ["filled", "rejected"]
    # 3 shares left, and never a negative holding — there is no short selling here.
    assert get_quantity(position_account_for(user, instrument)) == Decimal("3.00000000")


@pytest.mark.django_db
def test_lock_accounts_acquires_in_ascending_order_whatever_the_caller_says() -> None:
    """The mechanism, asserted directly: locks go out sorted by UUID, not in argument order.

    This is deliberately a white-box test of the *ordering*, not a black-box hunt for a deadlock.
    A deadlock needs two transactions to interleave inside the microseconds between two ``SELECT
    ... FOR UPDATE`` statements; a test that races for that window either passes by luck or fails
    by luck, and a probabilistic test in the failing direction is worse than none. The ordering is
    the thing under our control, and "a global acquisition order makes a lock cycle impossible" is
    the standard result the ADR reasons from — not something a test needs to rediscover.

    Every writer that needs the no-overdraft or no-short-sell guarantee routes through here, which
    is why this one function is worth pinning so precisely.
    """
    user = UserFactory.create()
    accounts = [AccountFactory.create(owner=user) for _ in range(3)]
    ascending = sorted(account.pk for account in accounts)

    def pk_in(sql: str) -> str:
        """Recover which account a FOR UPDATE statement named (hyphens vary by driver)."""
        flattened = sql.replace("-", "")
        return next(str(pk) for pk in ascending if str(pk).replace("-", "") in flattened)

    def locked_pks(order: list[Account]) -> list[str]:
        with CaptureQueriesContext(connection) as captured, transaction.atomic():
            lock_accounts(*order)
        return [pk_in(q["sql"]) for q in captured.captured_queries if "FOR UPDATE" in q["sql"]]

    forwards = locked_pks(accounts)
    backwards = locked_pks(list(reversed(accounts)))

    assert forwards == [str(pk) for pk in ascending]
    assert backwards == forwards, "acquisition order followed the caller, not the UUIDs"


@pytest.mark.django_db(transaction=True)
def test_a_trade_and_a_transfer_on_one_account_settle_correctly() -> None:
    """Two different writers contending on the same cash account produce consistent balances.

    Note what this does *not* prove: a fill locks {cash, position} and a transfer locks {cash,
    other}, so they share exactly one row — and two transactions contending on a single row cannot
    deadlock, they queue. The ordering guarantee is asserted by
    :func:`test_lock_accounts_acquires_in_ascending_order_whatever_the_caller_says` above; this test
    proves the two paths interleave without losing money.
    """
    user = UserFactory.create()
    instrument = InstrumentFactory.create(initial_price=Decimal("100.0000"))
    cash = AccountFactory.create(owner=user)
    other = AccountFactory.create(owner=user)
    fund_account(cash, Decimal("5000.00"))
    fund_account(other, Decimal("5000.00"))

    barrier = threading.Barrier(2, timeout=BARRIER_TIMEOUT)

    def buy() -> None:
        try:
            barrier.wait(timeout=_TIMEOUT)
            market_order(user, instrument, cash, OrderSide.BUY, "10")
        finally:
            connection.close()

    def move() -> None:
        try:
            barrier.wait(timeout=_TIMEOUT)
            transfer(source=other, destination=cash, amount=Decimal("100.00"))
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(buy), pool.submit(move)]
        for future in futures:
            # A deadlock surfaces here — as a Postgres error, or as this timeout.
            future.result(timeout=_TIMEOUT)

    # Both completed: $5000 − $1000 of shares + $100 transferred in.
    assert get_balance(cash) == Decimal("4100.0000")
    assert get_balance(other) == Decimal("4900.0000")
    assert get_quantity(position_account_for(user, instrument)) == Decimal("10.00000000")


@pytest.mark.django_db(transaction=True)
def test_racing_fills_on_one_order_post_a_single_entry() -> None:
    """Two sweeps grabbing the same resting order produce one fill, not two.

    Defence in depth: ``select_for_update(skip_locked=True)`` means the loser skips the order
    entirely, and the entry's ``order:{id}`` idempotency key would stop a double posting even if it
    did not.
    """
    from trading.tasks import match_resting_orders

    user = UserFactory.create()
    instrument = InstrumentFactory.create(initial_price=Decimal("100.0000"))
    cash = AccountFactory.create(owner=user)
    fund_account(cash, Decimal("5000.00"))

    place_order(
        user=user,
        instrument=instrument,
        cash_account=cash,
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Decimal("10"),
        limit_price=Decimal("100"),
    )

    barrier = threading.Barrier(2, timeout=BARRIER_TIMEOUT)

    def sweep() -> dict[str, int]:
        try:
            barrier.wait(timeout=_TIMEOUT)
            return cast(dict[str, int], match_resting_orders())
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(sweep), pool.submit(sweep)]
        results = [future.result(timeout=_TIMEOUT) for future in futures]

    assert sum(result["filled"] for result in results) == 1
    assert get_quantity(position_account_for(user, instrument)) == Decimal("10.00000000")
    assert get_balance(cash) == Decimal("4000.0000")
