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
