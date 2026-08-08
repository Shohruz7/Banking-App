"""Canonical digests of the request a keyed write came from (ADR-0024).

An idempotency key on its own answers "have I seen this request before?" with the client's word for
it. ADR-0010 accepted that in v1: same key ⇒ same entry returned, *payload not compared*. Two things
turned out to be wrong with that, and this module exists to fix both.

**A replay must be the same request.** A key reused with a different amount silently returned the
original entry, so a client that recycled a key across two genuinely different transfers moved money
once and was told it had moved twice. Storing a digest of the request alongside the key turns that
into a 409.

**The key namespace is global, not per-user.** ``JournalEntry.idempotency_key`` is unique across the
whole table, so two users who pick the same string — ``"transfer-1"``, a sequence number, today's
date — collide, and the loser was handed the winner's entry, lines and all. ``JournalEntry`` has no
owner column to scope the lookup by, so the *fingerprint* is the scoping mechanism: both account ids
are inside the digest, and a stranger's key can no longer match a stranger's request.

Three properties the digest has to have, each of which is a bug if it is missing:

* **Canonical**, so a retry that differs only in formatting is still a replay. ``Decimal("25")`` and
  ``Decimal("25.0000")`` are the same transfer, and a naive ``str(payload)`` hash disagrees.
* **Quantized first**, for the same reason and because ADR-0009 says the ledger's opinion of an
  amount is the quantized one.
* **Versioned**, so a field added later does not silently invalidate every stored digest and turn
  every in-flight retry into a conflict. The prefix changes deliberately, and old rows are then
  visibly of a different generation instead of quietly wrong.
"""

import hashlib
import json
from decimal import Decimal
from typing import Any
from uuid import UUID

from common.money import quantize_money, quantize_shares

#: Bump only when the *meaning* of a canonical payload changes. Digests carry it as a prefix, so
#: rows written under an older version are distinguishable rather than merely different.
FINGERPRINT_VERSION = "v1"


def _canonical(value: Any) -> Any:
    """Render one payload value in the single form the digest is allowed to see.

    ``Decimal`` is formatted with ``"f"`` — never ``str()``, which renders a large or tiny value in
    exponent notation and so digests one number two ways. It is **not** quantized here: whether a
    field is money (four places) or a share quantity (eight) is a fact about the field rather than
    about the value, and a helper guessing from the exponent would digest ``Decimal("1.5")`` and
    ``Decimal("1.50000000")`` differently. The builders quantize before handing a value over.
    """
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, UUID):
        return str(value)
    if value is None or isinstance(value, bool | int | str):
        return value
    return str(value)


def fingerprint(payload: dict[str, Any]) -> str:
    """Return the versioned digest of a request payload.

    ``sort_keys`` and the tightest separators make the encoding independent of how the caller
    happened to build the dict, so the digest depends on the request and nothing else.
    """
    body = json.dumps(
        {key: _canonical(value) for key, value in payload.items()},
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(f"{FINGERPRINT_VERSION}|{body}".encode()).hexdigest()
    return f"{FINGERPRINT_VERSION}:{digest}"


def transfer_fingerprint(*, source_id: UUID, destination_id: UUID, amount: Decimal) -> str:
    """The digest of a transfer request.

    Both account ids are in here deliberately: they are what stops one user's key from matching
    another user's transfer, and "the same amount between different accounts" is plainly a different
    request.

    ``description`` is deliberately **out**. A client retrying with a tweaked description has not
    asked for a different money movement, and 409-ing it would be hostile; the replay legitimately
    returns the description the first call posted.
    """
    return fingerprint(
        {
            "kind": "transfer",
            "source": source_id,
            "destination": destination_id,
            "amount": quantize_money(amount),
        }
    )


def order_fingerprint(
    *,
    user_id: int,
    symbol: str,
    side: str,
    order_type: str,
    quantity: Decimal,
    limit_price: Decimal | None,
    cash_account_id: UUID,
) -> str:
    """The digest of an order request.

    ``cash_account_id`` is included because the same order funded from a different account is a
    different request. ``user_id`` is included because, unlike the entry path, the order lookup can
    also be scoped directly — belt and braces on a globally unique key column.
    """
    return fingerprint(
        {
            "kind": "order",
            "user": user_id,
            "symbol": symbol.upper(),
            "side": side,
            "order_type": order_type,
            "quantity": quantize_shares(quantity),
            "limit_price": None if limit_price is None else quantize_money(limit_price),
            "cash_account": cash_account_id,
        }
    )


def backfill_entry_fingerprints(apps: Any) -> int:
    """Give every pre-ADR-0024 keyed entry the digest it would have been written with.

    Lives here rather than in the migration because ``coverage.omit`` excludes ``*/migrations/*``,
    which would make the one piece of code that rewrites historical rows the only uncovered code in
    the app. It takes the ``apps`` registry so the migration can hand it historical models.

    Only two shapes of keyed entry have ever existed, and both are exactly recoverable:

    * a **transfer**, whose two lines are the source (negative) and the destination (positive), so
      the original request is readable straight off the posting;
    * a **fill**, keyed ``order:{pk}``, whose request is the ``Order`` that produced it.

    Returns the number of rows updated so the migration can report it.
    """
    JournalEntry = apps.get_model("ledger", "JournalEntry")
    Order = apps.get_model("trading", "Order")

    updated = 0
    stale = JournalEntry.objects.filter(
        idempotency_key__isnull=False, payload_fingerprint__isnull=True
    )
    for entry in stale.prefetch_related("lines"):
        if entry.idempotency_key.startswith("order:"):
            order = Order.objects.filter(entry_id=entry.pk).select_related("instrument").first()
            if order is None:
                continue
            entry.payload_fingerprint = order_fingerprint(
                user_id=order.user_id,
                symbol=order.instrument.symbol,
                side=order.side,
                order_type=order.order_type,
                quantity=order.quantity,
                limit_price=order.limit_price,
                cash_account_id=order.cash_account_id,
            )
        else:
            lines = list(entry.lines.all())
            source = next((line for line in lines if line.amount < 0), None)
            destination = next((line for line in lines if line.amount > 0), None)
            if source is None or destination is None:
                continue
            entry.payload_fingerprint = transfer_fingerprint(
                source_id=source.account_id,
                destination_id=destination.account_id,
                amount=-source.amount,
            )
        entry.save(update_fields=["payload_fingerprint"])
        updated += 1
    return updated


def backfill_order_fingerprints(apps: Any) -> int:
    """The same, for orders placed with a key before ADR-0024."""
    Order = apps.get_model("trading", "Order")

    updated = 0
    stale = Order.objects.filter(
        idempotency_key__isnull=False, payload_fingerprint__isnull=True
    ).select_related("instrument")
    for order in stale:
        order.payload_fingerprint = order_fingerprint(
            user_id=order.user_id,
            symbol=order.instrument.symbol,
            side=order.side,
            order_type=order.order_type,
            quantity=order.quantity,
            limit_price=order.limit_price,
            cash_account_id=order.cash_account_id,
        )
        order.save(update_fields=["payload_fingerprint"])
        updated += 1
    return updated
