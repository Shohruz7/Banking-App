"""Statement periods and the data a statement is made of (ADR-0021).

This module answers "what happened to this account in July" and nothing else. It builds plain
dataclasses; :mod:`statements.render` turns them into a PDF and :mod:`statements.tasks` decides
when. The split is what makes the interesting half testable without ever parsing a PDF — every
balance, boundary and rounding decision below is asserted directly against these structures.

Everything here reads the ledger through the same derivations the API uses: the account
annotations from ``accounts.models`` and the valuation helpers in ``trading.portfolio``. A
statement that computed balances its own way would eventually disagree with the endpoint that
shows them, and the customer would be holding the PDF.
"""

import calendar
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.utils import timezone

from accounts.models import Account, AccountType
from ledger.models import JournalLine
from markets.models import Instrument, PriceTick
from trading.models import Order, OrderStatus
from trading.portfolio import Holding, PriceLookup, holdings_for, realized_pnl_for

_ZERO = Decimal("0.0000")


# --------------------------------------------------------------------------------------------
# Periods
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Period:
    """One calendar month, in UTC.

    ``end`` is the inclusive last day — what the PDF prints — while :attr:`end_at` is the exclusive
    instant after it, which is what every query uses. Getting those two confused is how a line
    posted at 23:59:59 on the 31st lands in neither month.
    """

    start: date
    end: date

    @property
    def start_at(self) -> datetime:
        return datetime.combine(self.start, time.min, tzinfo=UTC)

    @property
    def end_at(self) -> datetime:
        return datetime.combine(self.end + timedelta(days=1), time.min, tzinfo=UTC)

    @property
    def label(self) -> str:
        return f"{self.start:%Y-%m}"

    def __str__(self) -> str:
        return f"{self.start:%d %B %Y} – {self.end:%d %B %Y}"


def month_containing(day: date) -> Period:
    """The calendar month ``day`` falls in."""
    last = calendar.monthrange(day.year, day.month)[1]
    return Period(start=day.replace(day=1), end=day.replace(day=last))


def previous_month(today: date | None = None) -> Period:
    """The month that just closed. The default period for a Beat run on the 1st."""
    reference = today or timezone.now().date()
    return month_containing(reference.replace(day=1) - timedelta(days=1))


def parse_period(value: str) -> Period:
    """Parse ``YYYY-MM`` into a period. Raises ``ValueError`` on anything else."""
    parsed = datetime.strptime(value, "%Y-%m")  # noqa: DTZ007 — a month has no time or zone
    return month_containing(parsed.date())


# --------------------------------------------------------------------------------------------
# Statement data
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class StatementLine:
    """One row of a cash statement: a journal line, with the balance it left behind."""

    posted_at: datetime
    description: str
    amount: Decimal
    balance: Decimal


@dataclass(frozen=True)
class CashStatementData:
    """Everything printed on one account's monthly statement."""

    user_label: str
    period: Period
    account_id: str
    account_name: str
    opening_balance: Decimal
    closing_balance: Decimal
    lines: list[StatementLine]

    @property
    def line_count(self) -> int:
        return len(self.lines)

    @property
    def total_in(self) -> Decimal:
        return sum((line.amount for line in self.lines if line.amount > 0), start=_ZERO)

    @property
    def total_out(self) -> Decimal:
        return sum((line.amount for line in self.lines if line.amount < 0), start=_ZERO)


@dataclass(frozen=True)
class TradeRow:
    """One fill in the period."""

    resolved_at: datetime
    side: str
    symbol: str
    quantity: Decimal
    price: Decimal
    notional: Decimal


@dataclass(frozen=True)
class BrokerageStatementData:
    """Positions at the close of the period, the fills that got there, and the realized result."""

    user_label: str
    period: Period
    holdings: list[Holding]
    trades: list[TradeRow]
    realized_pnl: Decimal

    @property
    def cost_basis(self) -> Decimal:
        return sum((h.cost_basis for h in self.holdings), start=_ZERO)

    @property
    def market_value(self) -> Decimal:
        return sum((h.market_value for h in self.holdings), start=_ZERO)

    @property
    def unrealized_pnl(self) -> Decimal:
        return self.market_value - self.cost_basis


def user_label(user: User) -> str:
    """How a statement addresses its owner."""
    full_name = user.get_full_name().strip()
    return full_name or user.get_username()


# --------------------------------------------------------------------------------------------
# Building
# --------------------------------------------------------------------------------------------


def statement_accounts(user: User) -> Iterable[Account]:
    """The accounts a customer gets a statement for.

    Asset cash accounts only. Position accounts belong to the brokerage statement, and the
    opening-balances *equity* and realized-P&L *income* accounts are bookkeeping — the other side
    of the customer's own money, not something they hold.
    """
    return Account.objects.filter(owner=user, account_type=AccountType.ASSET).cash()


