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

from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.db.models import DecimalField, Sum, Value
from django.db.models.functions import Coalesce

from accounts.models import Account
from audit.models import AuditAction
from audit.services import record_audit
from common.money import quantize_money, quantize_shares
from realtime import events

from .exceptions import InsufficientFundsError, InvalidEntryError, UnbalancedEntryError
from .models import JournalEntry, JournalLine

_ZERO = Decimal("0.0000")
_ZERO_SHARES = Decimal("0E-8")


@dataclass(frozen=True)
class LineSpec:
    """One requested leg of an entry. ``amount`` is signed and gets quantized by the service.

    ``quantity`` is the share count for a line touching a position account, signed the same way as
    ``amount`` — required there, and forbidden anywhere else (ADR-0016).
    """

    account: Account
    amount: Decimal
    quantity: Decimal | None = None


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

    Lines touching a position account must carry a ``quantity`` and lines touching a cash account
    must not (ADR-0016) — checked here, because a position account whose share count and cost basis
    disagree is exactly the drift this design exists to prevent.

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

    quantities = [
        None if line.quantity is None else quantize_shares(line.quantity) for line in lines
    ]

    for line, quantity in zip(lines, quantities, strict=True):
        if line.account.is_position and quantity is None:
            raise InvalidEntryError(
                f"Account {line.account.id} holds {line.account.instrument_id}; "
                f"a line touching it must carry a share quantity."
            )
        if not line.account.is_position and quantity is not None:
            raise InvalidEntryError(
                f"Account {line.account.id} is a cash account; a line touching it cannot carry a "
                f"share quantity."
            )
        if quantity == _ZERO_SHARES:
            raise InvalidEntryError("Journal lines must have a non-zero share quantity.")

    total = sum(amounts, start=Decimal("0"))
    if total != _ZERO:
        raise UnbalancedEntryError(f"Entry lines must sum to zero; got {total}.")

    with transaction.atomic():
        entry = JournalEntry.objects.create(
            description=description, idempotency_key=idempotency_key
        )
        JournalLine.objects.bulk_create(
            [
                JournalLine(
                    entry=entry,
                    account=line.account,
                    amount=amount,
                    quantity=quantity,
                    currency="USD",
                )
                for line, amount, quantity in zip(lines, amounts, quantities, strict=True)
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
    actor: User | None = None,
) -> tuple[JournalEntry, bool]:
    """Move ``amount`` from ``source`` to ``destination`` as one balanced two-line entry.

    Returns ``(entry, created)``. ``created`` is ``False`` when an idempotency-key replay returned
    the entry a previous call posted — the money moved once, on that first call.

    Raises ``InvalidEntryError`` for a non-positive amount or a self-transfer, and
    ``InsufficientFundsError`` when the source cannot cover the amount (ADR-0010: no overdraft;
    transferring the balance exactly to zero is allowed).

    ``actor`` is *who moved the money* — a domain fact about the transfer, and the one HTTP-adjacent
    thing this signature admits (ADR-0014). The audit row's ip, user agent and request id ride the
    ambient context instead, because those are transport facts the ledger has no business knowing.
    The successful-posting audit row is written **inside** the transaction deliberately: if the
    transaction aborts, the row vanishes with the entry, so the log cannot claim a movement that
    never committed. A *rejected* transfer is audited by the caller, after the failed transaction
    has unwound — a write attempted here would be rolled back by the very exception it records.
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
            _audit_replay(replayed, actor, idempotency_key)
            return replayed, False

    try:
        with transaction.atomic():
            lock_accounts(source, destination)

            # Re-check under the lock: a concurrent first attempt may have committed since above.
            if idempotency_key is not None:
                replayed = _entry_for_key(idempotency_key)
                if replayed is not None:
                    _audit_replay(replayed, actor, idempotency_key)
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
            record_audit(
                action=AuditAction.TRANSFER_POSTED,
                actor=actor,
                target_type="journal_entry",
                target_id=str(entry.pk),
                context={
                    "amount": amount,
                    "source_account": str(source.pk),
                    "destination_account": str(destination.pk),
                    "idempotency_key": idempotency_key,
                },
            )
            _announce_transfer(entry, source, destination, amount, description)
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
        _audit_replay(replayed, actor, idempotency_key)
        return replayed, False


def _announce_transfer(
    entry: JournalEntry,
    source: Account,
    destination: Account,
    amount: Decimal,
    description: str,
) -> None:
    """Push the posting and both new balances to whoever is watching (ADR-0023).

    Called from inside the transaction, which is exactly right: ``realtime.events`` defers every
    send to ``on_commit``, so a transfer that ends up rolling back — an overdraft caught under the
    lock, a deadlock, a crash between here and COMMIT — announces nothing. The balances are read
    here rather than in the callback because *here* is where they are consistent.

    Both sides are told. A transfer to another user is that user's money arriving, and they are
    entitled to see it land without refreshing.
    """
    for account in (source, destination):
        events.publish_balance(account.owner_id, account.pk, get_balance(account))
    for owner_id in {source.owner_id, destination.owner_id}:
        events.publish_transfer(owner_id, entry_id=entry.pk, amount=amount, description=description)


def _audit_replay(entry: JournalEntry, actor: User | None, idempotency_key: str | None) -> None:
    """Record that a retry returned an already-posted entry rather than moving money again.

    Worth a row of its own: "this request arrived twice" is a fact about client behaviour, and
    without it a replay is indistinguishable in the log from a request that never happened.
    """
    record_audit(
        action=AuditAction.TRANSFER_REPLAYED,
        actor=actor,
        target_type="journal_entry",
        target_id=str(entry.pk),
        context={"idempotency_key": idempotency_key},
    )


def lock_accounts(*accounts: Account) -> None:
    """Take row locks on every given account in ascending UUID order (ADR-0010, ADR-0016).

    A fixed global acquisition order is what makes deadlock structurally impossible: A→B and B→A
    running at once request the same two locks in the same sequence, so one waits instead of both
    holding half of what the other needs. Locks are taken with sequential ``get()`` calls because
    a single ``filter(pk__in=...)`` would lock rows in plan order, which this code cannot pin.

    The ordering is only worth anything if it is *global*, so this is public and every writer that
    needs the no-overdraft or no-short-sell guarantee calls it — transfers touch two accounts, a
    trade fill touches two or three. Duplicates are collapsed: locking the same row twice in one
    transaction is harmless but pointless, and a caller shouldn't have to think about it.
    """
    for pk in sorted({account.pk for account in accounts}):
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


def get_quantity(account: Account) -> Decimal:
    """Return a position account's share count as the sum of its line quantities (ADR-0016).

    Derived for the same reason balances are: a stored share count is a second source of truth that
    can drift from the postings that produced it. A cash account reads ``0E-8`` — its lines all
    carry a NULL quantity, and SUM ignores NULLs.
    """
    result = account.lines.aggregate(
        quantity=Coalesce(
            Sum("quantity"),
            Value(_ZERO_SHARES, output_field=DecimalField(max_digits=20, decimal_places=8)),
        )
    )
    return result["quantity"]
