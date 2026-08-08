"""Shares cannot appear without an issuance leg, and Postgres is what says so (ADR-0025).

The gap this closes was stated in ADR-0016 and restated in two implementation logs: ``amount`` was
protected by a deferred trigger, ``quantity`` only by ``post_entry``'s pairing rule. These tests
write ``JournalLine`` rows **by hand**, bypassing the service, because a guarantee that holds only
for callers who used the front door is the thing that was already true.

What this earns, stated honestly: *conservation*, not authorization. Raw SQL can still mint shares
by creating its own equity account to issue them from — exactly as strong as the amount invariant,
which can likewise be fed from a fresh equity account, and no stronger.
"""

from decimal import Decimal

import pytest
from django.apps import apps as django_apps
from django.contrib.auth.models import User
from django.db import DatabaseError, connection, transaction

from accounts.models import Account, AccountType
from ledger.exceptions import UnbalancedSharesError
from ledger.models import JournalEntry, JournalLine
from ledger.services import LineSpec, conserve_shares, post_entry, share_contra_for
from ledger.share_conservation import (
    backfill_contra_lines,
    ensure_share_contras,
    find_unconserved_entries,
)
from markets.models import Instrument

from .factories import AccountFactory, InstrumentFactory, UserFactory, position_account


def _immediate() -> None:
    """Force deferred constraint triggers to fire now.

    pytest's ``django_db`` rolls its transaction back, so a COMMIT never happens and a deferred
    trigger never runs — a naive test passes vacuously. This is the same escape ``test_trigger.py``
    uses for the zero-sum trigger.
    """
    with connection.cursor() as cursor:
        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")


@pytest.mark.django_db
def test_a_buy_without_a_contra_leg_cannot_commit(instrument: Instrument) -> None:
    """The honest gap, closed.

    Written by hand: this is the two-line buy the ledger posted for six weeks, and the assertion is
    that the *database* now refuses it. Capable of failing because nothing in the service layer is
    involved — remove the trigger and these rows insert happily.
    """
    user = UserFactory.create()
    cash = AccountFactory.create(owner=user)
    position = position_account(user, instrument)
    entry = JournalEntry.objects.create(description="unconserved buy")

    with pytest.raises(DatabaseError, match="conserve shares"):
        JournalLine.objects.create(entry=entry, account=cash, amount=Decimal("-100.0000"))
        JournalLine.objects.create(
            entry=entry,
            account=position,
            amount=Decimal("100.0000"),
            quantity=Decimal("1.00000000"),
        )
        _immediate()


@pytest.mark.django_db(transaction=True)
def test_a_buy_without_a_contra_leg_fails_at_a_real_commit(instrument: Instrument) -> None:
    """The same claim without ``SET CONSTRAINTS``, so it does not rest on that mechanism.

    ``transaction=True`` gives a real COMMIT, which is the only way to show the trigger fires when
    nobody has asked it to. The pair of tests is the shape ``test_trigger.py`` established.
    """
    user = UserFactory.create()
    cash = AccountFactory.create(owner=user)
    position = position_account(user, instrument)

    with pytest.raises(DatabaseError, match="conserve shares"), transaction.atomic():
        entry = JournalEntry.objects.create(description="unconserved buy")
        JournalLine.objects.create(entry=entry, account=cash, amount=Decimal("-100.0000"))
        JournalLine.objects.create(
            entry=entry,
            account=position,
            amount=Decimal("100.0000"),
            quantity=Decimal("1.00000000"),
        )


@pytest.mark.django_db
def test_a_three_legged_buy_commits_cleanly(instrument: Instrument) -> None:
    """The positive control.

    Without this, a trigger that rejected *every* entry would pass every test above. It asserts the
    entry survives and that both invariants hold on it.
    """
    user = UserFactory.create()
    cash = AccountFactory.create(owner=user)
    position = position_account(user, instrument)

    entry = post_entry(
        description="conserved buy",
        lines=conserve_shares(
            [
                LineSpec(account=cash, amount=Decimal("-100.00")),
                LineSpec(account=position, amount=Decimal("100.00"), quantity=Decimal("1")),
            ]
        ),
    )
    _immediate()

    lines = list(entry.lines.all())
    assert len(lines) == 3
    assert sum(line.amount for line in lines) == Decimal("0.0000")
    assert sum(line.quantity or Decimal("0") for line in lines) == Decimal("0E-8")