def build_cash_statement(account: Account, period: Period) -> CashStatementData:
    """Assemble one account's statement for ``period``.

    The opening balance is the account's balance *before* the period — the same annotation the
    accounts endpoint uses, asked as of an instant — and the closing balance is that plus the
    period's lines, accumulated the same way a reader would. Deriving the closing balance
    independently would be a second implementation of addition; making it the running total means
    the last row of the table and the summary box cannot disagree.
    """
    opening = _balance_before(account, period.start_at)

    running = opening
    lines: list[StatementLine] = []
    for line in _period_lines(account, period):
        running += line.amount
        lines.append(
            StatementLine(
                posted_at=line.created_at,
                description=line.entry.description,
                amount=line.amount,
                balance=running,
            )
        )

    return CashStatementData(
        user_label=user_label(account.owner),
        period=period,
        account_id=str(account.pk),
        account_name=account.name,
        opening_balance=opening,
        closing_balance=running,
        lines=lines,
    )


def build_brokerage_statement(user: User, period: Period) -> BrokerageStatementData:
    """Assemble the brokerage statement for ``period``.

    Positions are valued at the last price *inside* the period, not at today's — a statement for
    July that revalues itself every time it is regenerated is not a statement. Both the as-of
    aggregation and the period-end pricing go through ``trading.portfolio.holdings_for``, so the
    numbers here are the same arithmetic ``/holdings/`` runs.
    """
    holdings = holdings_for(user, as_of=period.end_at, price_lookup=period_end_prices(period))
    fills = (
        Order.objects.filter(
            user=user,
            status=OrderStatus.FILLED,
            resolved_at__gte=period.start_at,
            resolved_at__lt=period.end_at,
        )
        .select_related("instrument")
        .order_by("resolved_at")
    )

    trades = [
        TradeRow(
            # Non-null by construction: a filled order always carries the instant it resolved and
            # the price it resolved at.
            resolved_at=order.resolved_at,  # type: ignore[arg-type]
            side=order.side,
            symbol=order.instrument.symbol,
            quantity=order.filled_quantity,
            price=order.filled_price,  # type: ignore[arg-type]
            notional=(order.filled_price or _ZERO) * order.filled_quantity,
        )
        for order in fills
    ]

    return BrokerageStatementData(
        user_label=user_label(user),
        period=period,
        holdings=holdings,
        trades=trades,
        realized_pnl=realized_pnl_for(user, since=period.start_at, until=period.end_at),
    )


def period_end_prices(period: Period) -> PriceLookup:
    """A price lookup answering with each instrument's last tick *within* ``period``.

    One ``DISTINCT ON`` query for the whole market rather than one per holding. An instrument with
    no tick in the window falls back to its seed price, which is the same answer
    ``Instrument.current_price`` gives before the market has ever moved.
    """
    latest = (
        PriceTick.objects.filter(created_at__lt=period.end_at)
        .order_by("instrument_id", "-created_at")
        .distinct("instrument_id")
        .values_list("instrument_id", "price")
    )
    prices = dict(latest)

    def lookup(instrument: Instrument) -> Decimal:
        return prices.get(instrument.pk, instrument.initial_price)

    return lookup


def has_activity(user: User, period: Period) -> bool:
    """Whether the user did anything in the period worth generating a brokerage statement for."""
    if holdings_for(user, as_of=period.end_at):
        return True
    return Order.objects.filter(
        user=user,
        status=OrderStatus.FILLED,
        resolved_at__gte=period.start_at,
        resolved_at__lt=period.end_at,
    ).exists()


def _balance_before(account: Account, instant: datetime) -> Decimal:
    """The account's balance immediately before ``instant``."""
    row = Account.objects.filter(pk=account.pk).with_balance(as_of=instant).first()
    # The account was fetched by the caller, so it exists; the annotation is the only reason to
    # go back to the database at all.
    return row.balance if row is not None else _ZERO  # type: ignore[attr-defined]


def _period_lines(account: Account, period: Period) -> Iterator[JournalLine]:
    """The account's lines in the period, oldest first, streamed.

    ``.iterator()`` because a busy account over a month is not something to materialize; ordering
    by ``(created_at, id)`` because two lines of the same entry share a timestamp to the
    microsecond and a running balance needs a total order.
    """
    return (
        account.lines.filter(created_at__gte=period.start_at, created_at__lt=period.end_at)
        .select_related("entry")
        .order_by("created_at", "id")
        .iterator()
    )
