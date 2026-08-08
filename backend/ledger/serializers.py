"""Ledger serializers — entry/line projections and the transfer request contract.

Every monetary field is a DRF ``DecimalField`` at the ADR-0009 precision, so money crosses the
wire as a string (``"25.0000"``) and never as a float.
"""

from decimal import Decimal

from rest_framework import serializers

from .models import JournalEntry, JournalLine


class JournalLineSerializer(serializers.ModelSerializer[JournalLine]):
    """One signed leg: ``amount`` is negative leaving the account, positive arriving.

    ``quantity`` is null on a money line and the signed share count on a line touching an
    instrument account. It was omitted until Week 7, which meant a fill's share movement was
    invisible in transaction history — harmless while position accounts were unreachable through
    the API, and a silent hole the moment one became reachable.
    """

    amount = serializers.DecimalField(max_digits=20, decimal_places=4, read_only=True)
    quantity = serializers.DecimalField(
        max_digits=20, decimal_places=8, read_only=True, allow_null=True
    )

    class Meta:
        model = JournalLine
        fields = ("id", "entry_id", "account_id", "amount", "quantity", "currency", "created_at")
        read_only_fields = fields


class JournalEntrySerializer(serializers.ModelSerializer[JournalEntry]):
    """An entry with its lines — the shape a posted transfer returns."""

    lines = JournalLineSerializer(many=True, read_only=True)

    class Meta:
        model = JournalEntry
        fields = ("id", "description", "idempotency_key", "created_at", "lines")
        read_only_fields = fields


class TransferCreateSerializer(serializers.Serializer[dict[str, object]]):
    """The transfer request body.

    Shape only — ownership, existence, and funds are the view's and the service's business.
    ``min_value`` rejects zero and negatives here so the common case fails as a per-field
    validation error rather than a domain exception.
    """

    source_account = serializers.UUIDField()
    destination_account = serializers.UUIDField()
    amount = serializers.DecimalField(max_digits=20, decimal_places=4, min_value=Decimal("0.0001"))
    description = serializers.CharField(max_length=255, required=False, default="Transfer")
    idempotency_key = serializers.CharField(
        max_length=64, required=False, allow_null=True, default=None
    )
