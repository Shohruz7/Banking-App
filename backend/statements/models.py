"""Generated monthly statements (ADR-0021).

A statement is a **point-in-time artifact**, which is the one thing that separates this model from
everything else built since Week 2. Balances everywhere else in the system are derived on read and
deliberately not stored; here they are stored, because a statement has to keep saying what its PDF
says, has to be answerable ("what did this account close July at?") without opening a file, and
describes a period that is closed and can no longer change.

That is safe for the same reason ``Instrument.last_price`` is safe: exactly one writer
(:mod:`statements.tasks`) and a window that is already over.
"""

import uuid6
from django.conf import settings
from django.db import models


class StatementKind(models.TextChoices):
    CASH = "cash", "Cash account"
    BROKERAGE = "brokerage", "Brokerage"


def statement_upload_to(instance: "Statement", filename: str) -> str:
    """``statements/<user>/<YYYY-MM>/<account|brokerage>.pdf``.

    Owner-partitioned so a misconfigured storage backend cannot mix two users' files into one
    directory, and month-partitioned so a year of statements is a listable tree rather than one
    flat folder with thousands of entries.
    """
    scope = str(instance.account_id) if instance.account_id else "brokerage"
    return f"statements/{instance.user_id}/{instance.period_start:%Y-%m}/{scope}.pdf"


class Statement(models.Model):
    """One generated statement for one closed month."""

    id = models.UUIDField(primary_key=True, default=uuid6.uuid7, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="statements",
    )
    # Set on a cash statement, NULL on the brokerage one — a brokerage statement spans every
    # position account the user holds, so no single account owns it.
    account = models.ForeignKey(
        "accounts.Account",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="statements",
    )
    kind = models.CharField(max_length=9, choices=StatementKind.choices)

    period_start = models.DateField()
    # Inclusive: the last day of the month, which is what the PDF prints. Every *query* uses the
    # exclusive instant after it (``Period.end_at``) so a line posted at 23:59:59.999 is included.
    period_end = models.DateField()

    opening_balance = models.DecimalField(max_digits=20, decimal_places=4)
    closing_balance = models.DecimalField(max_digits=20, decimal_places=4)
    line_count = models.PositiveIntegerField(default=0)

    file = models.FileField(upload_to=statement_upload_to)
    generated_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-period_start", "kind"]
        constraints = [
            # Regeneration is idempotent because of these two, not because the task checks first:
            # a second Beat run for the same month collides with the index rather than producing a
            # second July. Partial, in the shape `Account` already uses for position accounts —
            # `account` is NULL on every brokerage row, and NULLs do not collide in Postgres.
            models.UniqueConstraint(
                fields=["account", "period_start"],
                condition=models.Q(kind=StatementKind.CASH),
                name="one_cash_statement_per_account_month",
            ),
            models.UniqueConstraint(
                fields=["user", "period_start"],
                condition=models.Q(kind=StatementKind.BROKERAGE),
                name="one_brokerage_statement_per_user_month",
            ),
            # A cash statement without an account has nothing to be a statement *of*; a brokerage
            # statement with one is claiming a scope it does not have.
            models.CheckConstraint(
                condition=(
                    models.Q(kind=StatementKind.CASH, account__isnull=False)
                    | models.Q(kind=StatementKind.BROKERAGE, account__isnull=True)
                ),
                name="statement_account_iff_cash",
            ),
            models.CheckConstraint(
                condition=models.Q(period_end__gte=models.F("period_start")),
                name="statement_period_ordered",
            ),
        ]
        indexes = [
            models.Index(fields=["user", "-period_start"], name="statement_user_period_idx"),
        ]

    def __str__(self) -> str:
        scope = self.account_id if self.kind == StatementKind.CASH else "brokerage"
        return f"{self.period_start:%Y-%m} {scope}"

    @property
    def period_label(self) -> str:
        return f"{self.period_start:%Y-%m}"
