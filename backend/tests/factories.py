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
from ledger.services import LineSpec, conserve_shares, post_entry
from markets.models import Instrument, PriceTick
from trading.models import Order, OrderSide, OrderType

#: The password every factory-built user has. Week 4 gave UserFactory a real (hashed) password:
#: before that no factory user could ever authenticate, which is why nothing exercised the login
#: path end to end. config.settings.test swaps in a fast hasher so this stays cheap.
TEST_PASSWORD = "sw0rdf1sh-test-pw"  # a test fixture, not a credential


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


class InstrumentFactory(factory.django.DjangoModelFactory[Instrument]):
    """A tradeable symbol. Priced at $100 flat by default so arithmetic in tests stays legible."""

    class Meta:
        model = Instrument
        django_get_or_create = ("symbol",)

    symbol = factory.Sequence(lambda n: f"TST{n}")
    name = factory.LazyAttribute(lambda o: f"{o.symbol} Test Corp.")
    sector = "Technology"
    initial_price = Decimal("100.0000")
    drift = Decimal("0.0800")
    volatility = Decimal("0.2500")


class PriceTickFactory(factory.django.DjangoModelFactory[PriceTick]):
    class Meta:
        model = PriceTick

    instrument = factory.SubFactory(InstrumentFactory)
    price = Decimal("100.0000")


class OrderFactory(factory.django.DjangoModelFactory[Order]):
    """A resting limit buy. Traits flip it to a market order or the sell side."""

    class Meta:
        model = Order

    user = factory.SubFactory(UserFactory)
    instrument = factory.SubFactory(InstrumentFactory)
    cash_account = factory.SubFactory(AccountFactory)
    side = OrderSide.BUY
    order_type = OrderType.LIMIT
    quantity = Decimal("1.00000000")
    limit_price = Decimal("100.0000")

    class Params:
        market = factory.Trait(order_type=OrderType.MARKET, limit_price=None)
        sell = factory.Trait(side=OrderSide.SELL)


def position_account(owner: User, instrument: Instrument) -> Account:
    """The owner's position account for an instrument, created if absent.

    Mirrors ``trading.services.position_account_for`` rather than calling it, so a test that
    arranges a holding does not depend on the service under test to do the arranging.
    """
    account, _ = Account.objects.get_or_create(
        owner=owner,
        instrument=instrument,
        defaults={
            "name": f"{instrument.symbol} position",
            "account_type": AccountType.ASSET,
        },
    )
    return account


def give_shares(
    owner: User,
    instrument: Instrument,
    quantity: Decimal,
    cost: Decimal,
) -> JournalEntry:
    """Put a holding on the books at a known cost basis, without going through the order path.

    Posted against an equity account for the same reason :func:`fund_account` is: the entry has to
    balance, and money has to come from somewhere that is not the code under test.

    Wrapped in ``conserve_shares`` for the same reason a real fill is (ADR-0025). The shares also
    have to come from somewhere, and since Week 7 that is not a convention — an entry that skipped
    the contra leg would be refused at COMMIT. This one wrapper is why the ~25 call sites of this
    helper needed no edits of their own.
    """
    position = position_account(owner, instrument)
    opening = AccountFactory.create(
        owner=owner,
        name="Opening balances",
        account_type=AccountType.EQUITY,
    )
    return post_entry(
        description="opening position",
        lines=conserve_shares(
            [
                LineSpec(account=opening, amount=-cost),
                LineSpec(account=position, amount=cost, quantity=quantity),
            ]
        ),
    )


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
