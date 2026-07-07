"""The posting primitive and balance query — the only sanctioned write/read path for the ledger.

``post_entry`` is the single choke point through which every money movement flows: transfers
(Week 3), trade fills (Week 5), and fees all wrap it, inheriting its atomicity and the zero-sum
invariant. It validates first (fast, specific errors) and the deferred trigger enforces the same
invariant at COMMIT as defense in depth (ADR-0008, ADR-0009).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from django.db import transaction
from django.db.models import DecimalField, Sum, Value
from django.db.models.functions import Coalesce

from accounts.models import Account
from common.money import quantize_money

from .exceptions import InvalidEntryError, UnbalancedEntryError
from .models import JournalEntry, JournalLine

_ZERO = Decimal("0.0000")


@dataclass(frozen=True)
class LineSpec:
    """One requested leg of an entry. ``amount`` is signed and gets quantized by the service."""

    account: Account
    amount: Decimal


def post_entry(*, description: str, lines: Sequence[LineSpec]) -> JournalEntry:
    """Post a balanced journal entry atomically, or raise without persisting anything.

    Validation order (all before any write): at least two lines; every amount quantized to the
    money quantum; no zero amounts; a single uniform currency matching each account and equal to
    USD (ADR-0007); and the signed amounts summing to exactly zero. On success the entry and its
    lines are created inside one transaction so the deferred balance trigger checks a complete
    entry at COMMIT.
    """
    if len(lines) < 2:
        raise InvalidEntryError("A journal entry needs at least two lines to balance.")

    amounts = [quantize_money(line.amount) for line in lines]

    for amount in amounts:
        if amount == _ZERO:
            raise InvalidEntryError("Journal lines must have a non-zero amount.")

    for line in lines:
        if line.account.currency != "USD":
            raise InvalidEntryError(
                f"Account {line.account.id} is {line.account.currency}; only USD is supported."
            )

    total = sum(amounts, start=Decimal("0"))
    if total != _ZERO:
        raise UnbalancedEntryError(f"Entry lines must sum to zero; got {total}.")

    with transaction.atomic():
        entry = JournalEntry.objects.create(description=description)
        JournalLine.objects.bulk_create(
            [
                JournalLine(entry=entry, account=line.account, amount=amount, currency="USD")
                for line, amount in zip(lines, amounts, strict=True)
            ]
        )
    return entry


def get_balance(account: Account) -> Decimal:
    """Return an account's balance as the sum of its line amounts (derived, never stored).

    An account with no lines reads ``Decimal("0.0000")`` rather than ``None`` (ADR-0008).
    """
    result = account.lines.aggregate(
        balance=Coalesce(
            Sum("amount"),
            Value(_ZERO, output_field=DecimalField(max_digits=20, decimal_places=4)),
        )
    )
    return result["balance"]
