"""The PDF renderer (ADR-0021).

Narrow on purpose. Balances, boundaries and rounding are asserted in ``test_statements_data.py``
against the dataclasses; what is worth checking *here* is only that the bytes are a PDF and that
the numbers the service computed are the numbers on the page. Asserting on raw PDF bytes would be
a test of ReportLab's version, not of this code.
"""

from datetime import date
from decimal import Decimal
from io import BytesIO

import pytest
from django.contrib.auth.models import User
from freezegun import freeze_time
from pypdf import PdfReader

from markets.models import Instrument
from statements.render import money, render_brokerage_statement, render_cash_statement, shares
from statements.services import build_brokerage_statement, build_cash_statement, month_containing
from tests.factories import AccountFactory, fund_account, give_shares, post_balanced_entry
from trading.services import place_order

pytestmark = pytest.mark.django_db

JULY = month_containing(date(2026, 7, 15))


def text_of(pdf: bytes) -> str:
    """Everything the reader would see, whitespace-normalized so line breaks do not matter."""
    reader = PdfReader(BytesIO(pdf))
    return " ".join(" ".join(page.extract_text().split()) for page in reader.pages)


def test_money_prints_to_the_cent_with_thousands_separators() -> None:
    """Storage is four places (ADR-0009); a customer reading a statement wants two."""
    assert money(Decimal("1952.9670")) == "1,952.97"
    assert money(Decimal("-250.0000")) == "-250.00"
    assert money(Decimal("0.0000")) == "0.00"


def test_share_counts_drop_their_trailing_zeros() -> None:
    assert shares(Decimal("6.50000000")) == "6.5"
    assert shares(Decimal("10.00000000")) == "10"
    assert shares(Decimal("100.00000000")) == "100"
    assert shares(Decimal("0.00000001")) == "1E-8"


def test_a_cash_statement_is_a_pdf_carrying_its_own_numbers(password_user: User) -> None:
    cash = AccountFactory.create(owner=password_user, name="Everyday")
    other = AccountFactory.create(owner=password_user, name="Savings")
    with freeze_time("2026-06-28T09:00:00Z"):
        fund_account(cash, Decimal("1200.0000"))
    with freeze_time("2026-07-09T09:00:00Z"):
        post_balanced_entry(cash, other, Decimal("349.5000"), description="Electricity bill")

    data = build_cash_statement(cash, JULY)
    pdf = render_cash_statement(data)

    assert pdf.startswith(b"%PDF")
    body = text_of(pdf)
    assert "Account statement" in body
    assert "Everyday" in body
    assert "Electricity bill" in body
    assert money(data.opening_balance) in body
    assert money(data.closing_balance) in body
    assert "850.50" in body


def test_an_empty_cash_statement_says_so_rather_than_rendering_an_empty_table(
    password_user: User,
) -> None:
    cash = AccountFactory.create(owner=password_user, name="Dormant")
    with freeze_time("2026-05-01T09:00:00Z"):
        fund_account(cash, Decimal("40.0000"))

    body = text_of(render_cash_statement(build_cash_statement(cash, JULY)))

    assert "No transactions in this period." in body
    assert "40.00" in body


def test_a_brokerage_statement_prints_every_holding_and_its_valuation(
    password_user: User, instrument: Instrument
) -> None:
    with freeze_time("2026-07-06T09:00:00Z"):
        give_shares(password_user, instrument, Decimal("6.5"), Decimal("650.0000"))

    data = build_brokerage_statement(password_user, JULY)
    pdf = render_brokerage_statement(data)

    assert pdf.startswith(b"%PDF")
    body = text_of(pdf)
    assert "Brokerage statement" in body
    assert instrument.symbol in body
    assert "6.5" in body
    assert money(data.market_value) in body
    assert "No trades in this period." in body


def test_a_brokerage_statement_prints_the_periods_trades(
    password_user: User, instrument: Instrument
) -> None:
    cash = AccountFactory.create(owner=password_user, name="Brokerage cash")
    with freeze_time("2026-07-01T09:00:00Z"):
        fund_account(cash, Decimal("10000.0000"))
        give_shares(password_user, instrument, Decimal("10"), Decimal("1000.0000"))
    instrument.last_price = Decimal("125.0000")
    instrument.save(update_fields=["last_price"])
    with freeze_time("2026-07-22T15:45:00Z"):
        place_order(
            user=password_user,
            instrument=instrument,
            cash_account=cash,
            side="sell",
            order_type="market",
            quantity=Decimal("4"),
        )

    data = build_brokerage_statement(password_user, JULY)
    body = text_of(render_brokerage_statement(data))

    assert "2026-07-22 15:45" in body
    assert "Sell" in body
    assert "500.00" in body  # 4 × 125 proceeds
    assert money(data.realized_pnl) in body
    assert "No trades in this period." not in body


def test_a_fully_exited_month_shows_its_trades_and_no_positions(
    password_user: User, instrument: Instrument
) -> None:
    """Sold out by the 31st: there is nothing to list under holdings, and plenty under trades."""
    cash = AccountFactory.create(owner=password_user, name="Brokerage cash")
    with freeze_time("2026-07-01T09:00:00Z"):
        fund_account(cash, Decimal("10000.0000"))
        give_shares(password_user, instrument, Decimal("5"), Decimal("500.0000"))
    with freeze_time("2026-07-28T11:00:00Z"):
        place_order(
            user=password_user,
            instrument=instrument,
            cash_account=cash,
            side="sell",
            order_type="market",
            quantity=Decimal("5"),
        )

    body = text_of(render_brokerage_statement(build_brokerage_statement(password_user, JULY)))

    assert "No positions held at the end of this period." in body
    assert "Sell" in body


def test_a_long_statement_runs_to_more_than_one_page(password_user: User) -> None:
    """Repeated header rows and page numbers only matter if the document actually paginates."""
    cash = AccountFactory.create(owner=password_user, name="Busy")
    other = AccountFactory.create(owner=password_user, name="Savings")
    with freeze_time("2026-06-30T09:00:00Z"):
        fund_account(cash, Decimal("5000.0000"))
    with freeze_time("2026-07-15T09:00:00Z"):
        for index in range(60):
            post_balanced_entry(cash, other, Decimal("1.0000"), description=f"payment {index}")

    pdf = render_cash_statement(build_cash_statement(cash, JULY))

    assert len(PdfReader(BytesIO(pdf)).pages) > 1
    assert "Page 2" in text_of(pdf)
