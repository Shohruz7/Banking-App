"""Domain errors raised by the posting service.

These are business-rule violations caught *before* the database, so callers get fast, specific
messages. The deferred constraint trigger (migration ``0002``) is the second line of defense for
the zero-sum rule and raises a database error instead — see ``ledger.services.post_entry``.
"""


class LedgerError(Exception):
    """Base class for all ledger domain errors."""


class InvalidEntryError(LedgerError):
    """The entry is structurally invalid (too few lines, zero amount, currency mismatch)."""


class UnbalancedEntryError(LedgerError):
    """The entry's signed line amounts do not sum to zero."""


class UnbalancedSharesError(LedgerError):
    """The entry's share quantities do not net to zero *per instrument* (ADR-0025).

    Per instrument, not overall: an entry that adds 100 shares of one instrument and removes 100 of
    another nets to zero and is still an invention. The unit matters.
    """


class InsufficientFundsError(LedgerError):
    """The source account's balance is less than the amount being moved out of it (ADR-0010)."""


class IdempotencyKeyConflictError(LedgerError):
    """The key has been used before, for a request that is not this one (ADR-0024).

    Distinct from a replay, which is the same request arriving twice and is not an error at all.
    The whole point of the fingerprint is that these two are now distinguishable.
    """