@pytest.mark.django_db
def test_shares_of_one_instrument_cannot_be_minted_from_another() -> None:
    """**The test the whole trigger design turns on.**

    This entry is amount-balanced *and* its quantities net to zero across the entry — so it passes
    the zero-sum trigger, and it passes a naive ``SUM(quantity) = 0`` check too. The only thing that
    distinguishes it is ``GROUP BY instrument_id``.

    What it describes is a hundred shares of a penny stock becoming a hundred shares of Apple, at
    no cost, blessed by the database. Capable of failing against exactly one implementation error,
    which is what makes it worth more than the two above.
    """
    user = UserFactory.create()
    expensive = InstrumentFactory.create(symbol="AAPL")
    cheap = InstrumentFactory.create(symbol="PENY")
    into = position_account(user, expensive)
    out_of = position_account(user, cheap)
    entry = JournalEntry.objects.create(description="alchemy")

    with pytest.raises(DatabaseError, match="conserve shares"):
        JournalLine.objects.create(
            entry=entry,
            account=into,
            amount=Decimal("19500.0000"),
            quantity=Decimal("100.00000000"),
        )
        JournalLine.objects.create(
            entry=entry,
            account=out_of,
            amount=Decimal("-19500.0000"),
            quantity=Decimal("-100.00000000"),
        )
        _immediate()


@pytest.mark.django_db
def test_a_quantity_on_a_cash_line_cannot_commit(instrument: Instrument) -> None:
    """The fourth invariant, closed as a side effect.

    Cash lines group under a NULL instrument. Their quantities are always NULL, so the group sums to
    NULL and passes — unless one carries a quantity, which ``post_entry`` forbade and the database
    did not. Capable of failing against a trigger that filters ``instrument_id IS NOT NULL``, which
    is the obvious way to write it.
    """
    user = UserFactory.create()
    cash = AccountFactory.create(owner=user)
    other = AccountFactory.create(owner=user)
    entry = JournalEntry.objects.create(description="shares in a chequing account")

    with pytest.raises(DatabaseError, match="conserve shares"):
        JournalLine.objects.create(
            entry=entry, account=cash, amount=Decimal("-5.0000"), quantity=Decimal("1.00000000")
        )
        JournalLine.objects.create(entry=entry, account=other, amount=Decimal("5.0000"))
        _immediate()


@pytest.mark.django_db
def test_post_entry_refuses_an_unconserved_entry_before_the_database_does(
    instrument: Instrument,
) -> None:
    """The service catches it first, with a domain error naming the instrument.

    Capable of failing because the assertion names ``UnbalancedSharesError``. Remove the check from
    ``post_entry`` and the trigger still refuses the entry — but as a ``DatabaseError`` raised at
    COMMIT, from a frame nowhere near the caller that built the lines. A bare
    ``pytest.raises(Exception)`` here could not fail.
    """
    user = UserFactory.create()
    cash = AccountFactory.create(owner=user)
    position = position_account(user, instrument)

    with pytest.raises(UnbalancedSharesError):
        post_entry(
            description="forgot the contra",
            lines=[
                LineSpec(account=cash, amount=Decimal("-100.00")),
                LineSpec(account=position, amount=Decimal("100.00"), quantity=Decimal("1")),
            ],
        )

    assert JournalEntry.objects.filter(description="forgot the contra").count() == 0


@pytest.mark.django_db
def test_a_zero_amount_line_is_legal_only_when_it_moves_shares(instrument: Instrument) -> None:
    """Two-sided, so neither an over-tight nor an over-loose CHECK survives.

    The old ``amount != 0`` fails the second half; dropping the constraint entirely fails the first.
    """
    user = UserFactory.create()
    cash = AccountFactory.create(owner=user)
    entry = JournalEntry.objects.create(description="does nothing")

    with (
        pytest.raises(DatabaseError, match="journal_line_moves_money_or_shares"),
        transaction.atomic(),
    ):
        JournalLine.objects.create(entry=entry, account=cash, amount=Decimal("0.0000"))

    # And the contra leg — zero amount, real quantity — is accepted. Posted as part of a whole
    # entry rather than alone, because a lone issuance leg conserves nothing and the *other*
    # constraint would refuse it: the two rules are independent and both apply.
    position = position_account(user, instrument)
    posted = post_entry(
        description="issuance",
        lines=conserve_shares(
            [
                LineSpec(account=cash, amount=Decimal("-100.00")),
                LineSpec(account=position, amount=Decimal("100.00"), quantity=Decimal("1")),
            ]
        ),
    )
    contra = share_contra_for(instrument)
    contra_line = posted.lines.get(account=contra)
    assert contra_line.amount == Decimal("0.0000")
    assert contra_line.quantity == Decimal("-1.00000000")


