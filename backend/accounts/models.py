"""Account model — an owner's named ledger account (ADR-0008, docs/er-diagram.md).

Balances are never stored here; they are derived as the sum of the account's journal lines
(see ``ledger.services.get_balance``). ``account_type`` is recorded from day one but does not yet
drive debit/credit sign conventions — the v1 ledger is simplified-signed (ADR-0008).
"""

from datetime import datetime
from decimal import Decimal

import uuid6
from django.conf import settings
from django.db import models
from django.db.models.functions import Coalesce

from accounts import numbers
from common.crypto import decrypt, encrypt


def _before(as_of: datetime | None) -> models.Q | None:
    """The aggregate filter for an as-of sum, or None for "everything posted so far"."""
    return None if as_of is None else models.Q(lines__created_at__lt=as_of)


class AccountType(models.TextChoices):
    ASSET = "asset", "Asset"
    LIABILITY = "liability", "Liability"
    EQUITY = "equity", "Equity"
    INCOME = "income", "Income"
    EXPENSE = "expense", "Expense"


class AccountQuerySet(models.QuerySet["Account"]):
    def with_balance(self, *, as_of: datetime | None = None) -> "AccountQuerySet":
        """Annotate each row with its derived balance in the same query.

        One aggregate for the whole queryset instead of a ``get_balance()`` call per row — the
        difference between one query and N. Uses the ``lines`` reverse accessor by name so the
        accounts app stays free of ledger imports.

        ``as_of`` restricts the sum to lines posted *before* that instant, which is what a statement
        needs for an opening balance and for valuing a holding at a period boundary (ADR-0021).
        The ledger is append-only and lines are never backdated, so the answer for a closed period
        is stable no matter when it is asked.
        """
        return self.annotate(
            balance=Coalesce(
                models.Sum("lines__amount", filter=_before(as_of)),
                models.Value(
                    Decimal("0.0000"),
                    output_field=models.DecimalField(max_digits=20, decimal_places=4),
                ),
            )
        )

    def with_quantity(self, *, as_of: datetime | None = None) -> "AccountQuerySet":
        """Annotate each row with its derived share quantity (ADR-0016).

        Safe to chain with :meth:`with_balance`: both aggregate over the *same* ``lines`` relation,
        so the two SUMs share one JOIN. (The classic double-counting trap needs aggregates over two
        *different* multi-valued relations, which would fan the rows out.)

        For a cash account this is 0 — every line has a NULL quantity, and SUM ignores NULLs.
        """
        return self.annotate(
            quantity=Coalesce(
                models.Sum("lines__quantity", filter=_before(as_of)),
                models.Value(
                    Decimal("0E-8"),
                    output_field=models.DecimalField(max_digits=20, decimal_places=8),
                ),
            )
        )

    def positions(self) -> "AccountQuerySet":
        """Only holdings: instrument-backed accounts someone actually owns shares in.

        The ``account_type`` filter arrived with ADR-0025, which gave every instrument a second
        instrument-bearing account — the shares-outstanding contra that a fill's third line credits.
        A contra is EQUITY and is nobody's holding; without this filter it would surface as a
        position with a negative quantity, netting every portfolio to zero.

        Callers scope by owner first and the contras belong to the system user, so this is belt and
        braces. It is here anyway because "positions" should mean holdings no matter who asks.
        """
        return self.filter(instrument__isnull=False, account_type=AccountType.ASSET)

    def share_contras(self) -> "AccountQuerySet":
        """The market's side of every holding — the shares-outstanding accounts (ADR-0025)."""
        return self.filter(instrument__isnull=False, account_type=AccountType.EQUITY)

    def cash(self) -> "AccountQuerySet":
        """Only ordinary money accounts — everything that is not instrument-backed."""
        return self.filter(instrument__isnull=True)

    def spendable(self) -> "AccountQuerySet":
        """Accounts holding money the owner can actually move.

        :meth:`cash` means "not a position", which is a narrower claim than it reads like. It also
        admits the *equity* opening-balances account that funding posts against and the *income*
        realized-P&L account a sell creates — the other side of a user's own money, not money.

        ``trading.portfolio.cash_balance_for`` and ``statements.services`` had always added the
        ``account_type`` filter by hand, each with its own comment explaining why. The endpoints had
        not, and there the omission moved money: ``trading.services._sell_lines`` posts the residual
        as ``amount=-gain``, so a realized **loss** leaves the income account with a *positive*
        balance, and a transfer scoped by owner alone would happily pay it out as cash.

        Same predicate as :meth:`Account._deserves_a_number` — an account a customer holds is
        exactly an account worth printing a number on.
        """
        return self.cash().filter(account_type=AccountType.ASSET)


