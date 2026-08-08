"""Domain errors for order placement and execution.

Cash shortfalls deliberately reuse ``ledger.exceptions.InsufficientFundsError``: "you cannot spend
money you do not have" is one rule with one meaning, whether the spender is a transfer or a trade.
"""


class TradingError(Exception):
    """Base class for every trading domain error."""


class InsufficientSharesError(TradingError):
    """The seller does not hold enough of the instrument. The no-short-selling rule (ADR-0018)."""


class InvalidOrderError(TradingError):
    """The order is malformed, unaffordably small, or references something it may not."""


class InstrumentInactiveError(TradingError):
    """The instrument is delisted; it can still be held and read, but not traded."""


class OrderNotOpenError(TradingError):
    """The order has already been filled, cancelled or rejected — there is nothing left to do."""


class OrderKeyConflictError(TradingError):
    """The idempotency key belongs to a different order request (ADR-0024)."""
