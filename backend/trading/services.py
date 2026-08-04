"""Order placement and execution — the sanctioned trading API (ADR-0016, ADR-0018).

The claim this module exists to make good on: **a trade fill is just another balanced posting.**
Every fill goes through ``ledger.services.post_entry``, so it inherits the zero-sum invariant, the
deferred trigger, atomicity and the audit trail without restating any of them.

Two shapes of entry come out of here:

*A buy* is two lines — cash out, position in at cost::

    cash          -1000.0000
    AAPL position +1000.0000   qty +6.50000000

*A sell* is three, and the third one is not optional. A position account's balance **is** its cost
basis, so a sell must remove cost basis rather than proceeds; the difference between what came in
and what left is the realized gain, and it needs a line of its own or the entry does not balance::

    cash           +480.0000
    AAPL position  -461.5500   qty -3.00000000
    Realized P&L    -18.4500

That falls out of the model rather than being bolted on, which is why realized P&L needs no stored
column: it is ``get_balance(realized_pnl_account)``, derived like every other balance here.

Two rounding edges are handled explicitly below, both of which are silent corruption if missed —
a break-even sell, and a full exit.
"""

from decimal import Decimal

from django.contrib.auth.models import User
from django.db import transaction
from django.utils import timezone

from accounts.models import Account, AccountType
from audit.models import AuditAction
from audit.services import record_audit
from common.money import quantize_money, quantize_shares
from ledger.exceptions import InsufficientFundsError
from ledger.services import LineSpec, get_balance, get_quantity, lock_accounts, post_entry
from markets.models import Instrument
from realtime import events

from .exceptions import (
    InstrumentInactiveError,
    InsufficientSharesError,
    InvalidOrderError,
    OrderNotOpenError,
)
from .models import Order, OrderSide, OrderStatus, OrderType

_ZERO = Decimal("0.0000")
_ZERO_SHARES = Decimal("0E-8")

#: Domain failures meaning "this order cannot fill as written", and the reason recorded on it.
#: Anything else escaping a fill is a bug and should surface as a 500 rather than be filed as a
#: business rejection. Shared with ``trading.tasks`` so an order rejected by the matching sweep and
#: one rejected at placement are recorded identically.
REJECTABLE = (InsufficientFundsError, InsufficientSharesError, InvalidOrderError)

REJECT_REASONS: dict[type[Exception], str] = {
    InsufficientFundsError: "insufficient_funds",
    InsufficientSharesError: "insufficient_shares",
    InvalidOrderError: "invalid_order",
}

#: The name of every user's realized-P&L account. Matched by the partial unique constraint on
#: income accounts, which is what makes the lazy get_or_create below safe under concurrency.
REALIZED_PNL_ACCOUNT_NAME = "Realized P&L"


def position_account_for(user: User, instrument: Instrument) -> Account:
    """The user's position account for an instrument, created on first use (ADR-0016).

    Created lazily rather than up front for all 55 instruments: a user who never trades should not
    carry 55 empty accounts, and the unique constraint on ``(owner, instrument)`` settles the race
    if two first buys arrive at once.
    """
    account, _ = Account.objects.get_or_create(
        owner=user,
        instrument=instrument,
        defaults={
            "name": f"{instrument.symbol} position",
            "account_type": AccountType.ASSET,
        },
    )
    return account


def realized_pnl_account_for(user: User) -> Account:
    """The user's realized-P&L account, created on the first sell that needs one."""
    account, _ = Account.objects.get_or_create(
        owner=user,
        name=REALIZED_PNL_ACCOUNT_NAME,
        account_type=AccountType.INCOME,
        defaults={"instrument": None},
    )
    return account


