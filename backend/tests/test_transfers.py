"""Transfer semantics, single-threaded (ADR-0010).

The concurrency claims — no lost updates, no deadlocks, one entry per key under a race — live in
``test_concurrency.py``. These tests pin the rules a single caller sees.
"""

from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction

from ledger import services
from ledger.exceptions import InsufficientFundsError, InvalidEntryError
from ledger.models import JournalEntry, JournalLine
from ledger.services import LineSpec, get_balance, post_entry, transfer
from tests.factories import AccountFactory, fund_account


@pytest.mark.django_db
def test_idempotency_key_is_stored_and_unique() -> None:
    source = AccountFactory.create()
    destination = AccountFactory.create()
    lines = [LineSpec(source, Decimal("-5.00")), LineSpec(destination, Decimal("5.00"))]

    entry = post_entry(description="keyed", lines=lines, idempotency_key="key-1")
    assert entry.idempotency_key == "key-1"

    # The constraint is the backstop the transfer service relies on; prove the DB really has it.
    with pytest.raises(IntegrityError), transaction.atomic():
        post_entry(description="same key", lines=lines, idempotency_key="key-1")


@pytest.mark.django_db
def test_entries_without_keys_do_not_collide() -> None:
    """NULLs don't collide in a Postgres unique index — Week 2's keyless entries still post."""
    source = AccountFactory.create()
    destination = AccountFactory.create()
    lines = [LineSpec(source, Decimal("-5.00")), LineSpec(destination, Decimal("5.00"))]

    post_entry(description="no key", lines=lines)
    post_entry(description="also no key", lines=lines)

    assert JournalEntry.objects.filter(idempotency_key__isnull=True).count() == 2


@pytest.mark.django_db
def test_transfer_moves_money() -> None:
    source = AccountFactory.create()
    destination = AccountFactory.create()
    fund_account(source, Decimal("100.00"))

    entry, created = transfer(
        source=source,
        destination=destination,
        amount=Decimal("30.00"),
        description="rent",
    )

    assert created is True
    assert entry.description == "rent"
    assert entry.lines.count() == 2
    assert get_balance(source) == Decimal("70.0000")
    assert get_balance(destination) == Decimal("30.0000")


@pytest.mark.django_db
@pytest.mark.parametrize("amount", [Decimal("0.00"), Decimal("-10.00")])
def test_transfer_rejects_non_positive_amount(amount: Decimal) -> None:
    source = AccountFactory.create()
    destination = AccountFactory.create()
    fund_account(source, Decimal("100.00"))

    with pytest.raises(InvalidEntryError):
        transfer(source=source, destination=destination, amount=amount)


@pytest.mark.django_db
def test_transfer_rejects_same_account() -> None:
    account = AccountFactory.create()
    fund_account(account, Decimal("100.00"))

    with pytest.raises(InvalidEntryError):
        transfer(source=account, destination=account, amount=Decimal("10.00"))


@pytest.mark.django_db
def test_transfer_rejects_insufficient_funds() -> None:
    source = AccountFactory.create()
    destination = AccountFactory.create()
    fund_account(source, Decimal("50.00"))

    with pytest.raises(InsufficientFundsError):
        transfer(source=source, destination=destination, amount=Decimal("50.01"))

    # A rejected transfer leaves no trace: balances untouched, no orphan entry.
    assert get_balance(source) == Decimal("50.0000")
    assert get_balance(destination) == Decimal("0.0000")
    assert not JournalEntry.objects.filter(description="Transfer").exists()


@pytest.mark.django_db
def test_transfer_to_exact_zero_is_allowed() -> None:
    """The policy is "no overdraft", not "keep a buffer" — spending to zero is legal."""
    source = AccountFactory.create()
    destination = AccountFactory.create()
    fund_account(source, Decimal("50.00"))

    transfer(source=source, destination=destination, amount=Decimal("50.00"))

    assert get_balance(source) == Decimal("0.0000")
    assert get_balance(destination) == Decimal("50.0000")


