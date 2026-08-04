"""Statement data → PDF bytes. The only module in the system that imports ReportLab (ADR-0021).

Kept behind one function per statement kind so the interesting half — periods, balances, rounding —
is asserted against the dataclasses in :mod:`statements.services` and never through a parsed PDF.
What is tested *here* is narrow and deliberate: that the bytes are a PDF, and that the numbers the
service computed are the numbers on the page.

Money is formatted to the cent, which is what ``common.money.CENT`` exists for. Storage is four
places (ADR-0009) and always will be; a customer reading a statement does not want to see the other
two, and this is the one place in the system where presentation is the product.
"""

from decimal import Decimal
from io import BytesIO
from typing import Any

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from common.money import CENT

from .services import BrokerageStatementData, CashStatementData

DOCUMENT_TITLE = "Personal Banking Platform — Statement"

_HEADER_BG = colors.HexColor("#1f2933")
_ROW_BG = colors.HexColor("#f4f6f8")
_RULE = colors.HexColor("#c9d2da")


def money(value: Decimal) -> str:
    """Format an amount to the cent, with a leading minus for money leaving an account."""
    return f"{value.quantize(CENT):,}"


def shares(value: Decimal) -> str:
    """Share counts print with trailing zeros trimmed — ``6.5``, not ``6.50000000``."""
    normalized = value.normalize()
    # normalize() renders large integers in scientific notation (1E+2); quantizing to an integer
    # exponent undoes that without reintroducing the eight stored decimal places.
    if normalized == normalized.to_integral_value():
        return f"{normalized.quantize(Decimal(1)):,}"
    return f"{normalized:,}"


def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle("title", parent=base["Title"], fontSize=16, spaceAfter=2 * mm),
        "meta": ParagraphStyle("meta", parent=base["Normal"], fontSize=9, leading=13),
        "section": ParagraphStyle(
            "section", parent=base["Heading2"], fontSize=11, spaceBefore=6 * mm, spaceAfter=2 * mm
        ),
        "cell": ParagraphStyle("cell", parent=base["Normal"], fontSize=8, leading=11),
        "right": ParagraphStyle("right", parent=base["Normal"], fontSize=8, alignment=TA_RIGHT),
        "empty": ParagraphStyle("empty", parent=base["Normal"], fontSize=9, textColor=colors.grey),
    }


def _table(rows: list[list[Any]], widths: list[float], right_align: tuple[int, ...]) -> Table:
    table = Table(rows, colWidths=widths, repeatRows=1, hAlign="LEFT")
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), _HEADER_BG),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, _ROW_BG]),
        ("GRID", (0, 0), (-1, -1), 0.25, _RULE),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]
    for column in right_align:
        style.append(("ALIGN", (column, 0), (column, -1), "RIGHT"))
    table.setStyle(TableStyle(style))
    return table


