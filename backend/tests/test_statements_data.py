"""What a statement is made of, before anything is rendered (ADR-0021).

The interesting half of statement generation is arithmetic and boundaries, and none of it needs a
PDF to check. Everything here asserts against the dataclasses ``statements.services`` builds —
which is the whole reason the renderer is a separate module taking one of them as its argument.
"""

from datetime import date, datetime
from decimal import Decimal

import pytest
from django.contrib.auth.models import User
from freezegun import freeze_time

from accounts.models import Account, AccountType
from markets.models import Instrument
from statements.services import (
    build_brokerage_statement,
    build_cash_statement,
    has_activity,
    month_containing,
    parse_period,
    previous_month,
    statement_accounts,
)
from tests.factories import (
    AccountFactory,
    InstrumentFactory,
    PriceTickFactory,
    fund_account,
    give_shares,
    post_balanced_entry,
)
from trading.services import place_order

pytestmark = pytest.mark.django_db

JULY = month_containing(date(2026, 7, 15))
AUGUST = month_containing(date(2026, 8, 15))


# --------------------------------------------------------------------------------------------
# Periods
# --------------------------------------------------------------------------------------------


def test_a_period_covers_the_whole_month() -> None:
    assert JULY.start == date(2026, 7, 1)
    assert JULY.end == date(2026, 7, 31)
    assert JULY.label == "2026-07"


def test_end_at_is_the_instant_after_the_last_day() -> None:
    """The inclusive/exclusive split. A line at 23:59:59.999 on the 31st belongs to July."""
    assert JULY.end_at == datetime.fromisoformat("2026-08-01T00:00:00+00:00")
    assert JULY.end_at == AUGUST.start_at


def test_february_in_a_leap_year_has_twenty_nine_days() -> None:
    assert month_containing(date(2028, 2, 3)).end == date(2028, 2, 29)


def test_previous_month_crosses_a_year_boundary() -> None:
    assert previous_month(date(2027, 1, 4)).label == "2026-12"


def test_parse_period_reads_a_month_and_rejects_anything_else() -> None:
    assert parse_period("2026-07").end == date(2026, 7, 31)
    with pytest.raises(ValueError):  # noqa: PT011 — strptime's message is not ours to pin
        parse_period("July 2026")


# --------------------------------------------------------------------------------------------
# Which accounts get one
# --------------------------------------------------------------------------------------------


def test_only_asset_cash_accounts_get_a_statement(
    password_user: User, instrument: Instrument
) -> None:
    """Positions belong to the brokerage statement; equity and income are bookkeeping.

    ``fund_account`` leaves an opening-balances equity account behind and a sell creates a
    realized-P&L income account — the other side of the customer's own money, not something they
    hold. A statement for either would be a page of double-entry plumbing.
    """
    cash = AccountFactory.create(owner=password_user, name="Everyday")
    fund_account(cash, Decimal("500.0000"))
    give_shares(password_user, instrument, Decimal("1"), Decimal("100.0000"))
    AccountFactory.create(owner=password_user, name="Realized P&L", account_type=AccountType.INCOME)

    eligible = list(statement_accounts(password_user))

    assert [account.name for account in eligible] == ["Everyday"]


# --------------------------------------------------------------------------------------------
# Cash statements
# --------------------------------------------------------------------------------------------


def test_opening_balance_is_what_was_there_before_the_period(password_user: User) -> None:
    cash = AccountFactory.create(owner=password_user, name="Everyday")
    with freeze_time("2026-06-20T10:00:00Z"):
        fund_account(cash, Decimal("800.0000"))

    data = build_cash_statement(cash, JULY)

    assert data.opening_balance == Decimal("800.0000")
    assert data.closing_balance == Decimal("800.0000")
    assert data.lines == []


def test_the_running_balance_ends_at_the_closing_balance(password_user: User) -> None:
    """The last row of the table and the summary box cannot disagree — they are the same number."""
    cash = AccountFactory.create(owner=password_user, name="Everyday")
    other = AccountFactory.create(owner=password_user, name="Savings")
    with freeze_time("2026-06-30T23:00:00Z"):
        fund_account(cash, Decimal("1000.0000"))
    with freeze_time("2026-07-05T09:00:00Z"):
        post_balanced_entry(cash, other, Decimal("250.0000"), description="rent")
    with freeze_time("2026-07-19T09:00:00Z"):
        post_balanced_entry(other, cash, Decimal("60.0000"), description="refund")

    data = build_cash_statement(cash, JULY)

    assert data.opening_balance == Decimal("1000.0000")
    assert [line.amount for line in data.lines] == [Decimal("-250.0000"), Decimal("60.0000")]
    assert [line.balance for line in data.lines] == [Decimal("750.0000"), Decimal("810.0000")]
    assert data.closing_balance == data.lines[-1].balance == Decimal("810.0000")
    assert data.line_count == 2
    assert data.total_in == Decimal("60.0000")
    assert data.total_out == Decimal("-250.0000")


