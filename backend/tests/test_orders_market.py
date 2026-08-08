"""Market orders: the claim that a trade fill is just another balanced posting (ADR-0016).

The sell path carries this week's real difficulty. A position account's balance *is* its cost
basis, so a sell removes cost — not proceeds — and the residual is realized P&L on a third line.
Two rounding edges around that are silent corruption if mishandled, and each gets a test that fails
loudly against the naive implementation: ``test_break_even_sell_omits_the_pnl_line`` and
``test_closing_a_position_leaves_exactly_zero_basis``.
"""

from decimal import Decimal

import pytest
from django.contrib.auth.models import User

from accounts.models import Account
from ledger.exceptions import InsufficientFundsError
from ledger.models import JournalEntry
from ledger.services import get_balance, get_quantity
from markets.models import Instrument
from tests.factories import InstrumentFactory, give_shares
from trading.exceptions import InsufficientSharesError, InvalidOrderError
from trading.models import OrderSide, OrderStatus, OrderType
from trading.services import place_order, position_account_for, realized_pnl_account_for

pytestmark = pytest.mark.django_db


def buy(
    user: User,
    instrument: Instrument,
    cash: Account,
    quantity: str,
    **kwargs: object,
) -> object:
    return place_order(
        user=user,
        instrument=instrument,
        cash_account=cash,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal(quantity),
        **kwargs,  # type: ignore[arg-type]
    )


def sell(user: User, instrument: Instrument, cash: Account, quantity: str) -> object:
    return place_order(
        user=user,
        instrument=instrument,
        cash_account=cash,
        side=OrderSide.SELL,
        order_type=OrderType.MARKET,
        quantity=Decimal(quantity),
    )


def test_market_buy_posts_a_balanced_three_line_entry(
    password_user: User, instrument: Instrument, funded_cash_account: Account
) -> None:
    """The week's core claim: a fill is a journal entry, subject to every ledger guarantee."""
    order = buy(password_user, instrument, funded_cash_account, "2")

    assert order.status == OrderStatus.FILLED  # type: ignore[attr-defined]
    entry = order.entry  # type: ignore[attr-defined]
    assert entry is not None

    lines = list(entry.lines.all())
    # Three since ADR-0025: cash out, position in, and the shares out of the market. The contra
    # leg moves no money, so the amounts still sum to zero exactly as before.
    assert len(lines) == 3
    assert sum(line.amount for line in lines) == Decimal("0.0000")
    assert sum(line.quantity or Decimal("0") for line in lines) == Decimal("0E-8")
    # Keyed on the order, so a retried fill hits the unique index instead of posting twice.
    assert entry.idempotency_key == f"order:{order.pk}"  # type: ignore[attr-defined]


def test_buy_debits_cash_and_credits_the_position_account(
    password_user: User, instrument: Instrument, funded_cash_account: Account
) -> None:
    """Cash down by the notional, position up by the same, and the share count on the same row."""
    buy(password_user, instrument, funded_cash_account, "2.5")

    position = position_account_for(password_user, instrument)
    # 2.5 shares at the $100 seed price.
    assert get_balance(funded_cash_account) == Decimal("9750.0000")
    assert get_balance(position) == Decimal("250.0000")
    assert get_quantity(position) == Decimal("2.50000000")

    # The cash leg carries no quantity, and the position leg does — the pairing rule in post_entry.
    cash_line = funded_cash_account.lines.exclude(amount__gt=0).first()
    assert cash_line is not None
    assert cash_line.quantity is None


def test_buy_creates_the_position_account_once(
    password_user: User, instrument: Instrument, funded_cash_account: Account
) -> None:
    """The unique constraint on (owner, instrument) means a second buy reuses the first account."""
    buy(password_user, instrument, funded_cash_account, "1")
    buy(password_user, instrument, funded_cash_account, "1")

    positions = Account.objects.filter(owner=password_user, instrument=instrument)
    assert positions.count() == 1
    assert get_quantity(positions.get()) == Decimal("2.00000000")


def test_buy_rejected_when_cash_is_insufficient(
    password_user: User, instrument: Instrument, funded_cash_account: Account
) -> None:
    """The overdraft rule, unchanged: 10k of cash cannot buy 200 shares at $100."""
    with pytest.raises(InsufficientFundsError):
        buy(password_user, instrument, funded_cash_account, "200")

    assert JournalEntry.objects.count() == 1  # only the opening balance
    assert get_balance(funded_cash_account) == Decimal("10000.0000")


