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

    def with_quantity(self) -> "AccountQuerySet":
        """Annotate each row with its derived share quantity (ADR-0016).

        Safe to chain with :meth:`with_balance`: both aggregate over the *same* ``lines`` relation,
        so the two SUMs share one JOIN. (The classic double-counting trap needs aggregates over two
        *different* multi-valued relations, which would fan the rows out.)

        For a cash account this is 0 — every line has a NULL quantity, and SUM ignores NULLs.
        """
        return self.annotate(
            quantity=Coalesce(
                models.Sum("lines__quantity"),
                models.Value(
                    Decimal("0E-8"),
                    output_field=models.DecimalField(max_digits=20, decimal_places=8),
                ),
            )
        )

    def positions(self) -> "AccountQuerySet":
        """Only instrument-backed position accounts."""
        return self.filter(instrument__isnull=False)

    def cash(self) -> "AccountQuerySet":
        """Only ordinary money accounts — everything that is not a position."""
        return self.filter(instrument__isnull=True)


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
    # Set on a *position* account: one per (owner, instrument), holding that instrument's shares
    # (ADR-0016). Its balance is the holding's cost basis in USD; its share count is the sum of its
    # lines' quantities. NULL on every ordinary money account, which is most of them.
    instrument = models.ForeignKey(
        "markets.Instrument",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="position_accounts",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    objects = AccountQuerySet.as_manager()

    class Meta:
        ordering = ["created_at"]
        constraints = [
            # One position account per holding. NULLs don't collide in a Postgres unique index, so
            # a user's many cash accounts are unaffected — the same property ADR-0010 leans on for
            # idempotency keys.
            models.UniqueConstraint(
                fields=["owner", "instrument"],
                condition=models.Q(instrument__isnull=False),
                name="one_position_account_per_instrument",
            ),
            # A holding is something you own. Letting a position account be a liability or an
            # expense would make its balance mean something other than cost basis.
            models.CheckConstraint(
                condition=models.Q(instrument__isnull=True)
                | models.Q(account_type=AccountType.ASSET),
                name="position_account_is_an_asset",
            ),
            # Income accounts (today: realized P&L) are created lazily, on the first sell that
            # needs one. Two concurrent sells would otherwise each create their own and split the
            # running total between them; this makes get_or_create settle that race in the
            # database. Scoped to income so the many "Opening balances" equity accounts a test
            # fixture creates for one user stay legal.
            models.UniqueConstraint(
                fields=["owner", "name"],
                condition=models.Q(account_type=AccountType.INCOME),
                name="one_income_account_per_name",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.account_type})"

    @property
    def is_position(self) -> bool:
        return self.instrument_id is not None