def test_the_last_second_of_the_month_is_inside_the_period(password_user: User) -> None:
    """The boundary bug this design is most likely to have: a line landing in neither month."""
    cash = AccountFactory.create(owner=password_user, name="Everyday")
    other = AccountFactory.create(owner=password_user, name="Savings")
    with freeze_time("2026-06-15T09:00:00Z"):
        fund_account(cash, Decimal("100.0000"))
    with freeze_time("2026-07-31T23:59:59.999999Z"):
        post_balanced_entry(cash, other, Decimal("1.0000"), description="just in time")

    july = build_cash_statement(cash, JULY)
    august = build_cash_statement(cash, AUGUST)

    assert july.line_count == 1
    assert july.closing_balance == Decimal("99.0000")
    assert august.line_count == 0
    assert august.opening_balance == Decimal("99.0000")


def test_a_line_from_the_next_month_is_not_in_this_statement(password_user: User) -> None:
    cash = AccountFactory.create(owner=password_user, name="Everyday")
    other = AccountFactory.create(owner=password_user, name="Savings")
    with freeze_time("2026-07-10T09:00:00Z"):
        fund_account(cash, Decimal("500.0000"))
    with freeze_time("2026-08-01T00:00:00Z"):
        post_balanced_entry(cash, other, Decimal("500.0000"), description="next month")

    data = build_cash_statement(cash, JULY)

    assert data.line_count == 1
    assert data.closing_balance == Decimal("500.0000")


def test_the_statement_describes_its_owner_and_its_account(password_user: User) -> None:
    cash = AccountFactory.create(owner=password_user, name="Everyday")

    data = build_cash_statement(cash, JULY)

    assert data.account_id == str(cash.pk)
    assert data.account_name == "Everyday"
    assert data.user_label == password_user.get_username()
    assert data.period.label == "2026-07"


# --------------------------------------------------------------------------------------------
# Brokerage statements
# --------------------------------------------------------------------------------------------


def test_positions_are_valued_at_the_last_price_inside_the_period(
    password_user: User, instrument: Instrument
) -> None:
    """A July statement must not revalue itself in August — that is not a statement."""
    with freeze_time("2026-07-02T09:00:00Z"):
        give_shares(password_user, instrument, Decimal("10"), Decimal("1000.0000"))
    with freeze_time("2026-07-30T16:00:00Z"):
        PriceTickFactory.create(instrument=instrument, price=Decimal("130.0000"))
    with freeze_time("2026-08-04T16:00:00Z"):
        PriceTickFactory.create(instrument=instrument, price=Decimal("999.0000"))
    instrument.last_price = Decimal("999.0000")
    instrument.save(update_fields=["last_price"])

    data = build_brokerage_statement(password_user, JULY)

    assert data.holdings[0].last_price == Decimal("130.0000")
    assert data.market_value == Decimal("1300.0000")
    assert data.cost_basis == Decimal("1000.0000")
    assert data.unrealized_pnl == Decimal("300.0000")


def test_an_instrument_with_no_tick_in_the_period_falls_back_to_its_seed_price(
    password_user: User, instrument: Instrument
) -> None:
    with freeze_time("2026-07-02T09:00:00Z"):
        give_shares(password_user, instrument, Decimal("2"), Decimal("200.0000"))

    data = build_brokerage_statement(password_user, JULY)

    assert data.holdings[0].last_price == instrument.initial_price


