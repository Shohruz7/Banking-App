"""The posting primitive, transfers, and the balance query — the sanctioned ledger API.

``post_entry`` is the single choke point through which every money movement flows: transfers
(below), trade fills (Week 5), and fees all wrap it, inheriting its atomicity and the zero-sum
invariant. It validates first (fast, specific errors) and the deferred trigger enforces the same
invariant at COMMIT as defense in depth (ADR-0008, ADR-0009).

``transfer`` adds the protocol that concurrent money movement needs (ADR-0010): lock both account
rows in ascending UUID order so opposing transfers queue instead of deadlocking, read the source
balance *under that lock* so the overdraft check cannot be raced, and key the entry so a retried
request replays instead of posting twice. Any future writer that needs the no-overdraft guarantee
must follow the same lock-then-read protocol — the database enforces the zero-sum invariant, not
this one.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import DecimalField, Sum, Value
from django.db.models.functions import Coalesce

from accounts.models import Account
from common.money import quantize_money

from .exceptions import InsufficientFundsError, InvalidEntryError, UnbalancedEntryError
from .models import JournalEntry, JournalLine

_ZERO = Decimal("0.0000")


@dataclass(frozen=True)
class LineSpec:
    """One requested leg of an entry. ``amount`` is signed and gets quantized by the service."""

    account: Account
    amount: Decimal


def post_entry(
    *,
    description: str,
    lines: Sequence[LineSpec],
    idempotency_key: str | None = None,
) -> JournalEntry:
    """Post a balanced journal entry atomically, or raise without persisting anything.

    Validation order (all before any write): at least two lines; every amount quantized to the
    money quantum; no zero amounts; a single uniform currency matching each account and equal to
    USD (ADR-0007); and the signed amounts summing to exactly zero. On success the entry and its
    lines are created inside one transaction so the deferred balance trigger checks a complete
    entry at COMMIT.

    ``idempotency_key`` is stored as-is and is unique when present (ADR-0010); a duplicate raises
    ``IntegrityError`` from the database. Callers that want replay semantics should use
    :func:`transfer`, which handles that race.
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
        entry = JournalEntry.objects.create(
            description=description, idempotency_key=idempotency_key
        )
        JournalLine.objects.bulk_create(
            [
                JournalLine(entry=entry, account=line.account, amount=amount, currency="USD")
                for line, amount in zip(lines, amounts, strict=True)
            ]
        )
    return entry


def transfer(
    *,
    source: Account,
    destination: Account,
    amount: Decimal,
    description: str = "Transfer",
    idempotency_key: str | None = None,
) -> tuple[JournalEntry, bool]:
    """Move ``amount`` from ``source`` to ``destination`` as one balanced two-line entry.

    Returns ``(entry, created)``. ``created`` is ``False`` when an idempotency-key replay returned
    the entry a previous call posted — the money moved once, on that first call.

    Raises ``InvalidEntryError`` for a non-positive amount or a self-transfer, and
    ``InsufficientFundsError`` when the source cannot cover the amount (ADR-0010: no overdraft;
    transferring the balance exactly to zero is allowed).
    """
    amount = quantize_money(amount)
    if amount <= _ZERO:
        raise InvalidEntryError(f"Transfer amount must be positive; got {amount}.")
    if source.pk == destination.pk:
        raise InvalidEntryError("Source and destination must be different accounts.")

    # Cheap replay check: a settled retry needs no locks at all.
    if idempotency_key is not None:
        replayed = _entry_for_key(idempotency_key)
        if replayed is not None:
            return replayed, False

    try:
        with transaction.atomic():
            _lock_accounts(source, destination)

            # Re-check under the lock: a concurrent first attempt may have committed since above.
            if idempotency_key is not None:
                replayed = _entry_for_key(idempotency_key)
                if replayed is not None:
                    return replayed, False

            balance = get_balance(source)
            if balance < amount:
                raise InsufficientFundsError(
                    f"Account {source.pk} has {balance}; cannot transfer {amount}."
                )

            entry = post_entry(
                description=description,
                lines=[
                    LineSpec(account=source, amount=-amount),
                    LineSpec(account=destination, amount=amount),
                ],
                idempotency_key=idempotency_key,
            )
        return entry, True
    except IntegrityError:
        # Two first attempts with the same key raced past the checks above and the unique
        # constraint settled it. Recover *outside* the atomic block — inside a failed transaction
        # every query would raise TransactionManagementError.
        if idempotency_key is None:
            raise
        replayed = _entry_for_key(idempotency_key)
        if replayed is None:
            raise
        return replayed, False


def _lock_accounts(source: Account, destination: Account) -> None:
    """Take row locks on both accounts in ascending UUID order (ADR-0010).

    A fixed global acquisition order is what makes deadlock structurally impossible: A→B and B→A
    running at once request the same two locks in the same sequence, so one waits instead of both
    holding half of what the other needs. Locks are taken with sequential ``get()`` calls because
    a single ``filter(pk__in=...)`` would lock rows in plan order, which this code cannot pin.
    """
    for pk in sorted((source.pk, destination.pk)):
        Account.objects.select_for_update().get(pk=pk)


def _entry_for_key(idempotency_key: str) -> JournalEntry | None:
    return JournalEntry.objects.filter(idempotency_key=idempotency_key).first()


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
