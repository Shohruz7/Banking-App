"""The money contract: banker's rounding and no float drift (ADR-0009)."""

from decimal import Decimal

from common.money import MONEY_QUANTUM, quantize_money


def test_quantize_snaps_to_four_places() -> None:
    assert quantize_money(Decimal("1.23456")) == Decimal("1.2346")
    assert quantize_money(Decimal("1")).as_tuple().exponent == MONEY_QUANTUM.as_tuple().exponent


def test_quantize_uses_banker_rounding_at_the_tie() -> None:
    # Exact halves at the quantum boundary round to the nearest *even* last digit.
    assert quantize_money(Decimal("0.00005")) == Decimal("0.0000")  # 0 is even
    assert quantize_money(Decimal("0.00015")) == Decimal("0.0002")  # rounds up to even 2
    assert quantize_money(Decimal("0.00025")) == Decimal("0.0002")  # stays at even 2
    assert quantize_money(Decimal("0.00035")) == Decimal("0.0004")  # rounds up to even 4


def test_quantize_is_sign_symmetric() -> None:
    assert quantize_money(Decimal("-0.00025")) == Decimal("-0.0002")


def test_decimal_sum_has_no_float_drift() -> None:
    # The classic float trap: 0.1 + 0.2 != 0.3 in binary floating point.
    assert Decimal("0.1") + Decimal("0.2") == Decimal("0.3")
    # Ten dime deposits sum to exactly one dollar, quantized.
    total = sum((Decimal("0.10") for _ in range(10)), start=Decimal("0"))
    assert quantize_money(total) == Decimal("1.0000")