def test_shares_bought_after_the_period_are_not_in_it(
    password_user: User, instrument: Instrument
) -> None:
    """``as_of`` goes on the annotation, not on the result: the *sum* has to stop at the edge."""
    with freeze_time("2026-07-04T09:00:00Z"):
        give_shares(password_user, instrument, Decimal("3"), Decimal("300.0000"))
    with freeze_time("2026-08-04T09:00:00Z"):
        give_shares(password_user, instrument, Decimal("7"), Decimal("700.0000"))

    july = build_brokerage_statement(password_user, JULY)
    august = build_brokerage_statement(password_user, AUGUST)

    assert july.holdings[0].quantity == Decimal("3.00000000")
    assert july.cost_basis == Decimal("300.0000")
    assert august.holdings[0].quantity == Decimal("10.00000000")


def test_trades_and_realized_pnl_are_bounded_by_the_period(
    password_user: User, instrument: Instrument
) -> None:
    cash = AccountFactory.create(owner=password_user, name="Brokerage cash")
    with freeze_time("2026-07-01T09:00:00Z"):
        fund_account(cash, Decimal("10000.0000"))
        give_shares(password_user, instrument, Decimal("8"), Decimal("800.0000"))

    instrument.last_price = Decimal("150.0000")
    instrument.save(update_fields=["last_price"])
    with freeze_time("2026-07-20T14:30:00Z"):
        place_order(
            user=password_user,
            instrument=instrument,
            cash_account=cash,
            side="sell",
            order_type="market",
            quantity=Decimal("4"),
        )
    with freeze_time("2026-08-03T14:30:00Z"):
        place_order(
            user=password_user,
            instrument=instrument,
            cash_account=cash,
            side="sell",
            order_type="market",
            quantity=Decimal("2"),
        )

    july = build_brokerage_statement(password_user, JULY)

    assert len(july.trades) == 1
    assert july.trades[0].side == "sell"
    assert july.trades[0].quantity == Decimal("4.00000000")
    assert july.trades[0].price == Decimal("150.0000")
    assert july.trades[0].notional == Decimal("600.0000")
    # Sold 4 of 8 at 150; basis removed is 800 × 4/8 = 400, so the gain is 200 — July's alone.
    assert july.realized_pnl == Decimal("200.0000")
    assert july.holdings[0].quantity == Decimal("4.00000000")


def test_a_partial_exit_removes_basis_proportionally_not_by_average_cost(
    password_user: User,
) -> None:
    """The Week 5 rounding edge, seen from the statement.

    ``basis × qty ÷ held`` in one expression. Deriving an average cost first and multiplying rounds
    twice, and the second rounding has nothing left to correct it — a hundredth of a cent of basis
    strands in the position and the statement reports a cost the customer did not pay.

    Selling **two** of three is what discriminates: at a quantity of one the two formulas round to
    the same answer, so a test written that way would pass against the broken version.
    """
    instrument = InstrumentFactory.create(symbol="ODD", initial_price=Decimal("33.3333"))
    cash = AccountFactory.create(owner=password_user, name="Brokerage cash")
    with freeze_time("2026-07-01T09:00:00Z"):
        fund_account(cash, Decimal("10000.0000"))
        give_shares(password_user, instrument, Decimal("3"), Decimal("100.0000"))
    with freeze_time("2026-07-11T09:00:00Z"):
        place_order(
            user=password_user,
            instrument=instrument,
            cash_account=cash,
            side="sell",
            order_type="market",
            quantity=Decimal("2"),
        )

    data = build_brokerage_statement(password_user, JULY)

    # 100 × 2/3 = 66.6667 removed, so 33.3333 remains. Rounding first gives 33.3333 × 2 = 66.6666
    # removed and 33.3334 left — a hundredth of a cent of basis that was never paid.
    assert data.cost_basis == Decimal("33.3333")


def test_has_activity_is_false_for_someone_who_never_traded(password_user: User) -> None:
    assert has_activity(password_user, JULY) is False


def test_has_activity_is_true_while_a_position_is_held(
    password_user: User, instrument: Instrument
) -> None:
    with freeze_time("2026-06-02T09:00:00Z"):
        give_shares(password_user, instrument, Decimal("1"), Decimal("100.0000"))

    assert has_activity(password_user, JULY) is True


def test_one_users_ledger_never_reaches_anothers_statement(
    password_user: User, instrument: Instrument
) -> None:
    with freeze_time("2026-07-03T09:00:00Z"):
        give_shares(password_user, instrument, Decimal("5"), Decimal("500.0000"))
    stranger: Account = AccountFactory.create()

    data = build_brokerage_statement(stranger.owner, JULY)

    assert data.holdings == []
    assert data.market_value == Decimal("0.0000")