def test_sell_posts_four_lines_with_realized_pnl(
    password_user: User, instrument: Instrument, funded_cash_account: Account
) -> None:
    """Proceeds in, cost basis out, and the residual to realized P&L — summing to zero."""
    give_shares(password_user, instrument, Decimal("10"), Decimal("1000.00"))  # avg cost $100
    instrument.last_price = Decimal("160.0000")
    instrument.save(update_fields=["last_price"])

    order = sell(password_user, instrument, funded_cash_account, "3")
    lines = list(order.entry.lines.all())  # type: ignore[attr-defined]

    # Four since ADR-0025: proceeds, basis out, realized P&L, and the shares back to the market.
    assert len(lines) == 4
    assert sum(line.amount for line in lines) == Decimal("0.0000")
    assert sum(line.quantity or Decimal("0") for line in lines) == Decimal("0E-8")

    position = position_account_for(password_user, instrument)
    pnl = realized_pnl_account_for(password_user)

    assert get_balance(funded_cash_account) == Decimal("10480.0000")  # +3 × $160 proceeds
    assert get_balance(position) == Decimal("700.0000")  # $1000 − $300 of basis
    assert get_quantity(position) == Decimal("7.00000000")
    # Realized gain of $180 = $480 proceeds − $300 basis. Positive P&L reads as a credit balance,
    # which is why the ledger line is negative: income accounts fund the cash they produce.
    assert get_balance(pnl) == Decimal("-180.0000")


def test_break_even_sell_omits_the_pnl_line(
    password_user: User, instrument: Instrument, funded_cash_account: Account
) -> None:
    """Selling at exactly cost has no residual, and post_entry rejects a zero-amount line.

    The naive implementation appends the P&L line unconditionally and raises InvalidEntryError
    here, turning an ordinary trade into a 500.
    """
    give_shares(password_user, instrument, Decimal("10"), Decimal("1000.00"))  # avg cost $100
    instrument.last_price = Decimal("100.0000")
    instrument.save(update_fields=["last_price"])

    order = sell(password_user, instrument, funded_cash_account, "3")
    lines = list(order.entry.lines.all())  # type: ignore[attr-defined]

    # Three, not four: the P&L line is the one omitted, never the share contra.
    assert len(lines) == 3
    assert sum(line.amount for line in lines) == Decimal("0.0000")
    # No gain, so no realized-P&L account was ever needed.
    assert not Account.objects.filter(owner=password_user, name="Realized P&L").exists()


def test_closing_a_position_leaves_exactly_zero_basis(
    password_user: User, instrument: Instrument, funded_cash_account: Account
) -> None:
    """A full exit removes the account's exact balance, not average_cost × quantity.

    3 shares that cost $1000 have an average cost of $333.3333 once quantized. Multiplying that
    back out gives $999.9999 and strands a hundredth of a cent of basis in an account holding zero
    shares — dust that never clears and shows up as a phantom position forever after.
    """
    give_shares(password_user, instrument, Decimal("3"), Decimal("1000.00"))
    instrument.last_price = Decimal("500.0000")
    instrument.save(update_fields=["last_price"])

    sell(password_user, instrument, funded_cash_account, "3")

    position = position_account_for(password_user, instrument)
    assert get_quantity(position) == Decimal("0E-8")
    assert get_balance(position) == Decimal("0.0000"), "cost-basis dust left in a closed position"


def test_sell_rejected_without_enough_shares(
    password_user: User, instrument: Instrument, funded_cash_account: Account
) -> None:
    """No short selling — structurally the overdraft check, on the quantity side."""
    give_shares(password_user, instrument, Decimal("2"), Decimal("200.00"))

    with pytest.raises(InsufficientSharesError):
        sell(password_user, instrument, funded_cash_account, "5")

    position = position_account_for(password_user, instrument)
    assert get_quantity(position) == Decimal("2.00000000")


def test_selling_an_instrument_never_held_is_rejected(
    password_user: User, instrument: Instrument, funded_cash_account: Account
) -> None:
    """The empty-position case: zero held is still "not enough", not a crash on a missing row."""
    with pytest.raises(InsufficientSharesError):
        sell(password_user, instrument, funded_cash_account, "1")


