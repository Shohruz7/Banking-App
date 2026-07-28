"""Account model — an owner's named ledger account (ADR-0008, docs/er-diagram.md).

Balances are never stored here; they are derived as the sum of the account's journal lines
(see ``ledger.services.get_balance``). ``account_type`` is recorded from day one but does not yet
drive debit/credit sign conventions — the v1 ledger is simplified-signed (ADR-0008).
"""

from decimal import Decimal

import uuid6
from django.conf import settings
from django.db import models
from django.db.models.functions import Coalesce


class AccountType(models.TextChoices):
    ASSET = "asset", "Asset"
    LIABILITY = "liability", "Liability"
    EQUITY = "equity", "Equity"
    INCOME = "income", "Income"
    EXPENSE = "expense", "Expense"


class AccountQuerySet(models.QuerySet["Account"]):
    def with_balance(self) -> "AccountQuerySet":
        """Annotate each row with its derived balance in the same query.

        One aggregate for the whole queryset instead of a ``get_balance()`` call per row — the
        difference between one query and N. Uses the ``lines`` reverse accessor by name so the
        accounts app stays free of ledger imports.
        """
        return self.annotate(
            balance=Coalesce(
                models.Sum("lines__amount"),
                models.Value(
                    Decimal("0.0000"),
                    output_field=models.DecimalField(max_digits=20, decimal_places=4),
                ),
            )
        )


class Account(models.Model):
    """A ledger account owned by a user. UUIDv7 PK: non-enumerable, index-local (ADR-0005)."""

    id = models.UUIDField(primary_key=True, default=uuid6.uuid7, editable=False)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="accounts",
    )
    name = models.CharField(max_length=100)
    account_type = models.CharField(max_length=10, choices=AccountType.choices)
    currency = models.CharField(max_length=3, default="USD")
    created_at = models.DateTimeField(auto_now_add=True)

    objects = AccountQuerySet.as_manager()

    class Meta:
        ordering = ["created_at"]

    def __str__(self) -> str:
        return f"{self.name} ({self.account_type})"
