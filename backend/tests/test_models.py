"""Model-level guarantees: the non-zero CHECK constraint and append-only PROTECT semantics."""

from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError

from ledger.models import JournalEntry, JournalLine
from tests.factories import AccountFactory, post_balanced_entry


@pytest.mark.django_db
def test_zero_amount_line_violates_check_constraint() -> None:
    # Bypass the service on purpose: the DB CHECK is the guard under test.
    entry = JournalEntry.objects.create(description="zero line")
    account = AccountFactory.create()
    with pytest.raises(IntegrityError), transaction.atomic():
        JournalLine.objects.create(
            entry=entry, account=account, amount=Decimal("0.0000"), currency="USD"
        )


@pytest.mark.django_db
def test_account_with_lines_cannot_be_deleted() -> None:
    source = AccountFactory.create()
    destination = AccountFactory.create()
    post_balanced_entry(source, destination, Decimal("10.00"))

    with pytest.raises(ProtectedError):
        source.delete()


@pytest.mark.django_db
def test_entry_with_lines_cannot_be_deleted() -> None:
    source = AccountFactory.create()
    destination = AccountFactory.create()
    entry = post_balanced_entry(source, destination, Decimal("10.00"))

    with pytest.raises(ProtectedError):
        entry.delete()
