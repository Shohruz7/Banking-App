"""The migration-time code paths, tested directly.

These functions live outside ``migrations/`` precisely so they can be reached from here:
``coverage.omit`` excludes migration modules, which would otherwise make the code that rewrites
historical rows unattended the only untested code in the project. Each ran exactly once, on one
machine, with nobody watching — that is an argument for tests, not against them.
"""

from decimal import Decimal

import pytest
from django.apps import apps as django_apps
from django.contrib.auth.models import User
from django.db import connection

from accounts.models import Account, AccountType
from ledger.fingerprints import (
    backfill_entry_fingerprints,
    backfill_order_fingerprints,
    order_fingerprint,
    transfer_fingerprint,
)
from ledger.models import JournalEntry
from ledger.services import transfer
from markets.models import Instrument
from trading.models import Order, OrderSide, OrderType
from trading.services import place_order

from .factories import AccountFactory, UserFactory, fund_account

pytestmark = pytest.mark.django_db


def _unconstrain(table: str, constraint: str) -> None:
    """Drop a CHECK so a row can be returned to its pre-migration shape.

    ``SET CONSTRAINTS ALL IMMEDIATE`` first, and it is not optional: the entries these tests just
    posted leave deferred trigger events pending, and Postgres refuses to ALTER a table that has
    any. The error — "cannot ALTER TABLE because it has pending trigger events" — is otherwise
    completely mystifying.
    """
    with connection.cursor() as cursor:
        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
        cursor.execute(f"ALTER TABLE {table} DROP CONSTRAINT {constraint}")


def test_a_legacy_transfer_gets_the_digest_it_would_have_been_written_with() -> None:
    """A two-line transfer's request is exactly recoverable from its own posting.

    The migration's claim, tested: source is the negative line, destination the positive one, amount
    the magnitude. Capable of failing because it compares against the digest the *service* computes
    for the same request — so a backfill that recovered the payload backwards, or dropped the
    accounts, produces a different string and the replay would 409 forever after.
    """
    source = AccountFactory.create()
    destination = AccountFactory.create()
    fund_account(source, Decimal("100.00"))
    entry, _ = transfer(
        source=source, destination=destination, amount=Decimal("25.00"), idempotency_key="legacy"
    )
    expected = entry.payload_fingerprint

    # Return the row to its pre-ADR-0024 shape. The CHECK forbids this via the ORM's normal path, so
    # it is done with raw SQL — which is also how the row got that way in the first place.
    _unconstrain("ledger_journalentry", "keyed_entry_has_a_fingerprint")
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE ledger_journalentry SET payload_fingerprint = NULL WHERE id = %s",
            [str(entry.pk)],
        )

    assert backfill_entry_fingerprints(django_apps) == 1

    entry.refresh_from_db()
    assert entry.payload_fingerprint == expected
    assert entry.payload_fingerprint == transfer_fingerprint(
        source_id=source.pk, destination_id=destination.pk, amount=Decimal("25.00")
    )


def test_a_legacy_fill_recovers_its_digest_from_the_order(
    password_user: User, instrument: Instrument, funded_cash_account: Account
) -> None:
    """A fill keyed ``order:{pk}`` has no client request — its digest comes from the order.

    Capable of failing against the tempting alternative of hashing the posted lines: a sell's cost
    basis depends on the holding, so the lines are not a stable description of the request.
    """
    order = place_order(
        user=password_user,
        instrument=instrument,
        cash_account=funded_cash_account,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal("2"),
    )
    entry = order.entry
    assert entry is not None
    expected = entry.payload_fingerprint

    _unconstrain("ledger_journalentry", "keyed_entry_has_a_fingerprint")
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE ledger_journalentry SET payload_fingerprint = NULL WHERE id = %s",
            [str(entry.pk)],
        )

    assert backfill_entry_fingerprints(django_apps) == 1
    entry.refresh_from_db()
    assert entry.payload_fingerprint == expected


def test_a_keyless_entry_is_left_alone() -> None:
    """Most entries carry no key, so most entries want no digest."""
    source = AccountFactory.create()
    fund_account(source, Decimal("50.00"))

    assert backfill_entry_fingerprints(django_apps) == 0
    assert JournalEntry.objects.filter(payload_fingerprint__isnull=False).count() == 0


def test_a_legacy_order_gets_its_digest(instrument: Instrument) -> None:
    user = UserFactory.create()
    cash = AccountFactory.create(owner=user, account_type=AccountType.ASSET)
    fund_account(cash, Decimal("10000.00"))
    order = place_order(
        user=user,
        instrument=instrument,
        cash_account=cash,
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Decimal("3"),
        limit_price=Decimal("1.00"),
        idempotency_key="legacy-order",
    )

    _unconstrain("trading_order", "keyed_order_has_a_fingerprint")
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE trading_order SET payload_fingerprint = NULL WHERE id = %s", [str(order.pk)]
        )

    assert backfill_order_fingerprints(django_apps) == 1

    order.refresh_from_db()
    assert order.payload_fingerprint == order_fingerprint(
        user_id=user.pk,
        symbol=instrument.symbol,
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Decimal("3"),
        limit_price=Decimal("1.00"),
        cash_account_id=cash.pk,
    )
    assert Order.objects.filter(payload_fingerprint__isnull=True).count() == 0


def test_the_account_number_backfill_numbers_only_customer_accounts(
    instrument: Instrument,
) -> None:
    """Same scoping as ``Account.save``, so pre- and post-migration accounts are indistinguishable.

    Capable of failing against a backfill scoped by ``account_type`` alone: a position account is
    an ASSET too, and numbering it would make a holding of AAPL look like a bank account.
    """
    from accounts.backfills import assign_missing_numbers

    user = UserFactory.create()
    customer = AccountFactory.create(owner=user, account_type=AccountType.ASSET)
    equity = AccountFactory.create(owner=user, account_type=AccountType.EQUITY)
    position = AccountFactory.create(
        owner=user, account_type=AccountType.ASSET, instrument=instrument
    )

    Account.objects.update(number_ciphertext="", number_last4="")

    assert assign_missing_numbers(django_apps) == 1

    customer.refresh_from_db()
    equity.refresh_from_db()
    position.refresh_from_db()
    assert customer.number_ciphertext != ""
    assert equity.number_ciphertext == ""
    assert position.number_ciphertext == ""


def test_flushing_expired_tokens_runs_the_librarys_own_command() -> None:
    """Wrapping ``flushexpiredtokens`` rather than hand-rolling the DELETE.

    The definition of "expired" belongs to the library that mints the tokens; a local query would
    drift from it silently the first time SimpleJWT changed its retention rule.
    """
    from identity.tasks import flush_expired_tokens

    assert flush_expired_tokens() == {"flushed": True}
