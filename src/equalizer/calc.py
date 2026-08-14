"""
calc.py — Equalizer math: equity, after-tax, and the 50/50 equalization
payment. Pure functions over plain dicts (sqlite3.Row-shaped) — no SQLite or
Clio calls here, so this stays testable without either.

Equalization target is fixed at 50/50 (firm decision, 2026-08-12) — and,
confirmed 2026-08-14 against a real legacy Propertizer PDF output (not just
the earlier screenshot, which never had a case with real After-Tax
divergence to check against), Propertizer computes it **two ways**: once
from Before-Tax totals, once from After-Tax totals, shown side by side even
when they're identical (that PDF's own G/L column was blank on every row,
so both numbers happened to match — the two-column layout is still always
there). `compute_totals()` below does the same — `equalization_amount`/
`payer` from Before-Tax totals, `equalization_amount_after_tax`/
`payer_after_tax` from After-Tax totals — and both get shown, in the PDF
and on-screen, rather than picking one.

After-Tax formula (confirmed with Ted, 2026-08-12):
    After-Tax = Before-Tax - (unrealized gain x rate)
    unrealized gain = FMV - Tax Basis
apportioned to each party in proportion to their Before-Tax share of the
item's equity — a party only carries the tax hit on the share they're
actually receiving. That apportionment is this code's own interpretation of
Ted's plain-language formula, not something he specified at that level of
detail — worth confirming against a real worksheet before relying on it for
an actual filing.
"""

from dataclasses import dataclass

RATE_TYPES = ("none", "ordinary", "lt", "st")


def equity(fmv: float | None, debt: float | None) -> float:
    return (fmv or 0.0) - (debt or 0.0)


def unrealized_gain(fmv: float | None, tax_basis: float | None) -> float:
    """No Tax Basis entered means 'sold at FMV, no gain' (matches the legacy
    tool's default 'FMV' basis label) — not an error, not zero-by-omission."""
    if tax_basis is None:
        return 0.0
    return (fmv or 0.0) - tax_basis


def _rate_for(worksheet: dict, party: str, rate_type: str) -> float:
    suffix = "_a" if party == "a" else "_b"
    if rate_type == "ordinary":
        return (worksheet.get(f"fed_rate{suffix}") or 0.0) + (worksheet.get(f"state_rate{suffix}") or 0.0)
    if rate_type == "lt":
        return worksheet.get(f"lt_rate{suffix}") or 0.0
    if rate_type == "st":
        return worksheet.get(f"st_rate{suffix}") or 0.0
    return 0.0


def after_tax(item: dict, worksheet: dict, party: str) -> float:
    """party is 'a' or 'b'. Returns the manual override if one was typed in
    (after_tax_a/b is non-NULL), otherwise the computed figure — a manual
    entry always wins, same "never silently overwrite" rule as everywhere
    else in this project."""
    override = item.get(f"after_tax_{party}")
    if override is not None:
        return override

    before = item.get(f"before_tax_{party}") or 0.0
    if not item.get("gain_loss") or item.get("rate_type", "none") == "none":
        return before

    fmv = item.get("fmv") or 0.0
    debt = item.get("debt") or 0.0
    eq = equity(fmv, debt)
    gain = unrealized_gain(fmv, item.get("tax_basis"))
    if eq == 0 or gain <= 0:
        return before

    party_gain_share = gain * (before / eq)
    rate = _rate_for(worksheet, party, item.get("rate_type", "none"))
    return before - party_gain_share * rate


@dataclass
class WorksheetTotals:
    total_fmv: float
    total_debt: float
    total_equity: float
    total_before_tax_a: float
    total_before_tax_b: float
    total_after_tax_a: float
    total_after_tax_b: float
    equalization_amount: float
    payer: str | None  # 'a', 'b', or None if already balanced
    equalization_amount_after_tax: float
    payer_after_tax: str | None