class Account(models.Model):
    """A ledger account owned by a user. UUIDv7 PK: non-enumerable, index-local (ADR-0005)."""

    id = models.UUIDField(primary_key=True, default=uuid6.uuid7, editable=False)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="accounts",
    )
    name = models.CharField(max_length=100)
    # The customer-facing account number, envelope-encrypted (ADR-0027). Blank on the accounts a
    # customer never sees — position accounts, the shares-outstanding contras, the bookkeeping
    # equity and income accounts — because a number is a label for something someone holds.
    number_ciphertext = models.CharField(max_length=512, blank=True)
    # The last four digits, in plaintext and on purpose: a masked label is the common read, and
    # decrypting a whole page of accounts to render "••••6789" would be a lot of AES for something
    # that leaks four guessable digits. The full number stays encrypted.
    number_last4 = models.CharField(max_length=4, blank=True)
    account_type = models.CharField(max_length=10, choices=AccountType.choices)
    currency = models.CharField(max_length=3, default="USD")
    # Set on the two kinds of instrument-bearing account, distinguished by `account_type`:
    #
    #   ASSET  — a *position*, one per (owner, instrument), holding that instrument's shares
    #            (ADR-0016). Balance is the holding's cost basis in USD; share count is the sum of
    #            its lines' quantities.
    #   EQUITY — the *shares-outstanding contra*, one per instrument, owned by the system user
    #            (ADR-0025). It is where shares come from and go back to, and its existence is what
    #            lets Postgres check that a fill conserves them.
    #
    # NULL on every ordinary money account, which is most of them.
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
            # An instrument-bearing account is either a holding (ASSET, balance = cost basis) or
            # the market's side of it (EQUITY, the shares-outstanding contra of ADR-0025). Widened
            # from "must be an asset" to an explicit two-item list rather than to "not a liability":
            # an INCOME- or EXPENSE-typed instrument account would satisfy the loose version, and
            # nothing in trading/ is prepared to meet one.
            models.CheckConstraint(
                condition=models.Q(instrument__isnull=True)
                | models.Q(account_type__in=[AccountType.ASSET, AccountType.EQUITY]),
                name="instrument_account_is_an_asset_or_equity",
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

    def save(self, *args: object, **kwargs: object) -> None:
        """Assign a number on first save, to the accounts that should have one (ADR-0027).

        In ``save`` rather than a service because there is no account-creation service to put it
        in: accounts are made by ``get_or_create`` in the trading services, by the demo command and
        by test factories, and a rule enforced in one of those would be absent from the others. The
        alternative — every caller remembering — is the class of convention this week is removing.

        Bookkeeping accounts are deliberately left blank: position accounts, the shares-outstanding
        contras, and the equity and income accounts are the *other side* of a customer's money
        rather than something a customer holds. Numbering them would put them in every "your
        accounts" list that renders one — the mistake ``/accounts/`` already avoids by filtering.
        """
        if self._state.adding and not self.number_ciphertext and self._deserves_a_number():
            self.number = numbers.generate()
        super().save(*args, **kwargs)  # type: ignore[arg-type]

    def _deserves_a_number(self) -> bool:
        return self.instrument_id is None and self.account_type == AccountType.ASSET

    @property
    def number(self) -> str:
        """The full account number, decrypted (ADR-0027).

        A property rather than a custom field, for the reason ``MfaDevice.secret`` gives: a field
        subclass would decrypt during ``values()`` and admin list rendering too, widening the set
        of code paths holding plaintext. Read on the detail endpoint and nowhere else — list views
        render :attr:`masked_number`, which costs no decryption at all.
        """
        return decrypt(self.number_ciphertext)

    @number.setter
    def number(self, value: str) -> None:
        self.number_ciphertext = encrypt(value)
        self.number_last4 = numbers.last4(value)

    @property
    def masked_number(self) -> str:
        """``BK-••••••6789``, built from the plaintext last four — no key needed."""
        return f"{numbers.PREFIX}-{'•' * 6}{self.number_last4}" if self.number_last4 else ""

    @property
    def is_position(self) -> bool:
        """Whether this account is denominated in shares rather than only in dollars.

        True for a holding *and* for its shares-outstanding contra (ADR-0025) — both carry an
        instrument, and both must carry a quantity on every line that touches them, which is what
        this property is asked about. Use ``AccountQuerySet.positions()`` when the question is the
        narrower "is this somebody's holding".
        """
        return self.instrument_id is not None
