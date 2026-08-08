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
from typing import cast
from uuid import UUID

from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.db.models import DecimalField, Sum, Value
from django.db.models.functions import Coalesce

from accounts.models import Account, AccountType
from audit.models import AuditAction
from audit.services import record_audit
from common.money import quantize_money, quantize_shares
from markets.models import Instrument
from realtime import events

from .exceptions import (
    IdempotencyKeyConflictError,
    InsufficientFundsError,
    InvalidEntryError,
    UnbalancedEntryError,
    UnbalancedSharesError,
)
from .fingerprints import transfer_fingerprint
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
    payload_fingerprint: str | None = None,
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

    ``payload_fingerprint`` is the digest of the request the key came from (ADR-0024), and a CHECK
    constraint requires one wherever a key is present. This service stores what it is given and
    compares nothing: deciding whether two requests are *the same* request needs the caller's
    payload, which by the time lines have been built is no longer recoverable — a sell's cost basis
    depends on the holding, so identical requests a minute apart produce different lines.
    """
    if idempotency_key is not None and payload_fingerprint is None:
        raise InvalidEntryError("An idempotency key requires a payload fingerprint (ADR-0024).")
    if len(lines) < 2:
        raise InvalidEntryError("A journal entry needs at least two lines to balance.")

    amounts = [quantize_money(line.amount) for line in lines]
    quantities = [
        None if line.quantity is None else quantize_shares(line.quantity) for line in lines
    ]

    # A line must move money, shares, or both — the service half of the CHECK on JournalLine
    # (ADR-0025). Loosened from "every amount is non-zero" to admit the shares-outstanding contra,
    # whose whole job is to move shares at no cost. A line that moves neither is still refused, and
    # that is the case worth refusing: it is a row that does nothing.
    for amount, quantity in zip(amounts, quantities, strict=True):
        if amount == _ZERO and quantity is None:
            raise InvalidEntryError("Journal lines must move a non-zero amount or a quantity.")

    for line in lines:
        if line.account.currency != "USD":
            raise InvalidEntryError(
                f"Account {line.account.id} is {line.account.currency}; only USD is supported."
            )

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

    # Shares are conserved per instrument (ADR-0025), the same way amounts are conserved overall.
    # The deferred trigger enforces this too; checking here is not redundant, it is what turns "the
    # commit blew up somewhere" into a domain error naming the instrument, raised from the frame
    # that built the lines. Every caller that omits a contra leg finds out here instead.
    moved: dict[UUID | None, Decimal] = {}
    for line, quantity in zip(lines, quantities, strict=True):
        if quantity is None:
            continue
        instrument_id = line.account.instrument_id
        moved[instrument_id] = moved.get(instrument_id, _ZERO_SHARES) + quantity
    for instrument_id, net in moved.items():
        if net != _ZERO_SHARES:
            raise UnbalancedSharesError(
                f"Entry does not conserve shares of instrument {instrument_id}: net {net}."
            )

    with transaction.atomic():
        entry = JournalEntry.objects.create(
            description=description,
            idempotency_key=idempotency_key,
            payload_fingerprint=payload_fingerprint,
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


#: The system user that owns every shares-outstanding contra account (ADR-0025). One row, created
#: by migration, `PROTECT`-ed and therefore undeletable — which is correct: the accounts hanging off
#: it are half of the ledger's share invariant.
SYSTEM_USERNAME = "system"


#: Name of an instrument's contra account. One per instrument, not one per holder.
def contra_account_name(symbol: str) -> str:
    return f"{symbol} shares outstanding"


def share_contra_for(instrument: "Instrument") -> Account:
    """The shares-outstanding account for an instrument, created on first use (ADR-0025).

    Lazily, exactly like ``position_account_for``: an instrument nobody has ever traded does not
    need one, and the partial unique index on ``(owner, instrument)`` settles the race if two first
    fills arrive together.
    """
    system_user, _ = User.objects.get_or_create(
        username=SYSTEM_USERNAME,
        defaults={"is_active": False, "email": ""},
    )
    account, _ = Account.objects.get_or_create(
        owner=system_user,
        instrument=instrument,
        account_type=AccountType.EQUITY,
        defaults={"name": contra_account_name(instrument.symbol)},
    )
    return account


def conserve_shares(lines: Sequence[LineSpec]) -> list[LineSpec]:
    """Append the contra legs that make an entry's share movements net to zero (ADR-0025).

    Every share that enters a holding comes from somewhere, and this is the "somewhere": a per
    instrument equity account standing in for the market. With it, ``SUM(quantity) GROUP BY
    instrument`` over an entry is zero, and Postgres can check that at COMMIT the way it already
    checks amounts.

    The contra leg carries ``amount=0``. That is not a placeholder — a share arriving in a holding
    has a cost, and the *cash* leg already paid it. A second amount here would double-count money.

    Quantities are quantized before being netted, matching what ``post_entry`` will store. Netting
    the raw values would leave a contra off by the rounding, and an entry that misses by 1e-12 of a
    share is refused exactly as firmly as one that misses by a hundred.

    Called by the fill builders rather than by ``post_entry``, deliberately. ``post_entry`` posting
    lines it was not handed — and doing a ``get_or_create`` account write from inside the ledger
    core — would break the "validate what you were given, invent nothing" contract that makes it
    reviewable. The cost of that choice is that a caller can forget; the per-instrument check in
    ``post_entry`` is what makes forgetting loud instead of silent.
    """
    moved: dict[UUID, Decimal] = {}
    for line in lines:
        # `positions()` semantics, asked of one row: a holding, not its contra. Wrapping an entry
        # that already has contra legs must not add a second set.
        if line.quantity is None or line.account.account_type != AccountType.ASSET:
            continue
        instrument_id = line.account.instrument_id
        if instrument_id is None:
            continue
        moved[instrument_id] = moved.get(instrument_id, _ZERO_SHARES) + quantize_shares(
            line.quantity
        )

    # One query for every instrument involved, rather than a lazy FK fetch per line.
    instruments = Instrument.objects.in_bulk(moved.keys())
    return [
        *lines,
        *(
            LineSpec(account=share_contra_for(instruments[pk]), amount=_ZERO, quantity=-net)
            for pk, net in moved.items()
            if net != _ZERO_SHARES
        ),
    ]


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

    digest = (
        None
        if idempotency_key is None
        else transfer_fingerprint(source_id=source.pk, destination_id=destination.pk, amount=amount)
    )

    # Cheap replay check: a settled retry needs no locks at all.
    if idempotency_key is not None:
        replayed = _replayed_entry(idempotency_key, digest)
        if replayed is not None:
            _audit_replay(replayed, actor, idempotency_key)
            return replayed, False

    try:
        with transaction.atomic():
            lock_accounts(source, destination)

            # Re-check under the lock: a concurrent first attempt may have committed since above.
            if idempotency_key is not None:
                replayed = _replayed_entry(idempotency_key, digest)
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
                payload_fingerprint=digest,
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
        #
        # This is the third and last place a key is resolved, and the one most easily forgotten:
        # it is only reachable under a race, so a missing fingerprint check here would pass every
        # single-threaded test. That is why the comparison lives inside _replayed_entry rather than
        # beside each call — the loser of the race gets the same 409 the sequential path gives.
        if idempotency_key is None:
            raise
        replayed = _replayed_entry(idempotency_key, digest)
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


def _replayed_entry(idempotency_key: str, payload_fingerprint: str | None) -> JournalEntry | None:
    """The entry this key already posted, or ``None`` — raising if the key belongs elsewhere.

    Resolving a key and deciding whether it *may* be replayed are one operation, in one function, on
    purpose (ADR-0024). ``transfer`` reaches this from three places — before the lock, under the
    lock, and in the ``IntegrityError`` handler — and a comparison written beside each call is a
    comparison that will eventually be missing from one of them. The one that would be missed is the
    race handler, which no sequential test reaches.

    The mismatch is a hard error rather than "post a second entry", because a key that already
    identifies a different movement cannot be honoured either way: replaying returns money that went
    somewhere else, and posting again defeats the point of the key.

    This is also the whole of the ownership check. ``JournalEntry`` has no owner column and
    ``idempotency_key`` is unique across the entire table, so two users choosing the same string
    collide. The digest contains both account ids, so a foreign key now fails to match rather than
    handing over a stranger's entry and its lines.
    """
    entry = JournalEntry.objects.filter(idempotency_key=idempotency_key).first()
    if entry is None:
        return None
    if entry.payload_fingerprint != payload_fingerprint:
        raise IdempotencyKeyConflictError(
            f"Idempotency key {idempotency_key!r} was already used for a different request."
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
    # `.aggregate()` is typed as returning Any, so without the cast `warn_return_any` is satisfied
    # by a function that could return anything at all — at the one place every balance in the system
    # is produced. Coalesce guarantees a Decimal; this is where that guarantee gets written down.
    return cast(Decimal, result["balance"])


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
    return cast(Decimal, result["quantity"])
