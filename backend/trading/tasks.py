"""The limit-order matching sweep (ADR-0018, ADR-0019).

Runs after every price advance. A resting order fills when the market crosses its limit, and is
rejected when it crosses but can no longer be afforded — a resting order does *not* reserve cash or
shares, which is the honest boundary this design accepts (ADR-0018).
"""

import logging
from typing import Any

from celery import shared_task
from django.db import transaction

from audit.context import audit_context

from .models import Order, OrderStatus
from .services import REJECT_REASONS, REJECTABLE, execute_fill, reject_order

logger = logging.getLogger(__name__)


@shared_task(name="trading.match_resting_orders")
def match_resting_orders() -> dict[str, int]:
    """Fill every resting order the market has crossed.

    Candidate ids are collected without locks and each order is then processed in its own short
    transaction. The alternative — one transaction spanning the whole sweep — would hold every
    account row it touched until the last order finished, serializing the sweep against every live
    trader.
    """
    candidate_ids = list(Order.objects.filter(status=OrderStatus.OPEN).values_list("pk", flat=True))

    outcomes = {"filled": 0, "rejected": 0, "skipped": 0}
    for pk in candidate_ids:
        outcomes[_match_one(pk)] += 1
    return outcomes


def _match_one(pk: Any) -> str:
    """Try to fill one resting order. Returns ``filled``, ``rejected`` or ``skipped``.

    The order row stays locked for the whole fill attempt, so two workers cannot fill the same
    order; ``skip_locked`` means the loser moves on to the next order instead of queueing behind it.
    """
    try:
        with transaction.atomic():
            order = (
                Order.objects.select_for_update(skip_locked=True, of=("self",))
                # of=("self",) is load-bearing alongside select_related: without it Postgres locks
                # every joined row too, including accounts — acquired here in join order rather
                # than the global ascending order, which is precisely how a deadlock with a
                # concurrent transfer gets built (ADR-0010).
                .select_related("instrument", "cash_account", "user")
                .filter(pk=pk, status=OrderStatus.OPEN)
                .first()
            )
            # None means another worker holds it (skip_locked) or it resolved since the scan.
            if order is None:
                return "skipped"

            price = order.instrument.current_price
            if not order.crosses(price):
                return "skipped"

            with audit_context(actor=order.user):
                execute_fill(order, price)
            return "filled"

    except REJECTABLE as exc:
        # Reached only after the transaction above has rolled back, which is the point: a rejection
        # written inside it would be undone by the very exception it records (ADR-0014). The order
        # is re-fetched rather than carried out of the block — an instance from a rolled-back
        # transaction is not one to keep writing through, and this path is rare enough that the
        # extra query costs nothing.
        order = Order.objects.select_related("instrument", "user").get(pk=pk)
        reason = REJECT_REASONS[type(exc)]
        logger.info("rejecting resting order %s: %s", order.pk, reason)
        with audit_context(actor=order.user):
            reject_order(order, reason=reason)
        return "rejected"
