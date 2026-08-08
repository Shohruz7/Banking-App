"""Ledger models — the double-entry spine (ADR-0008, docs/er-diagram.md).

A ``JournalEntry`` is a transaction header; its ``JournalLine`` rows carry signed ``Decimal``
amounts (positive = credit to the account, negative = debit). The core invariant — an entry's
lines sum to exactly zero — is validated in ``ledger.services.post_entry`` and enforced at COMMIT
by a deferred Postgres constraint trigger (migration ``0002``). Nothing writes lines except the
posting service; the models below are deliberately behavior-free.
"""

import uuid6
from django.db import models


class JournalEntry(models.Model):
    """Transaction header, append-only in operation: no code path mutates a posted entry.

    One deliberate exception, in history rather than in code: migration ``0008`` added
    shares-outstanding contra legs to fills posted before ADR-0025, backdated to their siblings'
    timestamps so that ``as_of`` queries over closed periods keep the answers they already gave.
    Recorded here rather than left to be discovered — an undocumented exception is what turns an
    accurate docstring into a false one.
    """

    id = models.UUIDField(primary_key=True, default=uuid6.uuid7, editable=False)
    description = models.CharField(max_length=255)
    # Client-supplied retry token (ADR-0010). Unique, but NULLs don't collide in Postgres, so
    # entries posted without a key — everything that isn't a transfer — are unaffected.
    idempotency_key = models.CharField(max_length=64, null=True, blank=True, unique=True)
    # The digest of the request this entry came from (ADR-0024). Set whenever a key is, and the
    # reason a key is now safe: the key column is unique *globally*, so without this a second user
    # picking the same string was handed the first user's entry and its lines. Both account ids are
    # inside the digest, so a foreign key cannot match a foreign request.
    # DJ001 is suppressed on the field: NULL is the right absence here, not "". The CHECK below
    # pairs a NULL key with a NULL digest, and an empty string would be a digest matching nothing —
    # silently turning every keyless entry into a conflict waiting to happen. `idempotency_key`
    # above escapes the same lint only because it is unique.
    payload_fingerprint = models.CharField(max_length=80, null=True, blank=True)  # noqa: DJ001
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        verbose_name_plural = "journal entries"
        constraints = [
            # A key without a digest is a key nothing can be compared against — the pre-ADR-0024
            # shape. Validated against backfilled data rather than added NOT VALID, which would
            # have accepted every legacy row and left the guarantee true only for new ones.
            models.CheckConstraint(
                condition=models.Q(idempotency_key__isnull=True)
                | models.Q(payload_fingerprint__isnull=False),
                name="keyed_entry_has_a_fingerprint",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.description} ({self.id})"


class JournalLine(models.Model):
    """One signed leg of an entry. Internal BigAutoField PK — lines are never exposed by ID."""

    entry = models.ForeignKey(
        JournalEntry,
        on_delete=models.PROTECT,
        related_name="lines",
    )
    account = models.ForeignKey(
        "accounts.Account",
        on_delete=models.PROTECT,
        related_name="lines",
    )
    amount = models.DecimalField(max_digits=20, decimal_places=4)
    # Set only on a line touching a position account: how many shares moved, signed the same way as
    # ``amount`` (ADR-0016). The zero-sum invariant is about ``amount`` alone and is unaffected —
    # this column rides alongside it so a holding's share count and its cost basis are written in
    # the same row, and can never disagree about what a fill did.
    quantity = models.DecimalField(max_digits=20, decimal_places=8, null=True, blank=True)
    currency = models.CharField(max_length=3, default="USD")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            # A line must move *something* — money, shares, or both (ADR-0025).
            #
            # Relaxed from a flat `amount != 0` to make room for the shares-outstanding contra leg,
            # which moves shares at no cost: its amount is 0 by construction, because the cash and
            # the position legs already balance each other. The rule that replaced it is stricter
            # about what it means rather than merely looser: the old version could not express
            # "this row does nothing at all", which is the thing actually worth forbidding.
            #
            # No three-valued-logic trap here, though it looks like one: `amount` is NOT NULL, so
            # the left conjunct is never NULL, and `quantity IS NULL` is never NULL either.
            models.CheckConstraint(
                condition=~(models.Q(amount=0) & models.Q(quantity__isnull=True)),
                name="journal_line_moves_money_or_shares",
            ),
            # Mirrors the amount rule: absent is meaningful, zero is not.
            models.CheckConstraint(
                condition=models.Q(quantity__isnull=True) | ~models.Q(quantity=0),
                name="journal_line_quantity_nonzero",
            ),
        ]
        indexes = [
            models.Index(fields=["account", "created_at"], name="line_account_created_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.amount} {self.currency} → {self.account_id}"
