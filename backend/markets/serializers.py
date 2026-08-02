"""Instrument and price serializers.

Every price is a DRF ``DecimalField`` at the ADR-0009 precision, so it crosses the wire as the
string ``"153.8500"`` and never as a float that a JavaScript client would silently round.
"""

from rest_framework import serializers

from .models import Instrument, PriceTick


class InstrumentSerializer(serializers.ModelSerializer[Instrument]):
    """A tradeable symbol and its latest price.

    ``drift`` and ``volatility`` are deliberately absent: they are simulation parameters, and
    publishing them would tell a client exactly how the "market" it is trading against behaves.
    """

    last_price = serializers.DecimalField(max_digits=20, decimal_places=4, read_only=True)

    class Meta:
        model = Instrument
        fields = (
            "id",
            "symbol",
            "name",
            "sector",
            "currency",
            "is_active",
            "last_price",
            "last_tick_at",
        )
        read_only_fields = fields


class PriceTickSerializer(serializers.ModelSerializer[PriceTick]):
    """One point on a price series — the shape Week 7's chart consumes."""

    price = serializers.DecimalField(max_digits=20, decimal_places=4, read_only=True)

    class Meta:
        model = PriceTick
        fields = ("price", "created_at")
        read_only_fields = fields