def _equalization(total_a: float, total_b: float) -> tuple[float, str | None]:
    imbalance = round(total_a - total_b, 2)
    amount = round(abs(imbalance) / 2, 2)
    payer = None if imbalance == 0 else ("a" if imbalance > 0 else "b")
    return amount, payer


def compute_totals(items: list[dict], worksheet: dict) -> WorksheetTotals:
    total_fmv = sum(i.get("fmv") or 0.0 for i in items)
    total_debt = sum(i.get("debt") or 0.0 for i in items)
    total_before_a = sum(i.get("before_tax_a") or 0.0 for i in items)
    total_before_b = sum(i.get("before_tax_b") or 0.0 for i in items)
    total_after_a = sum(after_tax(i, worksheet, "a") for i in items)
    total_after_b = sum(after_tax(i, worksheet, "b") for i in items)

    equalization_amount, payer = _equalization(total_before_a, total_before_b)
    equalization_amount_after_tax, payer_after_tax = _equalization(total_after_a, total_after_b)

    return WorksheetTotals(
        total_fmv=total_fmv,
        total_debt=total_debt,
        total_equity=total_fmv - total_debt,
        total_before_tax_a=total_before_a,
        total_before_tax_b=total_before_b,
        total_after_tax_a=total_after_a,
        total_after_tax_b=total_after_b,
        equalization_amount=equalization_amount,
        payer=payer,
        equalization_amount_after_tax=equalization_amount_after_tax,
        payer_after_tax=payer_after_tax,
    )


def enrich_item(item: dict, worksheet: dict) -> dict:
    """Item dict plus display-only computed fields: equity, and the
    computed After-Tax figure for each side regardless of whether a manual
    override is set. Lets the UI show the actual auto-computed number in a
    muted style when after_tax_a/b is NULL, instead of a bare "auto"
    placeholder with no number behind it — the raw after_tax_a/b fields
    (possibly NULL) are left untouched so the override-vs-auto distinction
    is still there for whoever consumes this."""
    enriched = dict(item)
    enriched["equity"] = equity(item.get("fmv"), item.get("debt"))
    enriched["after_tax_a_computed"] = after_tax(item, worksheet, "a")
    enriched["after_tax_b_computed"] = after_tax(item, worksheet, "b")
    enriched["after_tax_a_is_auto"] = item.get("after_tax_a") is None
    enriched["after_tax_b_is_auto"] = item.get("after_tax_b") is None
    return enriched


def assign_button_labels(worksheet: dict) -> tuple[str, str]:
    """Text for the H/W toolbar buttons: literal 'H'/'W' in husband_wife
    mode (matches the legacy Propertizer tool), or each party's first
    initial in first_names mode — falling back to the full first name on
    both buttons if the initials would collide (e.g. Alex and Amanda both
    start with 'A'). A single shared letter on two side-by-side buttons is
    genuinely ambiguous, not just less descriptive, so this widens only
    when it actually needs to rather than guessing at a two-letter
    abbreviation that could still collide (Alex/Alexis)."""
    if worksheet.get("naming_mode") != "first_names":
        return "H", "W"

    label_a = (worksheet.get("party_a_label") or "").strip()
    label_b = (worksheet.get("party_b_label") or "").strip()
    initial_a = label_a[:1].upper()
    initial_b = label_b[:1].upper()

    if initial_a and initial_b and initial_a != initial_b:
        return initial_a, initial_b
    return label_a or "A", label_b or "B"


def assign_before_tax(fmv: float, debt: float, side: str) -> tuple[float, float]:
    """H/W/= toolbar buttons: 'a' -> full equity to party A, 'b' -> full to
    party B, 'split' -> half each. The two returned values always sum
    exactly to equity, including on an odd-cent split (e.g. -3,069 split as
    -1,535 / -1,534) — matches the legacy tool's own observed rounding."""
    eq = equity(fmv, debt)
    if side == "a":
        return eq, 0.0
    if side == "b":
        return 0.0, eq
    if side == "split":
        half = round(eq / 2, 2)
        return half, round(eq - half, 2)
    raise ValueError(f"Unknown assignment side: {side!r}")
