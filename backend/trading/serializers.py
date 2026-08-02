"""Order and holdings serializers.

Quantities are ``NUMERIC(20,8)`` and money ``NUMERIC(20,4)`` (ADR-0009, ADR-0016); both cross the
wire as strings so a JavaScript client cannot silently round a fractional share to nothing.
"""

from decimal import Decimal

from rest_framework import serializers

from .models import Order, OrderSide, OrderType


class OrderSerializer(serializers.ModelSerializer[Order]):
    """An order and its outcome.

    ``entry_id`` is the posting it produced, or null if it never filled.
    """

    symbol = serializers.CharField(source="instrument.symbol", read_only=True)
    quantity = serializers.DecimalField(max_digits=20, decimal_places=8, read_only=True)
    filled_quantity = serializers.DecimalField(max_digits=20, decimal_places=8, read_only=True)
    limit_price = serializers.DecimalField(max_digits=20, decimal_places=4, read_only=True)
    filled_price = serializers.DecimalField(max_digits=20, decimal_places=4, read_only=True)

    class Meta:
        model = Order
        fields = (
            "id",
            "symbol",
            "side",
            "order_type",
            "status",
            "quantity",
            "limit_price",
            "filled_quantity",
            "filled_price",
            "entry_id",
            "idempotency_key",
            "reject_reason",
            "created_at",
            "resolved_at",
        )
        read_only_fields = fields


class OrderCreateSerializer(serializers.Serializer[dict[str, object]]):
    """The order request body.

    Shape only — whether the instrument is tradeable, whether the account is yours, and whether you
    can afford it are the view's and the service's business, exactly as with transfers.
    """

    symbol = serializers.CharField(max_length=10)
    cash_account = serializers.UUIDField()
    side = serializers.ChoiceField(choices=OrderSide.choices)
    order_type = serializers.ChoiceField(choices=OrderType.choices)
    # min_value at the share quantum: a quantity that rounds to zero is rejected here as a per-field
    # validation error rather than surfacing later as a domain exception.
    quantity = serializers.DecimalField(
        max_digits=20, decimal_places=8, min_value=Decimal("0.00000001")
    )
    limit_price = serializers.DecimalField(
        max_digits=20,
        decimal_places=4,
        required=False,
        allow_null=True,
        default=None,
        min_value=Decimal("0.0001"),
    )
    idempotency_key = serializers.CharField(
        max_length=64, required=False, allow_null=True, default=None
    )

    def validate(self, attrs: dict[str, object]) -> dict[str, object]:
        """Reject the two combinations the database constraint would reject anyway.

        Caught here so the client gets a field-level 400 rather than a 500 from an IntegrityError —
        the constraint stays as the thing that makes it *true*.
        """
        if attrs["order_type"] == OrderType.LIMIT and attrs["limit_price"] is None:
            raise serializers.ValidationError({"limit_price": "A limit order needs a limit price."})
        if attrs["order_type"] == OrderType.MARKET and attrs["limit_price"] is not None:
            raise serializers.ValidationError(
                {"limit_price": "A market order fills at the current price and takes no limit."}
            )
        return attrs


class HoldingSerializer(serializers.Serializer[dict[str, object]]):
    """One position: how much is held, what it cost, and what it is worth now.

    Every field is derived — quantity and cost basis are sums over the ledger, average cost and
    market value are arithmetic on top (ADR-0016). Nothing here is stored, so nothing can drift.
    """

    symbol = serializers.CharField(read_only=True)
    name = serializers.CharField(read_only=True)
    account_id = serializers.UUIDField(read_only=True)
    quantity = serializers.DecimalField(max_digits=20, decimal_places=8, read_only=True)
    cost_basis = serializers.DecimalField(max_digits=20, decimal_places=4, read_only=True)
    average_cost = serializers.DecimalField(max_digits=20, decimal_places=4, read_only=True)
    last_price = serializers.DecimalField(max_digits=20, decimal_places=4, read_only=True)
    market_value = serializers.DecimalField(max_digits=20, decimal_places=4, read_only=True)
