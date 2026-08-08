"""Orders — the client's *intent* to trade, and the record of what came of it (ADR-0018).

An order is not a money movement. The money movement is the ``JournalEntry`` in ``entry``, posted
through ``ledger.services.post_entry`` like every other movement in the system; this model records
who asked for what, and how it turned out. Keeping the two separate is what lets a rejected order
leave a durable trace without leaving a financial one.

Market orders fill in the request that creates them. Limit orders rest at ``OPEN`` until a price
tick crosses them and ``trading.tasks.match_resting_orders`` fills them.
"""

from decimal import Decimal

import uuid6
from django.conf import settings
from django.db import models


class OrderSide(models.TextChoices):
    BUY = "buy", "Buy"
    SELL = "sell", "Sell"


class OrderType(models.TextChoices):
    MARKET = "market", "Market"
    LIMIT = "limit", "Limit"


class OrderStatus(models.TextChoices):
    OPEN = "open", "Open"
    FILLED = "filled", "Filled"
    CANCELLED = "cancelled", "Cancelled"
    REJECTED = "rejected", "Rejected"


class Order(models.Model):
    """One instruction to buy or sell, and its outcome.

    ``quantity`` is ``NUMERIC(20,8)`` — fractional shares are the point, and eight places is enough
    to express a dollar's worth of anything. Prices and money stay at ``NUMERIC(20,4)`` (ADR-0009);
    the two precisions meet in the fill service, which quantizes the notional exactly once.
    """

    id = models.UUIDField(primary_key=True, default=uuid6.uuid7, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="orders",
    )
    instrument = models.ForeignKey(
        "markets.Instrument",
        on_delete=models.PROTECT,
        related_name="orders",
    )
    # Which account pays or receives. Passed explicitly rather than inferred from the user, because
    # "my cash account" stops being unambiguous the moment anyone has two.
    cash_account = models.ForeignKey(
        "accounts.Account",
        on_delete=models.PROTECT,
        related_name="orders",
    )

    side = models.CharField(max_length=4, choices=OrderSide.choices)
    order_type = models.CharField(max_length=6, choices=OrderType.choices)
    status = models.CharField(max_length=9, choices=OrderStatus.choices, default=OrderStatus.OPEN)

    quantity = models.DecimalField(max_digits=20, decimal_places=8)
    limit_price = models.DecimalField(max_digits=20, decimal_places=4, null=True, blank=True)

    # Always 0 or `quantity` in v1 — there is no order book to partially cross. The field exists so
    # partial fills are a service change rather than a migration (ADR-0018).
    filled_quantity = models.DecimalField(max_digits=20, decimal_places=8, default=Decimal("0E-8"))
    filled_price = models.DecimalField(max_digits=20, decimal_places=4, null=True, blank=True)
    # The posting this order produced. NULL until it fills, and forever if it never does.
    entry = models.ForeignKey(
        "ledger.JournalEntry",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="orders",
    )

    # Same nullable-unique retry token as transfers (ADR-0010): unique when present, and Postgres
    # unique indexes ignore NULLs, so keyless orders never collide.
    idempotency_key = models.CharField(max_length=64, null=True, blank=True, unique=True)
    # The digest of the request that placed this order (ADR-0024), set whenever a key is. The order
    # replay is scoped by user as well, which the entry replay cannot be — but the digest is what
    # catches a client reusing its *own* key for a different order.
    # DJ001 is suppressed on the field, for the reason given on JournalEntry.payload_fingerprint:
    # NULL means "no key, so nothing to compare", which "" cannot express.
    payload_fingerprint = models.CharField(max_length=80, null=True, blank=True)  # noqa: DJ001
    reject_reason = models.CharField(max_length=64, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(quantity__gt=0),
                name="order_quantity_positive",
            ),
            # A limit order without a price is unfillable; a market order with one is a lie about
            # how it will execute. Both directions are wrong, so the constraint is an equivalence.
            models.CheckConstraint(
                condition=(
                    models.Q(order_type=OrderType.LIMIT, limit_price__isnull=False)
                    | models.Q(order_type=OrderType.MARKET, limit_price__isnull=True)
                ),
                name="order_limit_price_iff_limit",
            ),
            models.CheckConstraint(
                condition=models.Q(limit_price__isnull=True) | models.Q(limit_price__gt=0),
                name="order_limit_price_positive",
            ),
            # Mirrors the entry constraint (ADR-0024): a key with nothing to compare against is the
            # shape that let a reused key replay a different request.
            models.CheckConstraint(
                condition=models.Q(idempotency_key__isnull=True)
                | models.Q(payload_fingerprint__isnull=False),
                name="keyed_order_has_a_fingerprint",
            ),
        ]
        indexes = [
            models.Index(fields=["user", "-created_at"], name="order_user_created_idx"),
            # Partial: the matching sweep only ever asks for open orders, and open orders are a
            # vanishing fraction of all orders once the system has run for a while.
            models.Index(
                fields=["instrument"],
                condition=models.Q(status=OrderStatus.OPEN),
                name="order_open_instrument_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.side} {self.quantity} {self.instrument_id} ({self.status})"

    def crosses(self, price: Decimal) -> bool:
        """Whether ``price`` is good enough for this resting order to fill.

        A buy fills at or below its limit, a sell at or above it — the limit is the *worst* price
        the client will accept, in the direction that matters for their side.
        """
        if self.limit_price is None:
            return True
        if self.side == OrderSide.BUY:
            return price <= self.limit_price
        return price >= self.limit_price
