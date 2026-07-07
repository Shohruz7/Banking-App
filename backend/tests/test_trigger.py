"""The database itself refuses to commit an unbalanced entry (ADR-0008, migration 0002).

These tests deliberately bypass ``post_entry`` and write ``JournalLine`` rows directly — the
point is that the invariant holds even for code paths that never touch the service.

The gotcha: the deferred constraint trigger only fires at COMMIT. pytest-django's default
``django_db`` marker wraps each test in a transaction it rolls back, so the trigger would never
fire and a naive test would pass vacuously. Two ways to make the check actually happen, both
exercised below:

1. ``SET CONSTRAINTS ALL IMMEDIATE`` forces the deferred check to run *now*, inside the wrapped
   transaction — fast, keeps the rollback-based isolation.
2. ``django_db(transaction=True)`` runs a real transaction with a real COMMIT, proving the
   honest end-to-end claim that the commit itself fails.
"""

from decimal import Decimal

import pytest
from django.db import Error as DatabaseError
from django.db import connection, transaction

from ledger.models import JournalEntry, JournalLine
from tests.factories import AccountFactory


@pytest.mark.django_db
def test_unbalanced_lines_rejected_when_constraints_checked() -> None:
    entry = JournalEntry.objects.create(description="lone unbalanced line")
    account = AccountFactory.create()

    with pytest.raises(DatabaseError) as exc_info, transaction.atomic():
        JournalLine.objects.create(
            entry=entry, account=account, amount=Decimal("100.00"), currency="USD"
        )
        # Force the DEFERRABLE INITIALLY DEFERRED trigger to fire without waiting for COMMIT.
        with connection.cursor() as cursor:
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")

    assert "unbalanced" in str(exc_info.value).lower()


@pytest.mark.django_db(transaction=True)
def test_unbalanced_entry_cannot_commit() -> None:
    account = AccountFactory.create()

    # Leaving the atomic block triggers the COMMIT that the deferred check aborts.
    with pytest.raises(DatabaseError) as exc_info, transaction.atomic():
        entry = JournalEntry.objects.create(description="never commits")
        JournalLine.objects.create(
            entry=entry, account=account, amount=Decimal("50.00"), currency="USD"
        )

    assert "unbalanced" in str(exc_info.value).lower()
    # The failed commit rolled everything back: no partial entry survives.
    assert not JournalEntry.objects.filter(description="never commits").exists()


@pytest.mark.django_db(transaction=True)
def test_balanced_entry_commits_cleanly() -> None:
    source = AccountFactory.create()
    destination = AccountFactory.create()

    with transaction.atomic():
        entry = JournalEntry.objects.create(description="balanced by hand")
        JournalLine.objects.bulk_create(
            [
                JournalLine(entry=entry, account=source, amount=Decimal("-25.00"), currency="USD"),
                JournalLine(
                    entry=entry, account=destination, amount=Decimal("25.00"), currency="USD"
                ),
            ]
        )

    assert JournalEntry.objects.filter(description="balanced by hand").exists()