def test_average_cost_is_recomputed_across_lots(
    password_user: User, instrument: Instrument, funded_cash_account: Account
) -> None:
    """Two lots at different prices blend into one average — the basis method is average cost."""
    buy(password_user, instrument, funded_cash_account, "10")  # 10 @ $100 = $1000
    instrument.last_price = Decimal("200.0000")
    instrument.save(update_fields=["last_price"])
    buy(password_user, instrument, funded_cash_account, "10")  # 10 @ $200 = $2000

    position = position_account_for(password_user, instrument)
    assert get_quantity(position) == Decimal("20.00000000")
    assert get_balance(position) == Decimal("3000.0000")
    # $3000 over 20 shares — the blended average, not the price of either lot.
    assert get_balance(position) / get_quantity(position) == Decimal("150")


def test_fractional_quantity_survives_a_round_trip(
    password_user: User, funded_cash_account: Account
) -> None:
    """One satoshi-scale share in, the same out. The whole point of NUMERIC(20,8)."""
    pricey = InstrumentFactory.create(symbol="EXPY", initial_price=Decimal("20000.0000"))

    buy(password_user, pricey, funded_cash_account, "0.00000001")

    position = position_account_for(password_user, pricey)
    assert get_quantity(position) == Decimal("0.00000001")
    # 0.00000001 × $20,000 = $0.0002 — representable, but only just.
    assert get_balance(position) == Decimal("0.0002")


def test_notional_is_quantized_once_at_the_money_precision(
    password_user: User, instrument: Instrument, funded_cash_account: Account
) -> None:
    """price × quantity is rounded exactly once, by quantize_money (ADR-0009)."""
    buy(password_user, instrument, funded_cash_account, "1.23456789")

    # 1.23456789 × $100 = $123.456789 → $123.4568 under banker's rounding.
    assert get_balance(funded_cash_account) == Decimal("10000.0000") - Decimal("123.4568")
    assert get_balance(position_account_for(password_user, instrument)) == Decimal("123.4568")


def test_order_too_small_to_represent_is_rejected(
    password_user: User, funded_cash_account: Account
) -> None:
    """A notional that rounds to zero cannot be posted, and is refused rather than rounded up.

    Clamping to the money quantum would invent a movement the client never asked for; a 4-decimal
    ledger simply cannot express this trade.
    """
    penny = InstrumentFactory.create(symbol="PENY", initial_price=Decimal("0.0100"))

    with pytest.raises(InvalidOrderError):
        buy(password_user, penny, funded_cash_account, "0.00000001")


def test_order_idempotency_key_replays_instead_of_double_filling(
    password_user: User, instrument: Instrument, funded_cash_account: Account
) -> None:
    """The transfer pattern, applied to orders: one key, one fill, same order returned."""
    first = buy(password_user, instrument, funded_cash_account, "1", idempotency_key="order-key-1")
    second = buy(password_user, instrument, funded_cash_account, "1", idempotency_key="order-key-1")

    assert first.pk == second.pk  # type: ignore[attr-defined]
    assert get_quantity(position_account_for(password_user, instrument)) == Decimal("1.00000000")
    assert get_balance(funded_cash_account) == Decimal("9900.0000")


def test_a_foreign_cash_account_is_refused(
    password_user: User, instrument: Instrument, funded_cash_account: Account
) -> None:
    """The service checks ownership itself, so the rule holds for callers that skip the view."""
    from tests.factories import AccountFactory

    someone_else = AccountFactory.create()

    with pytest.raises(InvalidOrderError):
        place_order(
            user=password_user,
            instrument=instrument,
            cash_account=someone_else,
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=Decimal("1"),
        )


def test_a_position_account_cannot_be_used_as_the_cash_account(
    password_user: User, instrument: Instrument
) -> None:
    """Paying for shares with shares would post a quantity onto a leg that has no instrument."""
    give_shares(password_user, instrument, Decimal("5"), Decimal("500.00"))
    position = position_account_for(password_user, instrument)

    with pytest.raises(InvalidOrderError):
        place_order(
            user=password_user,
            instrument=instrument,
            cash_account=position,
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=Decimal("1"),
        )
