"""The money precision and rounding contract (ADR-0009).

Every monetary amount in the system is a ``Decimal`` stored at ``NUMERIC(20,4)``. Rounding
happens in exactly one place — :func:`quantize_money` — using banker's rounding so that summing
many rounded values does not accumulate bias. Nothing else should call ``.quantize()`` on money.
"""

from decimal import ROUND_HALF_EVEN, Decimal

# Storage precision for money: NUMERIC(20,4). Amounts are quantized to this before persistence.
MONEY_QUANTUM = Decimal("0.0001")

# Presentation precision: balances are shown to the cent even though they are stored to 4 places.
CENT = Decimal("0.01")

# Storage precision for share quantities: NUMERIC(20,8). Fractional shares (Week 5 brokerage).
SHARE_QUANTUM = Decimal("0.00000001")


def quantize_money(value: Decimal) -> Decimal:
    """Round ``value`` to the money quantum using banker's rounding (ADR-0009).

    This is the only sanctioned place money is rounded. Callers pass a ``Decimal`` and get back a
    ``Decimal`` snapped to ``NUMERIC(20,4)`` with ``ROUND_HALF_EVEN``.
    """
    return value.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)
