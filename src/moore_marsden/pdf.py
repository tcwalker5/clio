"""
pdf.py — Renders a Moore/Marsden worksheet to a PDF. Layout mirrors the
legacy report this calculation was reverse-engineered from: one "Step"
section per refinance/valuation row (a date/description/amount log, the
community percentage and appreciation for that period, a Community/Separate
Property mini-table), ending with the final community interest and its
50/50 split between the two spouses — a section the legacy report's redacted
excerpt didn't show, but the underlying formula (and Equalizer's own
equalization-payment precedent) calls for as the actually-actionable number.

One deliberate departure from the legacy report's own display: that report
cosmetically showed "0%" for a period with negative appreciation (a
depreciation period), even though the real computed percentage was nonzero
— see calc.py's docstring. This PDF always shows the real computed
percentage rather than zeroing it out for display, on the theory that
showing the true number next to a $0.00 community-appreciation line (clamped
by the max(0, ...) floor) is more legible and auditable than silently
rounding a real figure to zero.

Used for both the anytime "Preview" (regenerated live from current draft
state, never touches Clio) and "Save to Clio" (repeatable — a worksheet
stays editable after saving) — same render_worksheet_pdf() call either way.

capital_improvements (added 2026-08-18) get their own section, listed
individually with source (SP/CP) and treatment (reimbursement/pro-tanto) —
per Marriage of Allen, these need their own record, not a blanket add to
purchase price or an ordinary period's principal reduction. The Community
Interest section shows the reimbursement breakdown (segment chain total +
reimbursements = grand total) whenever any reimbursement-mode improvements
exist, so the final number is never opaque about where it came from.
`worksheet` is expected to already carry live date_of_marriage/
date_of_separation (see routes_moore_marsden.py's _worksheet_with_live_dates)
— this module makes no Clio calls itself.
"""

from datetime import datetime
from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from moore_marsden import calc

INK = colors.HexColor("#12161d")
ACCENT = colors.HexColor("#8a4712")
BORDER = colors.HexColor("#e3ddd0")


def _money(value: float | None) -> str:
    value = value or 0.0
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):,.2f}"


def _percent(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value * 100:.2f}%"


def _log_table(rows: list[tuple[str, str, str]]) -> Table:
    """The Date/Description/Amount log at the top of each Step, matching the
    legacy report's own layout."""
    table = Table([("Date", "Description", "Amount"), *rows], colWidths=[1.1 * inch, 3.3 * inch, 1.6 * inch])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), INK),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("GRID", (0, 0), (-1, -1), 0.5, BORDER),
        ("ALIGN", (2, 0), (2, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    return table


def _cp_sp_table(segment_cpr: float, community_appreciation: float, cumulative_cp: float,
                  sp_total: float, end_value: float) -> Table:
    rows = [
        ("", "CP", "SP"),
        ("Community Principal Reductions", _money(segment_cpr), ""),
        ("Community Appreciation", _money(community_appreciation), ""),
        ("Separate Property Total", "", _money(sp_total)),
        ("Totals", _money(cumulative_cp), _money(end_value - cumulative_cp)),
    ]
    table = Table(rows, colWidths=[2.6 * inch, 1.7 * inch, 1.7 * inch])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), INK),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("GRID", (0, 0), (-1, -1), 0.5, BORDER),
        ("LINEABOVE", (0, -1), (-1, -1), 1, ACCENT),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
    ]))
    return table


_FUNDED_BY_LABELS = {"sp": "Separate Property", "cp": "Community Property"}
_TREATMENT_LABELS = {"reimbursement": "Reimbursement", "pro_tanto": "Pro Tanto"}


