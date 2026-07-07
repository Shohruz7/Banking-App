"""Test factories and helpers for the ledger.

Feature tests build accounts with these factories and move money through
``ledger.services.post_entry`` via :func:`post_balanced_entry` — never by constructing
``JournalLine`` rows directly. Tests that *deliberately* bypass the service (to exercise the DB
trigger or the CHECK constraint) create rows by hand and say so.
"""

from decimal import Decimal

import factory
from django.contrib.auth.models import User

from accounts.models import Account, AccountType
from ledger.models import JournalEntry
from ledger.services import LineSpec, post_entry


class UserFactory(factory.django.DjangoModelFactory[User]):
    class Meta:
        model = User

    username = factory.Sequence(lambda n: f"user{n}")
    email = factory.LazyAttribute(lambda o: f"{o.username}@example.com")


class AccountFactory(factory.django.DjangoModelFactory[Account]):
    class Meta:
        model = Account

    owner = factory.SubFactory(UserFactory)
    name = factory.Sequence(lambda n: f"Account {n}")
    account_type = AccountType.ASSET
    currency = "USD"


def post_balanced_entry(
    source: Account,
    destination: Account,
    amount: Decimal,
    *,
    description: str = "test entry",
) -> JournalEntry:
    """Post a two-line balanced entry through the service: debit source, credit destination."""
    return post_entry(
        description=description,
        lines=[
            LineSpec(account=source, amount=-amount),
            LineSpec(account=destination, amount=amount),
        ],
    )