@pytest.mark.django_db
def test_a_share_contra_is_not_a_holding(instrument: Instrument) -> None:
    """A contra must never surface as a position, or every portfolio nets to zero.

    Capable of failing because the zero-quantity skip in ``holdings_for`` does *not* filter a
    contra — its quantity is negative, not zero. Both the count and the identity are asserted: the
    count alone could be gamed by a slice.
    """
    from trading.portfolio import holdings_for

    user = UserFactory.create()
    cash = AccountFactory.create(owner=user)
    position = position_account(user, instrument)
    post_entry(
        description="buy",
        lines=conserve_shares(
            [
                LineSpec(account=cash, amount=Decimal("-100.00")),
                LineSpec(account=position, amount=Decimal("100.00"), quantity=Decimal("1")),
            ]
        ),
    )

    holdings = holdings_for(user)
    assert len(holdings) == 1
    assert holdings[0].quantity == Decimal("1.00000000")
    assert holdings[0].instrument.pk == instrument.pk

    # The contra exists, is owned by the system user, and is excluded by type rather than by owner.
    contra = Account.objects.share_contras().get(instrument=instrument)
    assert contra.account_type == AccountType.EQUITY
    assert contra.owner.username == "system"
    assert contra not in Account.objects.positions()


@pytest.mark.django_db
def test_a_contra_is_absent_from_the_owners_cash_accounts(instrument: Instrument) -> None:
    """It carries an instrument, so `.cash()` excludes it and `/accounts/` never shows it."""
    share_contra_for(instrument)
    assert not Account.objects.cash().filter(instrument=instrument).exists()


@pytest.mark.django_db
def test_the_backfill_makes_a_pre_adr_0025_entry_conform(instrument: Instrument) -> None:
    """The migration's own claim, tested with the trigger as the judge.

    A two-line buy is written by hand — the exact shape six weeks of fills produced. The deferred
    trigger has not fired yet, because ``django_db`` never commits, so this is genuinely the
    pre-migration state. The backfill then runs, and ``SET CONSTRAINTS ALL IMMEDIATE`` asks Postgres
    whether it worked.

    Capable of failing in both directions: a backfill that inserts nothing leaves the entry
    unconserved and the final check raises, and one that inserts the wrong sign or quantity leaves
    a non-zero net and raises too.
    """
    user = UserFactory.create()
    cash = AccountFactory.create(owner=user)
    position = position_account(user, instrument)
    entry = JournalEntry.objects.create(description="a Week 5 fill")
    JournalLine.objects.create(entry=entry, account=cash, amount=Decimal("-100.0000"))
    JournalLine.objects.create(
        entry=entry, account=position, amount=Decimal("100.0000"), quantity=Decimal("4.00000000")
    )

    assert find_unconserved_entries(connection) != []

    ensure_share_contras(django_apps)
    inserted = backfill_contra_lines(connection)

    assert inserted == 1
    assert find_unconserved_entries(connection) == []

    contra = Account.objects.share_contras().get(instrument=instrument)
    contra_line = entry.lines.get(account=contra)
    assert contra_line.quantity == Decimal("-4.00000000")
    assert contra_line.amount == Decimal("0.0000")
    # Backdated to its sibling, not to migration time — otherwise every `as_of` query over a closed
    # period would start giving a different answer than the statement already printed.
    assert contra_line.created_at == entry.lines.get(account=position).created_at

    # And the entry now survives the trigger it previously violated.
    _immediate()


@pytest.mark.django_db
def test_the_backfill_is_idempotent(instrument: Instrument) -> None:
    """Re-running inserts nothing — it aggregates per (entry, instrument) and skips what exists."""
    user = UserFactory.create()
    cash = AccountFactory.create(owner=user)
    position = position_account(user, instrument)
    entry = JournalEntry.objects.create(description="a Week 5 fill")
    JournalLine.objects.create(entry=entry, account=cash, amount=Decimal("-100.0000"))
    JournalLine.objects.create(
        entry=entry, account=position, amount=Decimal("100.0000"), quantity=Decimal("1.00000000")
    )

    ensure_share_contras(django_apps)
    assert backfill_contra_lines(connection) == 1
    assert backfill_contra_lines(connection) == 0
    assert entry.lines.count() == 3
    _immediate()


@pytest.mark.django_db
def test_a_clean_ledger_has_no_unconserved_entries(
    password_user: User, instrument: Instrument, funded_cash_account: Account
) -> None:
    """The scan the trigger's migration runs, over a ledger built the ordinary way."""
    from trading.models import OrderSide, OrderType
    from trading.services import place_order

    place_order(
        user=password_user,
        instrument=instrument,
        cash_account=funded_cash_account,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal("2"),
    )
    assert find_unconserved_entries(connection) == []
