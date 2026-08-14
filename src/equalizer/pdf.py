"""
pdf.py — Renders an Equalizer worksheet to a PDF. This is this project's
first PDF-*writing* dependency (reportlab) — pdfplumber/pytesseract
elsewhere in this repo only ever read PDFs (Bradford, Legs). Column layout
mirrors the legacy Propertizer tool this project is modeled on: Joint
FMV/Debt/Equity, Before-Tax per party, Tax Basis, After-Tax per party, G/L,
a totals row, an always-shown Tax Rates table, and two equalization
statements (Before-Tax and After-Tax) — all confirmed against a real
Propertizer PDF output, not just the original screenshot (2026-08-14).

Used for both the anytime "Preview" (regenerated live from current draft
state, never touches Clio) and "Save to Clio" (repeatable — a worksheet
stays editable after saving) — same render_worksheet_pdf() call either way,
so what a paralegal previews is byte-for-byte what gets archived to Clio.
"""

from datetime import datetime
from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.pagesizes import landscape, letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from equalizer import calc

INK = colors.HexColor("#12161d")
ACCENT = colors.HexColor("#8a4712")
BORDER = colors.HexColor("#e3ddd0")

RATE_LABELS = {"none": "None", "ordinary": "Ordinary", "lt": "LT Gain", "st": "ST Gain"}


def _money(value: float | None) -> str:
    value = value or 0.0
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):,.2f}"


def _percent(value: float | None) -> str:
    return f"{(value or 0.0) * 100:.1f}%"


def _equalization_statement(label_a: str, label_b: str, amount: float, payer: str | None) -> str:
    if payer is None:
        return "Division is balanced at 50/50 — no equalization payment required."
    payer_label = label_a if payer == "a" else label_b
    payee_label = label_b if payer == "a" else label_a
    return f"{payer_label} pays {payee_label} {_money(amount)} to balance the division at 50/50."


def render_worksheet_pdf(worksheet: dict, items: list[dict]) -> bytes:
    totals = calc.compute_totals(items, worksheet)
    label_a = worksheet["party_a_label"]
    label_b = worksheet["party_b_label"]

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=landscape(letter),
        leftMargin=0.5 * inch, rightMargin=0.5 * inch,
        topMargin=0.5 * inch, bottomMargin=0.5 * inch,
    )
    styles = getSampleStyleSheet()
    story = [
        Paragraph(f"Asset &amp; Debt Equalization — Matter {worksheet['matter_display_number']}", styles["Title"]),
        Paragraph(
            f"{label_a} ({worksheet['party_a_role']}) / {label_b} ({worksheet['party_b_role']}) — "
            f"generated {datetime.today().strftime('%B %d, %Y')}",
            styles["Normal"],
        ),
        Spacer(1, 0.2 * inch),
    ]

    header = [
        "#", "Item", "FMV", "Debt", "Equity",
        f"{label_a} (Before)", f"{label_b} (Before)", "Tax Basis", "Tax Rate", "G/L",
        f"{label_a} (After)", f"{label_b} (After)",
    ]
    rows = [header]
    for idx, item in enumerate(items, start=1):
        eq = calc.equity(item.get("fmv"), item.get("debt"))
        after_a = calc.after_tax(item, worksheet, "a")
        after_b = calc.after_tax(item, worksheet, "b")
        rows.append([
            str(idx), item.get("description") or "",
            _money(item.get("fmv")), _money(item.get("debt")), _money(eq),
            _money(item.get("before_tax_a")), _money(item.get("before_tax_b")),
            _money(item["tax_basis"]) if item.get("tax_basis") is not None else "FMV",
            RATE_LABELS.get(item.get("rate_type", "none"), "None"),
            "Y" if item.get("gain_loss") else "",
            _money(after_a), _money(after_b),
        ])
    rows.append([
        "", "Totals",
        _money(totals.total_fmv), _money(totals.total_debt), _money(totals.total_equity),
        _money(totals.total_before_tax_a), _money(totals.total_before_tax_b), "", "", "",
        _money(totals.total_after_tax_a), _money(totals.total_after_tax_b),
    ])

    table = Table(rows, repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), INK),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("GRID", (0, 0), (-1, -2), 0.5, BORDER),
        ("LINEABOVE", (0, -1), (-1, -1), 1, ACCENT),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ("ALIGN", (2, 0), (-1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    story.append(table)
    story.append(Spacer(1, 0.3 * inch))

    # Always shown, matching the legacy Propertizer tool's own Tax Brackets
    # table — it appears unconditionally there too, even on a worksheet
    # with no G/L rows (confirmed 2026-08-14 against a real Propertizer
    # PDF output), rather than only when a row actually applies a rate.
    story.append(Paragraph("Tax Rates Applied", styles["Heading3"]))
    rate_rows = [
        ["", label_a, label_b],
        ["Federal", _percent(worksheet.get("fed_rate_a")), _percent(worksheet.get("fed_rate_b"))],
        ["State", _percent(worksheet.get("state_rate_a")), _percent(worksheet.get("state_rate_b"))],
        ["Long-Term Capital Gain", _percent(worksheet.get("lt_rate_a")), _percent(worksheet.get("lt_rate_b"))],
        ["Short-Term Capital Gain", _percent(worksheet.get("st_rate_a")), _percent(worksheet.get("st_rate_b"))],
    ]
    rate_table = Table(rate_rows, colWidths=[2.2 * inch, 1.3 * inch, 1.3 * inch])
    rate_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), INK),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("GRID", (0, 0), (-1, -1), 0.5, BORDER),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
    ]))
    story.append(rate_table)
    story.append(Spacer(1, 0.3 * inch))

    # Shown two ways, matching the legacy tool's own Summary page — one
    # equalization figure from Before-Tax totals, one from After-Tax
    # totals. They're identical whenever nothing on the worksheet uses G/L
    # (the common case), but diverge for real once a row has a taxable
    # gain, since the two totals themselves diverge.
    story.append(Paragraph(
        "Before-Tax: " + _equalization_statement(label_a, label_b, totals.equalization_amount, totals.payer),
        styles["Heading3"],
    ))
    story.append(Paragraph(
        "After-Tax: " + _equalization_statement(label_a, label_b, totals.equalization_amount_after_tax, totals.payer_after_tax),
        styles["Heading3"],
    ))

    doc.build(story)
    return buffer.getvalue()
