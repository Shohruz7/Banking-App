"""Generating a month, and generating it twice (ADR-0021).

The property that matters is idempotency, and it is enforced by two partial unique indexes rather
than by the task checking first — so the test that proves it runs the whole task again and counts
rows, not calls.
"""

from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest import mock

import pytest
from django.conf import settings
from django.contrib.auth.models import User
from freezegun import freeze_time

from accounts.models import Account, AccountType
from markets.models import Instrument
from statements.models import Statement, StatementKind
from statements.tasks import generate_monthly_statements
from tests.factories import (
    AccountFactory,
    UserFactory,
    fund_account,
    give_shares,
    post_balanced_entry,
)

pytestmark = pytest.mark.django_db

JULY = "2026-07"


@pytest.fixture
def account_with_july_activity(password_user: User) -> Account:
    cash = AccountFactory.create(owner=password_user, name="Everyday")
    other = AccountFactory.create(owner=password_user, name="Savings")
    with freeze_time("2026-06-25T09:00:00Z"):
        fund_account(cash, Decimal("2000.0000"))
    with freeze_time("2026-07-08T09:00:00Z"):
        post_balanced_entry(cash, other, Decimal("120.0000"), description="groceries")
    return cash


def test_a_month_produces_one_statement_per_active_account(
    account_with_july_activity: Account,
) -> None:
    result = generate_monthly_statements(JULY)

    assert result["period"] == JULY
    assert result["cash"] == 2  # Everyday and Savings both moved
    statement = Statement.objects.get(account=account_with_july_activity)
    assert statement.kind == StatementKind.CASH
    assert statement.period_start == date(2026, 7, 1)
    assert statement.period_end == date(2026, 7, 31)
    assert statement.opening_balance == Decimal("2000.0000")
    assert statement.closing_balance == Decimal("1880.0000")
    assert statement.line_count == 1
    assert str(statement.file.name).endswith(".pdf")
    assert statement.file.read().startswith(b"%PDF")


def test_running_it_twice_leaves_one_statement_and_one_file(
    account_with_july_activity: Account,
) -> None:
    """Beat retries, operators re-run, and a month must not grow a second July."""
    generate_monthly_statements(JULY)
    first = Statement.objects.get(account=account_with_july_activity)

    second_run = generate_monthly_statements(JULY)

    assert second_run["cash"] == 0
    assert second_run["skipped"] >= 1
    assert Statement.objects.filter(account=account_with_july_activity).count() == 1
    assert Statement.objects.get(account=account_with_july_activity).file.name == first.file.name


def test_the_unique_index_is_what_makes_it_idempotent(
    account_with_july_activity: Account,
) -> None:
    """Not the existence check — that is only the optimization.

    With the pre-flight check patched out, the second run still cannot create a second July: the
    partial unique index rejects it, the task counts it as skipped, and the orphaned file it had
    already written is cleaned up.
    """
    generate_monthly_statements(JULY)
    files_after_one_run = _stored_files()

    with mock.patch("statements.tasks.Statement.objects.filter") as no_precheck:
        no_precheck.return_value.exists.return_value = False
        result = generate_monthly_statements(JULY)

    assert result["cash"] == 0
    assert Statement.objects.filter(account=account_with_july_activity).count() == 1
    # Storage is not transactional, so the file written before the index rejected the row has to
    # be removed by hand. If it were not, every retry would leave a copy behind forever.
    assert _stored_files() == files_after_one_run


def _stored_files() -> set[str]:
    return {
        str(path.relative_to(settings.MEDIA_ROOT))
        for path in Path(settings.MEDIA_ROOT).rglob("*.pdf")
    }


def test_a_dormant_account_with_a_balance_still_gets_a_statement(password_user: User) -> None:
    """No activity is a fact worth stating; a customer with money is owed a statement saying so."""
    cash = AccountFactory.create(owner=password_user, name="Dormant")
    with freeze_time("2026-04-02T09:00:00Z"):
        fund_account(cash, Decimal("75.0000"))

    generate_monthly_statements(JULY)

    statement = Statement.objects.get(account=cash)
    assert statement.line_count == 0
    assert statement.opening_balance == statement.closing_balance == Decimal("75.0000")


def test_an_empty_account_gets_nothing(password_user: User) -> None:
    """A statement of two zeroes helps no one."""
    AccountFactory.create(owner=password_user, name="Never used")

    result = generate_monthly_statements(JULY)

    assert result["cash"] == 0
    assert not Statement.objects.exists()


def test_a_brokerage_statement_is_generated_for_a_holder(
    password_user: User, instrument: Instrument
) -> None:
    with freeze_time("2026-07-03T09:00:00Z"):
        give_shares(password_user, instrument, Decimal("4"), Decimal("400.0000"))

    result = generate_monthly_statements(JULY)

    assert result["brokerage"] == 1
    statement = Statement.objects.get(kind=StatementKind.BROKERAGE)
    assert statement.account_id is None
    assert statement.user == password_user
    assert statement.opening_balance == Decimal("400.0000")  # cost basis
    assert statement.closing_balance == Decimal("400.0000")  # market value at period end