def place_order(
    *,
    user: User,
    instrument: Instrument,
    cash_account: Account,
    side: str,
    order_type: str,
    quantity: Decimal,
    limit_price: Decimal | None = None,
    idempotency_key: str | None = None,
) -> Order:
    """Validate and record an order, filling it immediately if it is a market order.

    Returns the ``Order`` in its resolved state: ``filled`` for a market order, ``open`` for a limit
    order that is now waiting on a price. Raises the domain errors in ``trading.exceptions``; the
    view maps them onto the ADR-0006 envelope and records the rejection audit row, because a row
    written here would be rolled back by the very exception it records (ADR-0014).
    """
    if not instrument.is_active:
        raise InstrumentInactiveError(f"{instrument.symbol} is no longer tradeable.")
    if cash_account.owner_id != user.pk or cash_account.is_position:
        raise InvalidOrderError("The cash account must be a money account you own.")

    quantity = quantize_shares(quantity)
    if quantity <= _ZERO_SHARES:
        raise InvalidOrderError(f"Order quantity must be positive; got {quantity}.")

    if order_type == OrderType.LIMIT:
        if limit_price is None:
            raise InvalidOrderError("A limit order needs a limit price.")
        limit_price = quantize_money(limit_price)
        if limit_price <= _ZERO:
            raise InvalidOrderError(f"Limit price must be positive; got {limit_price}.")
    else:
        # A market order carries no price by construction — the DB constraint says so too.
        limit_price = None

    # Cheap replay check, before any lock — the same first move transfer() makes (ADR-0010).
    if idempotency_key is not None:
        replayed = Order.objects.filter(idempotency_key=idempotency_key).first()
        if replayed is not None:
            return replayed

    order = Order.objects.create(
        user=user,
        instrument=instrument,
        cash_account=cash_account,
        side=side,
        order_type=order_type,
        quantity=quantity,
        limit_price=limit_price,
        idempotency_key=idempotency_key,
    )
    record_audit(
        action=AuditAction.ORDER_PLACED,
        actor=user,
        target_type="order",
        target_id=str(order.pk),
        context={
            "symbol": instrument.symbol,
            "side": side,
            "order_type": order_type,
            "quantity": quantity,
            "limit_price": limit_price,
        },
    )

    if order_type == OrderType.MARKET:
        try:
            return execute_fill(order, instrument.current_price)
        except REJECTABLE as exc:
            # By the time this runs, ``execute_fill``'s atomic block has exited and rolled back —
            # so this write is outside the failed transaction and survives, which is exactly the
            # placement ADR-0014 requires for a rejection. The exception still propagates; the
            # caller's job is to turn it into HTTP, not to decide what gets recorded.
            reject_order(order, reason=REJECT_REASONS[type(exc)])
            raise

    record_audit(
        action=AuditAction.ORDER_RESTED,
        actor=user,
        target_type="order",
        target_id=str(order.pk),
        context={"symbol": instrument.symbol, "side": side, "limit_price": limit_price},
    )
    return order


def execute_fill(order: Order, price: Decimal) -> Order:
    """Fill ``order`` at ``price``, posting the money movement through the ledger.

    Everything happens under one transaction with both accounts row-locked in the global ascending
    order (ADR-0010): the affordability check reads a balance *under* the lock it depends on, so a
    concurrent trade or transfer cannot race it into an overdraft or a short position. The audit row
    is written inside that transaction deliberately — if the posting rolls back, the claim that it
    happened rolls back with it (ADR-0014).
    """
    price = quantize_money(price)
    position = position_account_for(order.user, order.instrument)

    with transaction.atomic():
        lock_accounts(order.cash_account, position)

        quantity = order.quantity
        notional = quantize_money(price * quantity)
        if notional <= _ZERO:
            # A 4-decimal ledger cannot represent this movement at all. Rejecting is the honest
            # answer; clamping to the quantum would invent money the client did not ask to move.
            raise InvalidOrderError(
                f"Order is too small to post: {quantity} @ {price} rounds to zero."
            )

        if order.side == OrderSide.BUY:
            lines = _buy_lines(order, position, notional, quantity)
        else:
            lines = _sell_lines(order, position, notional, quantity)

        entry = post_entry(
            description=f"{order.get_side_display()} {quantity} {order.instrument.symbol}",
            lines=lines,
            # Keyed on the order, so a retried fill collides with the unique index instead of
            # posting the trade twice (ADR-0010).
            idempotency_key=f"order:{order.pk}",
        )

        order.status = OrderStatus.FILLED
        order.filled_quantity = quantity
        order.filled_price = price
        order.entry = entry
        order.resolved_at = timezone.now()
        order.save(
            update_fields=["status", "filled_quantity", "filled_price", "entry", "resolved_at"]
        )

        record_audit(
            action=AuditAction.ORDER_FILLED,
            actor=order.user,
            target_type="order",
            target_id=str(order.pk),
            context={
                "symbol": order.instrument.symbol,
                "side": order.side,
                "quantity": quantity,
                "fill_price": price,
                "notional": notional,
                "entry": str(entry.pk),
            },
        )

        # Inside the transaction, delivered after it commits (ADR-0023). The whole point of a live
        # fill notification is that it is true — an order that rolls back must not produce one, and
        # the cash balance read here is the one the entry above just created.
        events.publish_order(
            order.user_id,
            "order.filled",
            {
                "order_id": str(order.pk),
                "symbol": order.instrument.symbol,
                "side": order.side,
                "quantity": quantity,
                "price": price,
                "notional": notional,
                "entry_id": str(entry.pk),
            },
        )
        events.publish_balance(
            order.user_id, order.cash_account_id, get_balance(order.cash_account)
        )

    return order


