"""Read endpoints answer in a constant number of queries, whatever the data size.

Balances are derived, not stored (ADR-0008, ADR-0020): ``/accounts/`` sums an account's signed
journal lines and ``/portfolio/`` derives cash, holdings and valuation the same way. That design is
only defensible because the aggregation happens *in one query per collection* —
:meth:`accounts.models.AccountQuerySet.with_balance` annotates a ``Sum`` over the whole queryset
rather than calling :func:`ledger.services.get_balance` per row, and
:func:`trading.portfolio.holdings_for` prices each holding from the ``last_price`` column that
``select_related`` already fetched, so valuation costs nothing extra.

Until this file, that claim lived only in docstrings. Nothing failed if someone reintroduced a
per-row ``get_balance()``; the endpoints would keep returning correct numbers and would simply get
slower in proportion to how much the customer owned — the failure mode that does not show up on a
seeded developer database and does show up on the largest account in production.

**The assertion is a comparison, not a constant.** Pinning "`/accounts/` takes 7 queries" measures
the auth stack as much as the read path and has to be renumbered whenever anything upstream changes,
which trains people to update the number instead of reading it. Measuring the same endpoint at two
data sizes and asserting the counts are *equal* tests the property that actually matters: the count
does not depend on how many rows come back. A per-row query fails it immediately and by a margin.

Sizes stay under each endpoint's page size (20 for cursor pagination, 50 for holdings) so the large
case genuinely serialises every row it created. Above the page size the counts would match for the
uninteresting reason that the extra rows were never fetched.
"""

from decimal import Decimal

import pytest
from django.contrib.auth.models import User
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from rest_framework.test import APIClient

from tests.factories import (
    AccountFactory,
    InstrumentFactory,
    OrderFactory,
    fund_account,
    give_shares,
)

#: Rows in the "small" and "large" arrangements. The large one stays under every page size in play.
SMALL = 2
LARGE = 15


def _measure(client: APIClient, url: str) -> int:
    """Queries issued while serving ``url``, after a warm-up request.

    The warm-up is load-bearing. The first authenticated request in a test also populates Django's
    content-type cache and resolves the token's session for the first time, so measuring it would
    charge the read path for work that happens once per process and make the two arrangements
    differ for a reason that has nothing to do with row counts.
    """
    assert client.get(url).status_code == 200
    with CaptureQueriesContext(connection) as captured:
        response = client.get(url)
    assert response.status_code == 200
    return len(captured)


def _assert_constant(client: APIClient, url: str, grow: object) -> None:
    """Measure, add rows, measure again, and require the two counts to match."""
    baseline = _measure(client, url)
    grow()  # type: ignore[operator]
    after = _measure(client, url)

    assert after == baseline, (
        f"{url} issued {baseline} queries for {SMALL} rows and {after} for {LARGE}. "
        f"A read path that scales with row count has been introduced — most likely a per-row "
        f"`get_balance()`/`get_quantity()` where an annotation belonged, or a dropped "
        f"`select_related`."
    )


@pytest.mark.django_db
def test_account_list_queries_do_not_grow_with_account_count(
    auth_client: APIClient, password_user: User
) -> None:
    """``/accounts/`` costs the same for fifteen accounts as for two.

    The annotation this pins is ``with_balance()``. Funding each account also creates an *equity*
    opening-balances account, which ``spendable()`` excludes — so the list grows by exactly the
    accounts created here, and each one has journal lines for the ``Sum`` to actually aggregate.
    """

    def arrange(count: int) -> None:
        for _ in range(count):
            fund_account(AccountFactory.create(owner=password_user), Decimal("100.00"))

    arrange(SMALL)
    _assert_constant(auth_client, reverse("account-list"), lambda: arrange(LARGE - SMALL))


@pytest.mark.django_db
def test_holdings_queries_do_not_grow_with_holding_count(
    auth_client: APIClient, password_user: User
) -> None:
    """``/holdings/`` costs the same for fifteen positions as for two.

    This is the endpoint where an N+1 would be most natural to write: every holding needs a current
    price, and fetching one per instrument would look entirely reasonable. It costs nothing because
    the price is a column on the instrument ``select_related`` has already loaded.
    """

    def arrange(count: int) -> None:
        for _ in range(count):
            give_shares(
                password_user, InstrumentFactory.create(), Decimal("10"), Decimal("1000.00")
            )

    arrange(SMALL)
    _assert_constant(auth_client, reverse("holdings"), lambda: arrange(LARGE - SMALL))


@pytest.mark.django_db
def test_portfolio_queries_do_not_grow_with_holding_count(
    auth_client: APIClient, password_user: User, funded_cash_account: object
) -> None:
    """``/portfolio/`` costs the same for fifteen positions as for two.

    Three aggregates — holdings, cash, realized P&L — and none of them iterate.
    """

    def arrange(count: int) -> None:
        for _ in range(count):
            give_shares(
                password_user, InstrumentFactory.create(), Decimal("10"), Decimal("1000.00")
            )

    arrange(SMALL)
    _assert_constant(auth_client, reverse("portfolio"), lambda: arrange(LARGE - SMALL))


@pytest.mark.django_db
def test_order_list_queries_do_not_grow_with_order_count(
    auth_client: APIClient, password_user: User, funded_cash_account: object
) -> None:
    """``/orders/`` costs the same for fifteen orders as for two.

    Pins the ``select_related("instrument")`` that keeps the serializer's symbol lookup free.
    """
    instrument = InstrumentFactory.create()

    def arrange(count: int) -> None:
        for _ in range(count):
            OrderFactory.create(
                user=password_user, instrument=instrument, cash_account=funded_cash_account
            )

    arrange(SMALL)
    _assert_constant(auth_client, reverse("order-list-create"), lambda: arrange(LARGE - SMALL))