def _capital_improvements_table(capital_improvements: list[dict], unbucketed_ids: set) -> Table:
    rows = [("Date", "Description", "Funded By", "Treatment", "Amount")]
    for imp in capital_improvements:
        flag = " (unplaced — not counted)" if imp["id"] in unbucketed_ids else ""
        treatment = _TREATMENT_LABELS.get(imp.get("treatment"), "") if imp.get("funded_by") == "cp" else "—"
        rows.append((
            imp.get("event_date") or "", imp.get("description") or "",
            _FUNDED_BY_LABELS.get(imp.get("funded_by"), ""), treatment + flag,
            _money(imp.get("amount")),
        ))
    table = Table(rows, colWidths=[0.9 * inch, 2.3 * inch, 1.3 * inch, 1.6 * inch, 1.0 * inch])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), INK),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("GRID", (0, 0), (-1, -1), 0.5, BORDER),
        ("ALIGN", (4, 0), (4, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    return table


def render_worksheet_pdf(worksheet: dict, segments: list[dict], capital_improvements: list[dict] | None = None) -> bytes:
    capital_improvements = capital_improvements or []
    enriched = calc.compute_segments(worksheet, segments, capital_improvements)
    totals = calc.compute_final(enriched, capital_improvements)
    unbucketed_ids = {imp["id"] for imp in calc.unbucketed_improvements(segments, capital_improvements)}
    owner_label = worksheet.get("owner_spouse_label") or "Owner Spouse"
    non_owner_label = worksheet.get("non_owner_spouse_label") or "Non-Owner Spouse"

    subtitle = f"{owner_label} (Owner Spouse) / {non_owner_label} (Non-Owner Spouse) — generated {datetime.today().strftime('%B %d, %Y')}"
    date_bits = []
    if worksheet.get("date_of_marriage"):
        date_bits.append(f"Date of Marriage: {worksheet['date_of_marriage']}")
    if worksheet.get("date_of_separation"):
        date_bits.append(f"Date of Separation: {worksheet['date_of_separation']}")

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=letter,
        leftMargin=0.75 * inch, rightMargin=0.75 * inch,
        topMargin=0.6 * inch, bottomMargin=0.6 * inch,
    )
    styles = getSampleStyleSheet()
    story = [
        Paragraph(f"Moore/Marsden Calculation — Matter {worksheet['matter_display_number']}", styles["Title"]),
        Paragraph(subtitle, styles["Normal"]),
    ]
    if date_bits:
        story.append(Paragraph(" &nbsp;&nbsp; ".join(date_bits), styles["Normal"]))
    story.append(Spacer(1, 0.2 * inch))

    purchase = enriched[0]
    purchase_rows = [("", "Purchase Price", _money(purchase.get("property_value")))]
    if worksheet.get("acquired_before_marriage") and worksheet.get("value_at_date_of_marriage") is not None:
        purchase_rows.append(("", "Value at Date of Marriage", _money(worksheet["value_at_date_of_marriage"])))
    if purchase.get("loan_balance") is not None:
        purchase_rows.append(("", "Loan Balance at Purchase", _money(purchase["loan_balance"])))
    if purchase.get("cp_contribution"):
        purchase_rows.append(("", "Community Down Payment", _money(purchase["cp_contribution"])))
    purchase_rows = [(r[0] or (purchase.get("event_date") or ""), r[1], r[2]) for r in purchase_rows]

    story.append(Paragraph("Purchase", styles["Heading2"]))
    story.append(_log_table(purchase_rows))
    story.append(Spacer(1, 0.25 * inch))

    refinance_count = 0
    for i, row in enumerate(enriched[1:], start=1):
        if row["segment_type"] == "refinance":
            refinance_count += 1
            step_label = calc.default_segment_label("refinance", refinance_count)
        else:
            step_label = calc.default_segment_label(row["segment_type"])
        label = row.get("event_label") or step_label

        story.append(Paragraph(f"Step {i}: {label}", styles["Heading2"]))

        log_rows = [(row.get("event_date") or "", "Property Value", _money(row.get("property_value")))]
        if row.get("loan_balance") is not None:
            log_rows.append(("", "Loan Balance", _money(row["loan_balance"])))
        log_rows.append(("", "Community Principal Reduction (this period)", _money(row.get("community_principal_reduction"))))
        if row.get("sp_contribution"):
            log_rows.append(("", "Separate Property Contribution (this period)", _money(row["sp_contribution"])))
        story.append(_log_table(log_rows))
        story.append(Spacer(1, 0.1 * inch))

        story.append(Paragraph(
            f"Appreciation this period: {_money(row['appreciation'])} &nbsp;&nbsp; "
            f"Community percentage: {_percent(row['community_pct'])}",
            styles["Normal"],
        ))
        story.append(Spacer(1, 0.08 * inch))

        segment_cpr = row["cumulative_cp"] - row["community_appreciation"]
        story.append(_cp_sp_table(
            segment_cpr, row["community_appreciation"], row["cumulative_cp"],
            row["sp_total"], row.get("property_value") or 0.0,
        ))
        story.append(Spacer(1, 0.3 * inch))

    if capital_improvements:
        story.append(Paragraph("Capital Improvements", styles["Heading2"]))
        story.append(_capital_improvements_table(capital_improvements, unbucketed_ids))
        if unbucketed_ids:
            story.append(Spacer(1, 0.08 * inch))
            story.append(Paragraph(
                "“Unplaced” improvements above could not be matched to a segment period "
                "(missing or out-of-range date) and are excluded from the total below — fix the date "
                "and re-save to include them.",
                styles["Normal"],
            ))
        story.append(Spacer(1, 0.3 * inch))

    story.append(Paragraph("Community Interest", styles["Heading2"]))
    if totals.reimbursement_total:
        story.append(Paragraph(
            f"Community interest from the purchase/refinance/valuation chain: {_money(totals.segment_chain_total)}",
            styles["Normal"],
        ))
        story.append(Paragraph(
            f"Plus community-funded improvement reimbursements: {_money(totals.reimbursement_total)}",
            styles["Normal"],
        ))
        story.append(Spacer(1, 0.05 * inch))
    story.append(Paragraph(
        f"The community interest in the property is: <b>{_money(totals.total_community_interest)}</b>",
        styles["Normal"],
    ))
    story.append(Spacer(1, 0.1 * inch))
    story.append(Paragraph(
        f"{owner_label}'s share: {_money(totals.owner_spouse_share)} &nbsp;&nbsp; "
        f"{non_owner_label}'s share: {_money(totals.non_owner_spouse_share)}",
        styles["Normal"],
    ))

    doc.build(story)
    return buffer.getvalue()