def _buy_lines(
    order: Order, position: Account, notional: Decimal, quantity: Decimal
) -> list[LineSpec]:
    """Cash out, position in at cost. Two lines, and the cost basis is what was actually paid."""
    balance = get_balance(order.cash_account)
    if balance < notional:
        raise InsufficientFundsError(
            f"Account {order.cash_account.pk} has {balance}; cannot spend {notional}."
        )
    return [
        LineSpec(account=order.cash_account, amount=-notional),
        LineSpec(account=position, amount=notional, quantity=quantity),
    ]


def _sell_lines(
    order: Order, position: Account, notional: Decimal, quantity: Decimal
) -> list[LineSpec]:
    """Cash in, position out at *cost basis*, and the residual to realized P&L.

    Two edges, both silent if mishandled:

    **The full exit.** When the whole holding is sold, the cost removed is the account's exact
    remaining balance, not ``average_cost × quantity``. Recomputing would leave ten-thousandths of
    a dollar of basis behind in an account holding zero shares — dust that never clears, and that
    reads as a tiny permanent position on every statement thereafter.

    **The break-even sell.** When proceeds equal cost, the realized-P&L line would be zero, and
    ``post_entry`` rejects zero amounts. There is no gain to record, so the line is simply omitted
    and the entry is a balanced two-liner.
    """
    held = get_quantity(position)
    if held < quantity:
        raise InsufficientSharesError(
            f"You hold {held} {order.instrument.symbol}; cannot sell {quantity}."
        )

    basis = get_balance(position)
    # A partial exit takes basis × qty ÷ held in one expression: deriving an average cost first and
    # multiplying would round twice, and the second rounding has nothing left to correct it.
    cost = basis if quantity == held else quantize_money(basis * quantity / held)

    if cost <= _ZERO:
        raise InvalidOrderError(
            f"Order is too small to post: the cost basis of {quantity} "
            f"{order.instrument.symbol} rounds to zero."
        )

    lines = [
        LineSpec(account=order.cash_account, amount=notional),
        LineSpec(account=position, amount=-cost, quantity=-quantity),
    ]

    gain = notional - cost
    if gain != _ZERO:
        lines.append(LineSpec(account=realized_pnl_account_for(order.user), amount=-gain))
    return lines


def cancel_order(order: Order, *, actor: User) -> Order:
    """Cancel a resting order. Only an open order can be cancelled.

    The status change is a conditional ``UPDATE`` rather than a read-then-write: the matching sweep
    may be filling this exact order right now, and letting the database decide who got there first
    is the same move the MFA timestep burn makes.
    """
    updated = Order.objects.filter(pk=order.pk, status=OrderStatus.OPEN).update(
        status=OrderStatus.CANCELLED, resolved_at=timezone.now()
    )
    if not updated:
        raise OrderNotOpenError(f"Order {order.pk} is not open.")

    order.refresh_from_db()
    record_audit(
        action=AuditAction.ORDER_CANCELLED,
        actor=actor,
        target_type="order",
        target_id=str(order.pk),
        context={"symbol": order.instrument.symbol, "side": order.side},
    )
    events.publish_order(
        order.user_id,
        "order.cancelled",
        {"order_id": str(order.pk), "symbol": order.instrument.symbol, "side": order.side},
    )
    return order


def reject_order(order: Order, *, reason: str) -> Order:
    """Mark an order rejected and record why.

    Called after a failed fill has unwound, never inside the transaction that failed — the audit
    row and the status would both be rolled back by the exception they describe (ADR-0014).
    """
    order.status = OrderStatus.REJECTED
    order.reject_reason = reason[:64]
    order.resolved_at = timezone.now()
    order.save(update_fields=["status", "reject_reason", "resolved_at"])

    record_audit(
        action=AuditAction.ORDER_REJECTED,
        actor=order.user,
        target_type="order",
        target_id=str(order.pk),
        context={
            "symbol": order.instrument.symbol,
            "side": order.side,
            "quantity": order.quantity,
            "reason": reason,
        },
    )
    # No balance event rides along, and that is the assertion the rollback test makes: nothing
    # moved, so there is no new balance to announce. Outside any transaction, ``on_commit`` fires
    # this immediately — which is why a rejection reaches the client at all.
    events.publish_order(
        order.user_id,
        "order.rejected",
        {
            "order_id": str(order.pk),
            "symbol": order.instrument.symbol,
            "side": order.side,
            "quantity": order.quantity,
            "reason": reason,
        },
    )
    return order
