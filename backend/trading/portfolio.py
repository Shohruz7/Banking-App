"""Portfolio and holdings valuation — read-only arithmetic over the ledger (ADR-0020).

**Nothing in this module is stored.** A holding's share count is the sum of its position account's
line quantities; its cost basis is the sum of the same lines' amounts; realized P&L is the balance
of an income account; unrealized P&L is market value minus cost basis. Every figure the portfolio
endpoint reports is derived on read, which is why none of it can drift from the postings that
produced it — the property Week 5 built the position accounts to have.

Three callers share this code: ``/holdings/``, ``/portfolio/``, and the brokerage statement. The
statement values a position at a *period boundary* rather than now, which is what ``as_of`` and
``price_lookup`` are for — a second implementation valuing the same rows is exactly the drift this
design exists to prevent.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import cast
from uuid import UUID

from django.contrib.auth.models import User
from django.db.models import DecimalField, Q, Sum, Value
from django.db.models.functions import Coalesce

from accounts.models import Account, AccountType
from common.money import quantize_money
from ledger.models import JournalLine
from markets.models import Instrument

from .services import REALIZED_PNL_ACCOUNT_NAME

_ZERO = Decimal("0.0000")
_ZERO_SHARES = Decimal("0E-8")

#: How a holding is priced. Defaults to the instrument's latest tick; the statement passes a lookup
#: that answers with the last price *inside* the reporting period instead.
PriceLookup = Callable[[Instrument], Decimal]


@dataclass(frozen=True)
class Holding:
    """One position, valued. Every field is derived — see the module docstring."""

    instrument: Instrument
    account_id: UUID
    quantity: Decimal
    cost_basis: Decimal
    average_cost: Decimal
    last_price: Decimal
    market_value: Decimal
    unrealized_pnl: Decimal


@dataclass(frozen=True)
class Portfolio:
    """What everything a user owns is worth, and how much of that is gain."""

    cash: Decimal
    holdings_value: Decimal
    cost_basis: Decimal
    unrealized_pnl: Decimal
    realized_pnl: Decimal
    total_value: Decimal
    positions: list[Holding]


def _current_price(instrument: Instrument) -> Decimal:
    return instrument.current_price


def holdings_for(
    user: User,
    *,
    as_of: datetime | None = None,
    price_lookup: PriceLookup = _current_price,
) -> list[Holding]:
    """Every instrument ``user`` holds, valued.

    One annotated query over the position accounts, so N holdings still cost one query rather than
    N. Positions that have been fully sold are omitted: an account with zero shares is history, not
    a holding.
    """
    positions = (
        Account.objects.filter(owner=user)
        .positions()
        .select_related("instrument")
        .with_balance(as_of=as_of)
        .with_quantity(as_of=as_of)
    )

    holdings: list[Holding] = []
    for account in positions:
        quantity: Decimal = account.quantity  # type: ignore[attr-defined]
        if quantity == _ZERO_SHARES:
            continue

        cost_basis: Decimal = account.balance  # type: ignore[attr-defined]
        # Non-null by construction: .positions() filters instrument__isnull=False.
        instrument = cast(Instrument, account.instrument)
        last_price = price_lookup(instrument)
        market_value = quantize_money(last_price * quantity)
        holdings.append(
            Holding(
                instrument=instrument,
                account_id=account.pk,
                quantity=quantity,
                cost_basis=cost_basis,
                average_cost=quantize_money(cost_basis / quantity),
                last_price=last_price,
                market_value=market_value,
                unrealized_pnl=market_value - cost_basis,
            )
        )
    return holdings


def cash_balance_for(user: User, *, as_of: datetime | None = None) -> Decimal:
    """The user's spendable money: the sum of their **asset** cash accounts.

    The reasoning that used to live here — that ``cash()`` also admits the opening-balances equity
    account and the realized-P&L income account, so summing them nets a funded portfolio to roughly
    zero — is now the docstring of ``AccountQuerySet.spendable()``, which is where it belongs. This
    module and ``statements.services`` were the only two callers that got it right by hand; Week 8
    found the four endpoints that did not.
    """
    total = (
        Account.objects.filter(owner=user)
        .spendable()
        .with_balance(as_of=as_of)
        .aggregate(
            total=Coalesce(
                Sum("balance"),
                Value(_ZERO, output_field=DecimalField(max_digits=20, decimal_places=4)),
            )
        )["total"]
    )
    return quantize_money(total)


def realized_pnl_for(
    user: User, *, since: datetime | None = None, until: datetime | None = None
) -> Decimal:
    """Realized P&L, in the sign a human expects.

    ``trading.services._sell_lines`` posts the residual as ``amount=-gain``, so a *gain* leaves the
    income account with a negative balance — correct signed double-entry, and the opposite of what
    a statement should print. The negation lives here and nowhere else.

    Bounded by ``since``/``until`` for a statement period; unbounded it is the lifetime figure. The
    account is queried rather than fetched with ``get_or_create``: reading a portfolio should not
    create an account for a user who has never sold anything.
    """
    window = Q()
    if since is not None:
        window &= Q(created_at__gte=since)
    if until is not None:
        window &= Q(created_at__lt=until)

    total = (
        JournalLine.objects.filter(
            account__owner=user,
            account__account_type=AccountType.INCOME,
            account__name=REALIZED_PNL_ACCOUNT_NAME,
        )
        .filter(window)
        .aggregate(
            total=Coalesce(
                Sum("amount"),
                Value(_ZERO, output_field=DecimalField(max_digits=20, decimal_places=4)),
            )
        )["total"]
    )
    return -quantize_money(total)


def portfolio_for(user: User) -> Portfolio:
    """Value everything ``user`` owns, right now."""
    positions = holdings_for(user)
    cash = cash_balance_for(user)
    holdings_value = sum((h.market_value for h in positions), start=_ZERO)
    cost_basis = sum((h.cost_basis for h in positions), start=_ZERO)

    return Portfolio(
        cash=cash,
        holdings_value=holdings_value,
        cost_basis=cost_basis,
        unrealized_pnl=holdings_value - cost_basis,
        realized_pnl=realized_pnl_for(user),
        total_value=cash + holdings_value,
        positions=positions,
    )
