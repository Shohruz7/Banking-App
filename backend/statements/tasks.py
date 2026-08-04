"""Monthly statement generation (ADR-0021, ADR-0019).

Beat fires this a quarter past midnight on the 1st for the month that just closed. It is safe to
run twice, and safe to run late: a statement covers a period that is over, so the answer does not
change between the 1st and the 8th.

**Idempotency is the database's job, not a check.** Each statement is created inside its own
transaction, and a second run for the same month collides with the partial unique indexes on
``Statement``. Checking first and then writing would leave the window between the two open — the
same reasoning ``ledger.transfer`` applies to idempotency keys and ``trading.cancel_order`` applies
to status transitions.
"""

import logging
from typing import Any

from celery import shared_task
from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction

from accounts.models import Account

from .models import Statement, StatementKind
from .render import render_brokerage_statement, render_cash_statement
from .services import (
    Period,
    build_brokerage_statement,
    build_cash_statement,
    has_activity,
    parse_period,
    previous_month,
    statement_accounts,
)

logger = logging.getLogger(__name__)


@shared_task(name="statements.generate_monthly")
def generate_monthly_statements(period: str | None = None) -> dict[str, Any]:
    """Generate every statement for a closed month.

    ``period`` is ``YYYY-MM``; omitted, it is the month that just closed. Returns counts rather
    than objects — there is no result backend (ADR-0019), and the rows are the result.

    One account's failure is logged and skipped rather than aborting the month. A month missing one
    statement is a fixable gap; a month missing all of them because the last account raised is an
    outage.
    """
    window = parse_period(period) if period else previous_month()
    outcomes = {"cash": 0, "brokerage": 0, "skipped": 0, "failed": 0}

    for user in User.objects.order_by("pk").iterator():
        for account in statement_accounts(user):
            outcomes[_generate_cash(account, window)] += 1
        outcomes[_generate_brokerage(user, window)] += 1

    logger.info("statements for %s: %s", window.label, outcomes)
    return {"period": window.label, **outcomes}


def _generate_cash(account: Account, period: Period) -> str:
    """One account's cash statement. Returns the outcome key to count."""
    if Statement.objects.filter(
        account=account, period_start=period.start, kind=StatementKind.CASH
    ).exists():
        return "skipped"

    try:
        data = build_cash_statement(account, period)
        # Nothing happened and there was nothing there — a statement of two zeroes helps no one.
        if not data.lines and data.opening_balance == 0 and data.closing_balance == 0:
            return "skipped"

        return _persist(
            Statement(
                user=account.owner,
                account=account,
                kind=StatementKind.CASH,
                period_start=period.start,
                period_end=period.end,
                opening_balance=data.opening_balance,
                closing_balance=data.closing_balance,
                line_count=data.line_count,
            ),
            render_cash_statement(data),
            outcome="cash",
        )
    except Exception:
        logger.exception("statement generation failed for account %s", account.pk)
        return "failed"


def _generate_brokerage(user: User, period: Period) -> str:
    """The user's brokerage statement, if they held or traded anything."""
    if Statement.objects.filter(
        user=user, period_start=period.start, kind=StatementKind.BROKERAGE
    ).exists():
        return "skipped"

    try:
        if not has_activity(user, period):
            return "skipped"

        data = build_brokerage_statement(user, period)
        return _persist(
            Statement(
                user=user,
                account=None,
                kind=StatementKind.BROKERAGE,
                period_start=period.start,
                period_end=period.end,
                # A brokerage statement's "balances" are what the positions were worth: an opening
                # figure is not meaningful for a portfolio mid-life, so the two columns carry cost
                # basis and market value, and the per-symbol detail lives in the PDF.
                opening_balance=data.cost_basis,
                closing_balance=data.market_value,
                line_count=len(data.trades),
            ),
            render_brokerage_statement(data),
            outcome="brokerage",
        )
    except Exception:
        logger.exception("brokerage statement generation failed for user %s", user.pk)
        return "failed"


def _persist(statement: Statement, pdf: bytes, *, outcome: str) -> str:
    """Write the file and the row, or neither.

    ``FileField.save`` pushes the bytes through the storage backend and *then* saves the model, so
    a unique-index collision arrives after the file already exists. Storage is not transactional
    and cannot be rolled back with the row, so the orphan is removed by hand — the checks in the
    callers make this the rare path, and the index is what makes it correct rather than likely.
    """
    try:
        with transaction.atomic():
            statement.file.save(f"{statement.period_start:%Y-%m}.pdf", ContentFile(pdf), save=True)
    except IntegrityError:
        statement.file.delete(save=False)
        return "skipped"
    return outcome
