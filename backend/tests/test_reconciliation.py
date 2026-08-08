"""The backstop for the one invariant no trigger can hold (ADR-0026).

``no asset account goes negative`` is enforced by every writer taking the row lock before reading
the balance (ADR-0010), which is a convention. It cannot be pushed into a deferred constraint
trigger — the reasons are in ``ledger.reconciliation``'s docstring, and the short version is that
such a trigger would only catch a violation depending on commit interleaving. So this scans
committed data instead, where the question is actually answerable.
"""

from decimal import Decimal

import pytest
from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection

from accounts.models import Account, AccountType
from audit.models import AuditAction, AuditEvent
from ledger.models import JournalEntry, JournalLine
from ledger.reconciliation import check_invariants
from markets.models import Instrument
from trading.models import OrderSide, OrderType
from trading.services import place_order

from .factories import AccountFactory, UserFactory, fund_account, give_shares

pytestmark = pytest.mark.django_db


def test_a_clean_ledger_reports_no_violations(
    password_user: User, instrument: Instrument, funded_cash_account: Account
) -> None:
    """A funded account, a buy and a profitable sell — all legal, none reported.

    Capable of failing against a check scoped to *all* accounts rather than to assets, which is the
    obvious way to write it. ``fund_account`` guarantees an ``Opening balances`` equity account
    sitting at −10000, and a profitable sell guarantees a ``Realized P&L`` income account sitting
    negative too. Both are correct double-entry — they are where the money came from — and a
    wrongly-scoped check has no choice but to report them.
    """
    give_shares(password_user, instrument, Decimal("10"), Decimal("1000.00"))
    instrument.last_price = Decimal("160.0000")
    instrument.save(update_fields=["last_price"])
    place_order(
        user=password_user,
        instrument=instrument,
        cash_account=funded_cash_account,
        side=OrderSide.SELL,
        order_type=OrderType.MARKET,
        quantity=Decimal("3"),
    )

    # The false positives the scoping avoids really are present.
    assert Account.objects.filter(
        owner=password_user, account_type=AccountType.EQUITY, name="Opening balances"
    ).exists()
    assert Account.objects.filter(
        owner=password_user, account_type=AccountType.INCOME, name="Realized P&L"
    ).exists()

    assert check_invariants(connection) == []


def test_a_negative_asset_balance_is_reported() -> None:
    """The violation is constructed by hand, because no service will produce one.

    Capable of failing because the assertion names the specific account. A check that reported
    everything, or that returned a constant non-empty list, would satisfy a bare count assertion.
    """
    user = UserFactory.create()
    overdrawn = AccountFactory.create(owner=user, account_type=AccountType.ASSET)
    equity = AccountFactory.create(owner=user, account_type=AccountType.EQUITY)

    # A perfectly balanced entry that nonetheless drives an asset account below zero — which is
    # exactly why the zero-sum trigger cannot catch this class of problem.
    entry = JournalEntry.objects.create(description="overdraft by hand")
    JournalLine.objects.create(entry=entry, account=overdrawn, amount=Decimal("-50.0000"))
    JournalLine.objects.create(entry=entry, account=equity, amount=Decimal("50.0000"))

    violations = check_invariants(connection)

    assert [v.invariant for v in violations] == ["negative_asset_balance"]
    assert violations[0].subject == str(overdrawn.pk)
    # And the equity account, also negative-by-design elsewhere, is not among them.
    assert str(equity.pk) not in [v.subject for v in violations]


def test_a_negative_holding_is_reported(instrument: Instrument) -> None:
    """An accidental short — no service produces one, and nothing else would notice."""
    user = UserFactory.create()
    from .factories import position_account

    position = position_account(user, instrument)
    contra = Account.objects.create(
        owner=UserFactory.create(),
        instrument=instrument,
        account_type=AccountType.EQUITY,
        name=f"{instrument.symbol} shares outstanding",
    )
    entry = JournalEntry.objects.create(description="short by hand")
    JournalLine.objects.create(
        entry=entry,
        account=position,
        amount=Decimal("-10.0000"),
        quantity=Decimal("-1.00000000"),
    )
    JournalLine.objects.create(
        entry=entry, account=contra, amount=Decimal("10.0000"), quantity=Decimal("1.00000000")
    )

    invariants = [v.invariant for v in check_invariants(connection)]
    assert "negative_holding" in invariants


def test_the_command_exits_non_zero_on_a_violation() -> None:
    """So it works as a cron guard and a deploy gate, not only as something a human reads."""
    user = UserFactory.create()
    overdrawn = AccountFactory.create(owner=user, account_type=AccountType.ASSET)
    equity = AccountFactory.create(owner=user, account_type=AccountType.EQUITY)
    entry = JournalEntry.objects.create(description="overdraft by hand")
    JournalLine.objects.create(entry=entry, account=overdrawn, amount=Decimal("-50.0000"))
    JournalLine.objects.create(entry=entry, account=equity, amount=Decimal("50.0000"))

    with pytest.raises(CommandError, match="1 ledger invariant violation"):
        call_command("check_ledger_invariants")


def test_the_command_audits_the_all_clear_too() -> None:
    """The clean runs are the valuable rows: a gap in them says the check stopped running."""
    account = AccountFactory.create()
    fund_account(account, Decimal("100.00"))

    call_command("check_ledger_invariants", quiet=True)

    event = AuditEvent.objects.filter(action=AuditAction.LEDGER_RECONCILED).latest("created_at")
    assert event.context["violations"] == 0


def test_the_task_records_violations_without_raising() -> None:
    """A failing Celery task gets retried, and re-scanning a corrupt ledger just re-reports it.

    So the task's contract is the audit row and the log line, not an exception. Capable of failing
    against the natural implementation that reuses the command's ``CommandError``.
    """
    from ledger.tasks import check_ledger_invariants

    user = UserFactory.create()
    overdrawn = AccountFactory.create(owner=user, account_type=AccountType.ASSET)
    equity = AccountFactory.create(owner=user, account_type=AccountType.EQUITY)
    entry = JournalEntry.objects.create(description="overdraft by hand")
    JournalLine.objects.create(entry=entry, account=overdrawn, amount=Decimal("-50.0000"))
    JournalLine.objects.create(entry=entry, account=equity, amount=Decimal("50.0000"))

    result = check_ledger_invariants()

    assert result == {"violations": 1}
    event = AuditEvent.objects.filter(action=AuditAction.LEDGER_RECONCILED).latest("created_at")
    assert event.context["violations"] == 1
    assert "negative_asset_balance" in event.context["sample"][0]
