"""Test factories and helpers for the ledger.

Feature tests build accounts with these factories and move money through
``ledger.services.post_entry`` via :func:`post_balanced_entry` — never by constructing
``JournalLine`` rows directly. Tests that *deliberately* bypass the service (to exercise the DB
trigger or the CHECK constraint) create rows by hand and say so.
"""

from decimal import Decimal

import factory
import pyotp
from django.contrib.auth.hashers import make_password
from django.contrib.auth.models import User
from django.utils import timezone

from accounts.models import Account, AccountType
from identity.models import AuthSession, MfaDevice
from ledger.models import JournalEntry
from ledger.services import LineSpec, post_entry

#: The password every factory-built user has. Week 4 gave UserFactory a real (hashed) password:
#: before that no factory user could ever authenticate, which is why nothing exercised the login
#: path end to end. config.settings.test swaps in a fast hasher so this stays cheap.
TEST_PASSWORD = "sw0rdf1sh-test-pw"  # noqa: S105 — a test fixture, not a credential


class UserFactory(factory.django.DjangoModelFactory[User]):
    class Meta:
        model = User

    username = factory.Sequence(lambda n: f"user{n}")
    email = factory.LazyAttribute(lambda o: f"{o.username}@example.com")
    # Hashed at build time rather than via a post-generation ``set_password`` hook: the hook needs
    # a second save to persist, and factory_boy's ``skip_postgeneration_save`` would silently drop
    # it. One assignment, one INSERT, password actually usable.
    password = factory.LazyFunction(lambda: make_password(TEST_PASSWORD))


class AccountFactory(factory.django.DjangoModelFactory[Account]):
    class Meta:
        model = Account

    owner = factory.SubFactory(UserFactory)
    name = factory.Sequence(lambda n: f"Account {n}")
    account_type = AccountType.ASSET
    currency = "USD"


class MfaDeviceFactory(factory.django.DjangoModelFactory[MfaDevice]):
    """An enrolled authenticator. Unconfirmed by default — use the ``confirmed`` trait for MFA."""

    class Meta:
        model = MfaDevice

    user = factory.SubFactory(UserFactory)
    secret = factory.LazyFunction(pyotp.random_base32)

    class Params:
        confirmed = factory.Trait(confirmed_at=factory.LazyFunction(timezone.now))


class AuthSessionFactory(factory.django.DjangoModelFactory[AuthSession]):
    class Meta:
        model = AuthSession

    user = factory.SubFactory(UserFactory)
    ip = "127.0.0.1"


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


def fund_account(account: Account, amount: Decimal) -> JournalEntry:
    """Give an account a starting balance, posted against a fresh equity account.

    Money has to come from somewhere for the entry to balance — an opening-balances equity
    account is the accounting-correct source, and it keeps funding out of the transfer path
    under test.
    """
    opening = AccountFactory.create(
        owner=account.owner,
        name="Opening balances",
        account_type=AccountType.EQUITY,
    )
    return post_balanced_entry(opening, account, amount, description="opening balance")
