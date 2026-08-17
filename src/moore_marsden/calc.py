"""
calc.py — Moore/Marsden math: the recursive multi-refinance community-interest
calculation. Pure functions over plain dicts (sqlite3.Row-shaped) — no SQLite
or Clio calls here, so this stays testable without either.

Formula (reverse-engineered from a real legacy report and verified to the
penny against all three of its real segments — see
`tests/test_moore_marsden_calc.py`): each refinance resets the "basis" a new
community-percentage calculation is measured against, the community's prior
cumulative equity (principal + appreciation) carries forward as its stake in
the new segment, and a segment's community-appreciation share floors at $0
when that segment's own appreciation is negative (a depreciation period —
separate property absorbs the loss, not community).

    for each period (refinance or valuation row), in order:
        basis = value at the START of this period (purchase price, or the
                prior period's ending/refi value)
        appreciation = end_value - appreciation_start
            (appreciation_start is normally == basis, except the very first
            period when the property was acquired before marriage — there,
            it's value_at_date_of_marriage instead, so premarital
            appreciation stays separate property; the community-percentage
            denominator is still the original purchase price either way,
            per the doctrinal formula)
        segment_cpr = cumulative_cp_so_far + this_period's_own_paydown
        community_pct = segment_cpr / basis
        community_appreciation = max(0, community_pct * appreciation)
        cumulative_cp_so_far = segment_cpr + community_appreciation

    total_community_interest = cumulative_cp_so_far after the last period
    each_spouse_share = total_community_interest / 2

Staff type each period's own raw principal paydown (from mortgage
statements) — this module carries the running cumulative total forward
itself, rather than requiring staff to hand-sum it before typing it in the
way the legacy Excel tool's preparer had to.
"""

from dataclasses import dataclass

_ORDINALS = {1: "1st", 2: "2nd", 3: "3rd"}


def _ordinal(n: int) -> str:
    return _ORDINALS.get(n, f"{n}th")


def default_segment_label(segment_type: str, refinance_number: int | None = None) -> str:
    """Display label used when a segment's own event_label is blank."""
    if segment_type == "purchase":
        return "Purchase"
    if segment_type == "valuation":
        return "Current Valuation"
    if segment_type == "refinance":
        return f"{_ordinal(refinance_number or 1)} Refinance"
    return segment_type.title()


def _own_paydown(segment: dict) -> float:
    return (segment.get("community_principal_reduction") or 0.0) + (segment.get("cp_contribution") or 0.0)


def compute_segments(worksheet: dict, segments: list[dict]) -> list[dict]:
    """segments must be in position order, starting with the 'purchase' row
    and ending with the 'valuation' row (0+ 'refinance' rows between).
    Returns a new list of dicts — each input segment plus computed fields:
    `basis` (None on the purchase row), `appreciation`, `community_pct`,
    `community_appreciation`, `cumulative_cp` (community interest through
    the end of this row), `sp_total` (property_value - cumulative_cp,
    display-only — reconciles by construction, verified against all 3 real
    segments in the sample report)."""
    if not segments:
        return []

    purchase = segments[0]
    cumulative_cp = purchase.get("cp_contribution") or 0.0
    enriched = [{
        **purchase,
        "basis": None,
        "appreciation": None,
        "community_pct": None,
        "community_appreciation": None,
        "cumulative_cp": cumulative_cp,
        "sp_total": (purchase.get("property_value") or 0.0) - cumulative_cp,
    }]

    basis = purchase.get("property_value") or 0.0
    acquired_before_marriage = bool(worksheet.get("acquired_before_marriage"))
    value_at_marriage = worksheet.get("value_at_date_of_marriage")

    for i, segment in enumerate(segments[1:], start=1):
        end_value = segment.get("property_value") or 0.0

        appreciation_start = basis
        if i == 1 and acquired_before_marriage and value_at_marriage is not None:
            appreciation_start = value_at_marriage

        appreciation = end_value - appreciation_start
        own_paydown = _own_paydown(segment)
        segment_cpr = cumulative_cp + own_paydown
        community_pct = segment_cpr / basis if basis else 0.0
        community_appreciation = max(0.0, community_pct * appreciation)
        cumulative_cp = segment_cpr + community_appreciation

        enriched.append({
            **segment,
            "basis": basis,
            "appreciation": appreciation,
            "community_pct": community_pct,
            "community_appreciation": community_appreciation,
            "cumulative_cp": cumulative_cp,
            "sp_total": end_value - cumulative_cp,
        })
        basis = end_value

    return enriched


@dataclass
class WorksheetTotals:
    total_community_interest: float
    owner_spouse_share: float
    non_owner_spouse_share: float


def compute_final(enriched_segments: list[dict]) -> WorksheetTotals:
    """Each spouse's share is a fixed 50/50 split of the total community
    interest ("each spouse ordinarily receives one-half of that community
    interest"). non_owner_spouse_share is derived as the remainder (not
    independently rounded) so the two always sum exactly to the total,
    including on an odd-cent split — same rounding approach as Equalizer's
    H/W/= split."""
    total = enriched_segments[-1]["cumulative_cp"] if enriched_segments else 0.0
    total = round(total, 2)
    owner_share = round(total / 2, 2)
    return WorksheetTotals(
        total_community_interest=total,
        owner_spouse_share=owner_share,
        non_owner_spouse_share=round(total - owner_share, 2),
    )