def _summary(pairs: list[tuple[str, str]]) -> Table:
    table = Table([[label, value] for label, value in pairs], colWidths=[60 * mm, 50 * mm])
    table.setStyle(
        TableStyle(
            [
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("ALIGN", (1, 0), (1, -1), "RIGHT"),
                ("LINEBELOW", (0, 0), (-1, -2), 0.25, _RULE),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )
    return table


def _document(buffer: BytesIO, subject: str) -> SimpleDocTemplate:
    return SimpleDocTemplate(
        buffer,
        pagesize=A4,
        title=DOCUMENT_TITLE,
        subject=subject,
        author="Personal Banking Platform",
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
    )


def _footer(canvas: Any, document: Any) -> None:
    """Page numbers, drawn on every page. A statement without them is not evidence of anything."""
    canvas.saveState()
    canvas.setFont("Helvetica", 7)
    canvas.setFillColor(colors.grey)
    canvas.drawRightString(
        document.pagesize[0] - 18 * mm, 12 * mm, f"Page {canvas.getPageNumber()}"
    )
    canvas.restoreState()


def render_cash_statement(data: CashStatementData) -> bytes:
    """One account's monthly statement."""
    styles = _styles()
    buffer = BytesIO()
    document = _document(buffer, f"Statement {data.period.label} — {data.account_name}")

    story: list[Any] = [
        Paragraph("Account statement", styles["title"]),
        Paragraph(
            f"<b>{data.account_name}</b><br/>"
            f"Account {data.account_id}<br/>"
            f"{data.user_label}<br/>"
            f"Period {data.period}",
            styles["meta"],
        ),
        Spacer(1, 6 * mm),
        _summary(
            [
                ("Opening balance", money(data.opening_balance)),
                ("Money in", money(data.total_in)),
                ("Money out", money(data.total_out)),
                ("Closing balance", money(data.closing_balance)),
            ]
        ),
        Paragraph("Transactions", styles["section"]),
    ]

    if data.lines:
        rows: list[list[Any]] = [["Date", "Description", "Amount", "Balance"]]
        rows += [
            [
                line.posted_at.strftime("%Y-%m-%d %H:%M"),
                Paragraph(line.description, styles["cell"]),
                money(line.amount),
                money(line.balance),
            ]
            for line in data.lines
        ]
        story.append(_table(rows, [32 * mm, 78 * mm, 30 * mm, 30 * mm], right_align=(2, 3)))
    else:
        story.append(Paragraph("No transactions in this period.", styles["empty"]))

    document.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return buffer.getvalue()


def render_brokerage_statement(data: BrokerageStatementData) -> bytes:
    """Positions at the close of the period, the fills that produced them, and realized P&L."""
    styles = _styles()
    buffer = BytesIO()
    document = _document(buffer, f"Brokerage statement {data.period.label}")

    story: list[Any] = [
        Paragraph("Brokerage statement", styles["title"]),
        Paragraph(f"{data.user_label}<br/>Period {data.period}", styles["meta"]),
        Spacer(1, 6 * mm),
        _summary(
            [
                ("Market value", money(data.market_value)),
                ("Cost basis", money(data.cost_basis)),
                ("Unrealized P&L", money(data.unrealized_pnl)),
                ("Realized P&L (period)", money(data.realized_pnl)),
            ]
        ),
        Paragraph("Holdings at period end", styles["section"]),
    ]

    if data.holdings:
        holding_rows: list[list[Any]] = [
            ["Symbol", "Quantity", "Avg cost", "Price", "Cost basis", "Market value", "Unrealized"]
        ]
        holding_rows += [
            [
                holding.instrument.symbol,
                shares(holding.quantity),
                money(holding.average_cost),
                money(holding.last_price),
                money(holding.cost_basis),
                money(holding.market_value),
                money(holding.unrealized_pnl),
            ]
            for holding in data.holdings
        ]
        story.append(
            _table(
                holding_rows,
                [20 * mm, 22 * mm, 24 * mm, 22 * mm, 26 * mm, 30 * mm, 26 * mm],
                right_align=(1, 2, 3, 4, 5, 6),
            )
        )
    else:
        story.append(Paragraph("No positions held at the end of this period.", styles["empty"]))

    story.append(Paragraph("Trades", styles["section"]))
    if data.trades:
        trade_rows: list[list[Any]] = [["Date", "Side", "Symbol", "Quantity", "Price", "Value"]]
        trade_rows += [
            [
                trade.resolved_at.strftime("%Y-%m-%d %H:%M"),
                trade.side.title(),
                trade.symbol,
                shares(trade.quantity),
                money(trade.price),
                money(trade.notional),
            ]
            for trade in data.trades
        ]
        story.append(
            _table(
                trade_rows,
                [32 * mm, 18 * mm, 22 * mm, 28 * mm, 28 * mm, 32 * mm],
                right_align=(3, 4, 5),
            )
        )
    else:
        story.append(Paragraph("No trades in this period.", styles["empty"]))

    document.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return buffer.getvalue()
