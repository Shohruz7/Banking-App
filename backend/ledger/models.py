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
    """Transaction header. Append-only: entries and their lines are never mutated after posting."""

    id = models.UUIDField(primary_key=True, default=uuid6.uuid7, editable=False)
    description = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        verbose_name_plural = "journal entries"

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
    currency = models.CharField(max_length=3, default="USD")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(amount=0),
                name="journal_line_amount_nonzero",
            ),
        ]
        indexes = [
            models.Index(fields=["account", "created_at"], name="line_account_created_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.amount} {self.currency} → {self.account_id}"
