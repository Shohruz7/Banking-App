"""The ledger seam: one nullable column, and the invariant it must not weaken (ADR-0016).

``JournalLine.quantity`` rides alongside ``amount`` so a holding's share count and its cost basis
are written in the same row. The zero-sum invariant is about ``amount`` alone and is untouched —
these tests are what says so out loud.
"""

from decimal import Decimal

import pytest
from django.db import Error as DatabaseError
from django.db import connection, transaction
from django.db.utils import IntegrityError

from accounts.models import Account, AccountType
from ledger.exceptions import InvalidEntryError
from ledger.models import JournalEntry, JournalLine
from ledger.services import LineSpec, get_balance, get_quantity, post_entry
from markets.models import Instrument
from tests.factories import AccountFactory, InstrumentFactory, UserFactory, position_account

pytestmark = pytest.mark.django_db


def test_zero_sum_trigger_still_fires_when_quantities_are_present(
    instrument: Instrument,
) -> None:
    """The new column did not weaken the old invariant.

    Written by hand rather than through ``post_entry`` — the point is that the *database* refuses
    it, not that the service does. ``SET CONSTRAINTS ALL IMMEDIATE`` is what makes the deferred
    trigger fire inside the test's wrapped transaction, following ``test_trigger.py``: without it
    the check waits for a COMMIT that never comes and the test passes vacuously.
    """
    user = UserFactory.create()
    cash = AccountFactory.create(owner=user)
    position = position_account(user, instrument)

    with pytest.raises(DatabaseError, match="unbalanced"), transaction.atomic():
        entry = JournalEntry.objects.create(description="unbalanced trade")
        JournalLine.objects.create(entry=entry, account=cash, amount=Decimal("-100.0000"))
        JournalLine.objects.create(
            entry=entry,
            account=position,
            amount=Decimal("-50.0000"),  # should be +100
            quantity=Decimal("1.00000000"),
        )
        with connection.cursor() as cursor:
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")


def test_a_line_on_a_position_account_must_carry_a_quantity(instrument: Instrument) -> None:
    """Otherwise a holding's cost basis could move without its share count moving with it."""
    user = UserFactory.create()
    cash = AccountFactory.create(owner=user)
    position = position_account(user, instrument)

    with pytest.raises(InvalidEntryError, match="share quantity"):
        post_entry(
            description="basis without shares",
            lines=[
                LineSpec(account=cash, amount=Decimal("-100")),
                LineSpec(account=position, amount=Decimal("100")),
            ],
        )


def test_a_line_on_a_cash_account_cannot_carry_a_quantity() -> None:
    """The mirror rule: dollars are not shares, and a cash line has no instrument to count."""
    user = UserFactory.create()
    first = AccountFactory.create(owner=user)
    second = AccountFactory.create(owner=user)

    with pytest.raises(InvalidEntryError, match="cannot carry a share quantity"):
        post_entry(
            description="shares of nothing",
            lines=[
                LineSpec(account=first, amount=Decimal("-100")),
                LineSpec(account=second, amount=Decimal("100"), quantity=Decimal("1")),
            ],
        )


def test_a_zero_quantity_is_refused(instrument: Instrument) -> None:
    """Absent is meaningful, zero is not — the same rule ``amount`` has always had."""
    user = UserFactory.create()
    cash = AccountFactory.create(owner=user)
    position = position_account(user, instrument)

    with pytest.raises(InvalidEntryError, match="non-zero share quantity"):
        post_entry(
            description="no shares at all",
            lines=[
                LineSpec(account=cash, amount=Decimal("-100")),
                LineSpec(account=position, amount=Decimal("100"), quantity=Decimal("0")),
            ],
        )


def test_quantities_are_quantized_to_eight_places(instrument: Instrument) -> None:
    """Rounding happens in ``quantize_shares`` and nowhere else (ADR-0009)."""
    user = UserFactory.create()
    cash = AccountFactory.create(owner=user)
    position = position_account(user, instrument)

    post_entry(
        description="over-precise",
        lines=[
            LineSpec(account=cash, amount=Decimal("-100")),
            LineSpec(account=position, amount=Decimal("100"), quantity=Decimal("1.234567891234")),
        ],
    )
    assert get_quantity(position) == Decimal("1.23456789")


def test_a_cash_account_reads_zero_shares() -> None:
    """SUM ignores NULLs, so a money account's share count is 0, never None."""
    account = AccountFactory.create()
    assert get_quantity(account) == Decimal("0E-8")


def test_one_position_account_per_owner_and_instrument(instrument: Instrument) -> None:
    """The database settles the race that lazy creation opens."""
    user = UserFactory.create()
    position_account(user, instrument)

    with pytest.raises(IntegrityError), transaction.atomic():
        Account.objects.create(
            owner=user,
            instrument=instrument,
            name="a second position",
            account_type=AccountType.ASSET,
        )


def test_a_position_account_must_be_an_asset(instrument: Instrument) -> None:
    """Its balance means cost basis; a liability or expense would make that read wrong."""
    user = UserFactory.create()

    with pytest.raises(IntegrityError), transaction.atomic():
        Account.objects.create(
            owner=user,
            instrument=instrument,
            name="upside down",
            account_type=AccountType.LIABILITY,
        )


def test_with_balance_and_with_quantity_compose_without_double_counting(
    instrument: Instrument,
) -> None:
    """Both aggregate over the same ``lines`` join, so chaining them is one JOIN with two SUMs.

    If they fanned out, every amount would be counted once per line and the balance would be wrong
    by a factor of the line count — which is exactly the trap that makes this worth asserting.
    """
    user = UserFactory.create()
    opening = AccountFactory.create(owner=user, account_type=AccountType.EQUITY)
    position = position_account(user, instrument)

    for _ in range(3):
        post_entry(
            description="lot",
            lines=[
                LineSpec(account=opening, amount=Decimal("-100")),
                LineSpec(account=position, amount=Decimal("100"), quantity=Decimal("1")),
            ],
        )

    row = Account.objects.filter(pk=position.pk).with_balance().with_quantity().get()
    assert row.balance == Decimal("300.0000")  # type: ignore[attr-defined]
    assert row.quantity == Decimal("3.00000000")  # type: ignore[attr-defined]
    # And the annotations agree with the one-account-at-a-time helpers.
    assert row.balance == get_balance(position)  # type: ignore[attr-defined]
    assert row.quantity == get_quantity(position)  # type: ignore[attr-defined]


def test_lock_accounts_collapses_duplicates(instrument: Instrument) -> None:
    """A caller should not have to think about passing the same account twice."""
    from ledger.services import lock_accounts

    user = UserFactory.create()
    cash = AccountFactory.create(owner=user)

    with transaction.atomic():
        lock_accounts(cash, cash, cash)  # must not raise


def test_instrument_symbols_are_normalized_to_upper_case() -> None:
    """Postgres unique indexes are case-sensitive, so ``aapl`` and ``AAPL`` would be two rows."""
    created = InstrumentFactory.create(symbol="lower")
    assert created.symbol == "LOWER"