def test_position_and_bookkeeping_accounts_get_no_cash_statement(
    password_user: User, instrument: Instrument
) -> None:
    """A position account's balance is a cost basis; an equity account is the other side of a gift.

    Either one rendered as a bank statement would read as spendable money the customer does not
    have — the same misreading ``/accounts/`` was filtered to prevent in Week 5.
    """
    with freeze_time("2026-07-03T09:00:00Z"):
        give_shares(password_user, instrument, Decimal("4"), Decimal("400.0000"))

    generate_monthly_statements(JULY)

    scoped = Statement.objects.filter(kind=StatementKind.CASH).values_list(
        "account__name", flat=True
    )
    assert list(scoped) == []
    assert not Account.objects.filter(
        pk__in=Statement.objects.exclude(account=None).values("account"),
        account_type=AccountType.EQUITY,
    ).exists()


def test_a_brokerage_statement_is_not_regenerated_either(
    password_user: User, instrument: Instrument
) -> None:
    """The brokerage side has its own partial unique index, and its own pre-check in front of it."""
    with freeze_time("2026-07-03T09:00:00Z"):
        give_shares(password_user, instrument, Decimal("2"), Decimal("200.0000"))
    generate_monthly_statements(JULY)

    second_run = generate_monthly_statements(JULY)

    assert second_run["brokerage"] == 0
    assert Statement.objects.filter(kind=StatementKind.BROKERAGE).count() == 1


def test_a_failing_brokerage_statement_is_counted_not_raised(
    password_user: User, instrument: Instrument
) -> None:
    """Same containment as the cash path: one user's bad month is not everyone's."""
    with freeze_time("2026-07-03T09:00:00Z"):
        give_shares(password_user, instrument, Decimal("1"), Decimal("100.0000"))

    with mock.patch(
        "statements.tasks.render_brokerage_statement", side_effect=RuntimeError("boom")
    ):
        result = generate_monthly_statements(JULY)

    assert result["failed"] == 1
    assert result["brokerage"] == 0
    assert not Statement.objects.filter(kind=StatementKind.BROKERAGE).exists()


def test_a_statement_describes_itself_in_the_admin(password_user: User) -> None:
    """``__str__`` is what an operator reads in a change list; a bare object repr is useless."""
    cash = AccountFactory.create(owner=password_user, name="Everyday")
    with freeze_time("2026-06-20T09:00:00Z"):
        fund_account(cash, Decimal("10.0000"))
    generate_monthly_statements(JULY)

    cash_statement = Statement.objects.get(kind=StatementKind.CASH)
    assert str(cash_statement) == f"2026-07 {cash.pk}"
    assert cash_statement.period_label == "2026-07"


def test_one_failing_account_does_not_abort_the_month(password_user: User) -> None:
    """A month missing one statement is a gap; a month missing all of them is an outage."""
    first = AccountFactory.create(owner=password_user, name="A first")
    second = AccountFactory.create(owner=password_user, name="B second")
    with freeze_time("2026-06-20T09:00:00Z"):
        fund_account(first, Decimal("100.0000"))
        fund_account(second, Decimal("200.0000"))

    real_render = "statements.tasks.render_cash_statement"
    with mock.patch(real_render, side_effect=[RuntimeError("boom"), b"%PDF-fake"]):
        result = generate_monthly_statements(JULY)

    assert result["failed"] == 1
    assert result["cash"] == 1
    assert Statement.objects.count() == 1


def test_statements_are_scoped_to_their_own_user(password_user: User) -> None:
    mine = AccountFactory.create(owner=password_user, name="Mine")
    stranger = UserFactory.create()
    theirs = AccountFactory.create(owner=stranger, name="Theirs")
    with freeze_time("2026-06-20T09:00:00Z"):
        fund_account(mine, Decimal("10.0000"))
        fund_account(theirs, Decimal("20.0000"))

    generate_monthly_statements(JULY)

    assert Statement.objects.get(account=mine).user == password_user
    assert Statement.objects.get(account=theirs).user == stranger


def test_the_default_period_is_the_month_that_just_closed(password_user: User) -> None:
    """Beat passes no argument on the 1st; it must not generate the month it is standing in."""
    cash = AccountFactory.create(owner=password_user, name="Everyday")
    with freeze_time("2026-07-14T09:00:00Z"):
        fund_account(cash, Decimal("500.0000"))

    with freeze_time("2026-08-01T00:15:00Z"):
        result = generate_monthly_statements()

    assert result["period"] == "2026-07"
    assert Statement.objects.get(account=cash).period_start == date(2026, 7, 1)