@pytest.mark.django_db
def test_transfer_replay_returns_the_original_entry() -> None:
    source = AccountFactory.create()
    destination = AccountFactory.create()
    fund_account(source, Decimal("100.00"))

    first, created_first = transfer(
        source=source, destination=destination, amount=Decimal("25.00"), idempotency_key="abc-123"
    )
    second, created_second = transfer(
        source=source, destination=destination, amount=Decimal("25.00"), idempotency_key="abc-123"
    )

    assert created_first is True
    assert created_second is False
    assert first.pk == second.pk
    # The decisive assertion: the retry did not move money a second time.
    assert get_balance(source) == Decimal("75.0000")
    assert JournalEntry.objects.filter(idempotency_key="abc-123").count() == 1


@pytest.mark.django_db
def test_transfer_quantizes_amount() -> None:
    """Amounts land on the ADR-0009 quantum with banker's rounding before they are stored."""
    source = AccountFactory.create()
    destination = AccountFactory.create()
    fund_account(source, Decimal("100.00"))

    transfer(source=source, destination=destination, amount=Decimal("10.12345"))

    assert get_balance(destination) == Decimal("10.1234")


@pytest.mark.django_db
def test_transfer_lines_are_signed_opposites() -> None:
    source = AccountFactory.create()
    destination = AccountFactory.create()
    fund_account(source, Decimal("100.00"))

    entry, _ = transfer(source=source, destination=destination, amount=Decimal("40.00"))

    debit = JournalLine.objects.get(entry=entry, account=source)
    credit = JournalLine.objects.get(entry=entry, account=destination)
    assert debit.amount == Decimal("-40.0000")
    assert credit.amount == Decimal("40.0000")


@pytest.mark.django_db
def test_duplicate_key_race_recovers_the_original_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    """The unique constraint backstops two first attempts that both miss the replay checks.

    That window is too narrow to hit on purpose, so it is simulated: both lookups inside
    ``transfer`` are forced to miss, the INSERT then collides with the key already in the table,
    and the service must recover by refetching rather than surfacing ``IntegrityError``.
    """
    source = AccountFactory.create()
    destination = AccountFactory.create()
    fund_account(source, Decimal("100.00"))
    original, _ = transfer(
        source=source, destination=destination, amount=Decimal("10.00"), idempotency_key="dup"
    )

    real_lookup = services._entry_for_key
    misses = {"count": 0}

    def miss_twice(key: str) -> JournalEntry | None:
        misses["count"] += 1
        return None if misses["count"] <= 2 else real_lookup(key)

    monkeypatch.setattr(services, "_entry_for_key", miss_twice)

    entry, created = transfer(
        source=source, destination=destination, amount=Decimal("10.00"), idempotency_key="dup"
    )

    assert created is False
    assert entry.pk == original.pk
    # Still exactly one posting, and the money moved only on the first call.
    assert JournalEntry.objects.filter(idempotency_key="dup").count() == 1
    assert get_balance(source) == Decimal("90.0000")


@pytest.mark.django_db
def test_integrity_error_without_a_key_is_not_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The recovery path is for key collisions only — any other database error must surface.

    Without this, a constraint violation during a keyless transfer would be reported to the
    caller as a successful replay.
    """
    source = AccountFactory.create()
    destination = AccountFactory.create()
    fund_account(source, Decimal("100.00"))

    def boom(**kwargs: object) -> JournalEntry:
        raise IntegrityError("some other constraint")

    monkeypatch.setattr(services, "post_entry", boom)

    with pytest.raises(IntegrityError):
        transfer(source=source, destination=destination, amount=Decimal("10.00"))


@pytest.mark.django_db
def test_integrity_error_with_an_unmatched_key_is_not_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A key was supplied, but the collision wasn't on that key — so there's nothing to replay."""
    source = AccountFactory.create()
    destination = AccountFactory.create()
    fund_account(source, Decimal("100.00"))

    def boom(**kwargs: object) -> JournalEntry:
        raise IntegrityError("some other constraint")

    monkeypatch.setattr(services, "post_entry", boom)

    with pytest.raises(IntegrityError):
        transfer(
            source=source,
            destination=destination,
            amount=Decimal("10.00"),
            idempotency_key="never-posted",
        )
