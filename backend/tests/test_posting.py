"""The posting primitive: what posts, what is rejected, and that balances derive correctly."""

from decimal import Decimal

import pytest

from ledger.exceptions import InvalidEntryError, UnbalancedEntryError
from ledger.models import JournalEntry, JournalLine
from ledger.services import LineSpec, get_balance, post_entry
from tests.factories import AccountFactory, post_balanced_entry


@pytest.mark.django_db
def test_balanced_entry_posts_with_its_lines() -> None:
    source = AccountFactory.create()
    destination = AccountFactory.create()

    entry = post_balanced_entry(source, destination, Decimal("100.00"), description="deposit")

    assert JournalEntry.objects.count() == 1
    assert entry.lines.count() == 2
    assert get_balance(source) == Decimal("-100.0000")
    assert get_balance(destination) == Decimal("100.0000")


@pytest.mark.django_db
def test_unbalanced_entry_rejected_by_service_and_persists_nothing() -> None:
    source = AccountFactory.create()
    destination = AccountFactory.create()

    with pytest.raises(UnbalancedEntryError):
        post_entry(
            description="off by a cent",
            lines=[
                LineSpec(account=source, amount=Decimal("-100.00")),
                LineSpec(account=destination, amount=Decimal("100.01")),
            ],
        )

    assert JournalEntry.objects.count() == 0
    assert JournalLine.objects.count() == 0


@pytest.mark.django_db
@pytest.mark.parametrize("line_count", [0, 1])
def test_entry_requires_at_least_two_lines(line_count: int) -> None:
    account = AccountFactory.create()
    lines = [LineSpec(account=account, amount=Decimal("1.00"))][:line_count]

    with pytest.raises(InvalidEntryError):
        post_entry(description="too few lines", lines=lines)


@pytest.mark.django_db
def test_zero_amount_line_rejected_by_service() -> None:
    source = AccountFactory.create()
    destination = AccountFactory.create()

    with pytest.raises(InvalidEntryError):
        post_entry(
            description="zero leg",
            lines=[
                LineSpec(account=source, amount=Decimal("0.00")),
                LineSpec(account=destination, amount=Decimal("0.00")),
            ],
        )


@pytest.mark.django_db
def test_non_usd_account_rejected() -> None:
    usd = AccountFactory.create()
    eur = AccountFactory.create(currency="EUR")

    with pytest.raises(InvalidEntryError):
        post_entry(
            description="wrong currency",
            lines=[
                LineSpec(account=usd, amount=Decimal("-10.00")),
                LineSpec(account=eur, amount=Decimal("10.00")),
            ],
        )


@pytest.mark.django_db
def test_amounts_are_quantized_with_banker_rounding() -> None:
    source = AccountFactory.create()
    destination = AccountFactory.create()

    # 10.12345 sits exactly on the 4-place boundary; half-even rounds to the even digit → 10.1234.
    entry = post_entry(
        description="sub-cent rounding",
        lines=[
            LineSpec(account=source, amount=Decimal("-10.12345")),
            LineSpec(account=destination, amount=Decimal("10.12345")),
        ],
    )

    stored = sorted(line.amount for line in entry.lines.all())
    assert stored == [Decimal("-10.1234"), Decimal("10.1234")]


@pytest.mark.django_db
def test_balances_derive_across_multiple_accounts() -> None:
    a = AccountFactory.create()
    b = AccountFactory.create()
    c = AccountFactory.create()

    post_balanced_entry(a, b, Decimal("100.00"))
    post_balanced_entry(a, c, Decimal("30.00"))
    post_balanced_entry(b, c, Decimal("10.00"))

    assert get_balance(a) == Decimal("-130.0000")  # -100 - 30
    assert get_balance(b) == Decimal("90.0000")  # +100 - 10
    assert get_balance(c) == Decimal("40.0000")  # +30 + 10
    # Conservation: every posting nets to zero across the system.
    assert get_balance(a) + get_balance(b) + get_balance(c) == Decimal("0.0000")


@pytest.mark.django_db
def test_empty_account_balance_is_zero() -> None:
    assert get_balance(AccountFactory.create()) == Decimal("0.0000")
