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

Capital improvements (added 2026-08-18) are tracked separately from the
segment chain — per California case law (Marriage of Allen, 96 Cal.App.4th
497 (2002)) a community-funded improvement to separate property needs its
own source/timing/treatment analysis, not a blanket add to purchase price
or an ordinary period's principal reduction. Only funded_by='cp' rows
affect the total at all (an SP-funded improvement to the owner spouse's own
separate property creates no community interest — logged for the record
only). Each CP-funded improvement's `treatment` picks one of two genuinely
different theories, left to staff judgment per item rather than assumed:

- 'reimbursement': the dollar amount is added straight to the FINAL total,
  after the whole segment chain is computed — no appreciation share, since
  the community is just being repaid what it spent.
- 'pro_tanto': folded into whichever segment period contains the
  improvement's event_date, exactly like that segment's own cp_contribution
  — so it shares proportionally in appreciation from that point forward,
  the same way ordinary principal reduction does.
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


def _own_paydown(segment: dict, pro_tanto_this_period: float = 0.0) -> float:
    return (
        (segment.get("community_principal_reduction") or 0.0)
        + (segment.get("cp_contribution") or 0.0)
        + pro_tanto_this_period
    )


def _bucket_period_index(segments: list[dict], event_date: str | None) -> int | None:
    """Returns i (1 <= i < len(segments)) such that event_date falls within
    the period ending at segments[i] (i.e., after segments[i-1]'s date and
    on/before segments[i]'s date) — or None if it can't be determined
    (missing event_date, or an earlier segment is missing its own date,
    which breaks the walk). Segment dates are ISO "YYYY-MM-DD" strings,
    which compare correctly as plain strings."""
    if not event_date:
        return None
    result = None
    for i in range(1, len(segments)):
        start_date = segments[i - 1].get("event_date")
        if not start_date or start_date > event_date:
            break
        result = i
    return result


def _pro_tanto_by_period(segments: list[dict], capital_improvements: list[dict]) -> dict[int, float]:
    totals: dict[int, float] = {}
    for imp in capital_improvements:
        if imp.get("funded_by") != "cp" or imp.get("treatment") != "pro_tanto":
            continue
        idx = _bucket_period_index(segments, imp.get("event_date"))
        if idx is None:
            continue
        totals[idx] = totals.get(idx, 0.0) + (imp.get("amount") or 0.0)
    return totals


def unbucketed_improvements(segments: list[dict], capital_improvements: list[dict]) -> list[dict]:
    """Pro-tanto CP-funded improvements whose event_date couldn't be placed
    into any segment period (missing date, or an earlier segment's own date
    is missing) — these are silently excluded from the total rather than
    guessed at, so the UI should surface them explicitly rather than let
    their dollar amount just vanish unexplained."""
    return [
        imp for imp in capital_improvements
        if imp.get("funded_by") == "cp" and imp.get("treatment") == "pro_tanto"
        and _bucket_period_index(segments, imp.get("event_date")) is None
    ]


def compute_segments(worksheet: dict, segments: list[dict], capital_improvements: list[dict] | None = None) -> list[dict]:
    """segments must be in position order, starting with the 'purchase' row
    and ending with the 'valuation' row (0+ 'refinance' rows between).
    Returns a new list of dicts — each input segment plus computed fields:
    `basis` (None on the purchase row), `appreciation`, `community_pct`,
    `community_appreciation`, `cumulative_cp` (community interest through
    the end of this row, including any pro-tanto capital improvements
    bucketed into this period), `sp_total` (property_value - cumulative_cp,
    display-only — reconciles by construction, verified against all 3 real
    segments in the sample report), and `spans_separation` (True if this
    period's date range straddles worksheet['date_of_separation'] — a hint
    that the entered principal-reduction figure may be mixing pre- and
    post-separation activity, since post-separation earnings are generally
    separate property)."""
    if not segments:
        return []

    capital_improvements = capital_improvements or []
    pro_tanto_by_period = _pro_tanto_by_period(segments, capital_improvements)

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
        "spans_separation": False,
    }]

    basis = purchase.get("property_value") or 0.0
    acquired_before_marriage = bool(worksheet.get("acquired_before_marriage"))
    value_at_marriage = worksheet.get("value_at_date_of_marriage")
    date_of_separation = worksheet.get("date_of_separation")

    for i, segment in enumerate(segments[1:], start=1):
        end_value = segment.get("property_value") or 0.0
        start_date = segments[i - 1].get("event_date")
        end_date = segment.get("event_date")

        appreciation_start = basis
        if i == 1 and acquired_before_marriage and value_at_marriage is not None:
            appreciation_start = value_at_marriage

        appreciation = end_value - appreciation_start
        own_paydown = _own_paydown(segment, pro_tanto_by_period.get(i, 0.0))
        segment_cpr = cumulative_cp + own_paydown
        community_pct = segment_cpr / basis if basis else 0.0
        community_appreciation = max(0.0, community_pct * appreciation)
        cumulative_cp = segment_cpr + community_appreciation
        spans_separation = bool(
            date_of_separation and start_date and end_date
            and start_date < date_of_separation < end_date
        )

        enriched.append({
            **segment,
            "basis": basis,
            "appreciation": appreciation,
            "community_pct": community_pct,
            "community_appreciation": community_appreciation,
            "cumulative_cp": cumulative_cp,
            "sp_total": end_value - cumulative_cp,
            "spans_separation": spans_separation,
        })
        basis = end_value

    return enriched


@dataclass
class WorksheetTotals:
    total_community_interest: float
    owner_spouse_share: float
    non_owner_spouse_share: float
    segment_chain_total: float
    reimbursement_total: float


def compute_final(enriched_segments: list[dict], capital_improvements: list[dict] | None = None) -> WorksheetTotals:
    """Each spouse's share is a fixed 50/50 split of the total community
    interest ("each spouse ordinarily receives one-half of that community
    interest"). non_owner_spouse_share is derived as the remainder (not
    independently rounded) so the two always sum exactly to the total,
    including on an odd-cent split — same rounding approach as Equalizer's
    H/W/= split.

    Reimbursement-mode CP-funded capital improvements are added here, flat,
    AFTER the segment chain's own cumulative_cp is final — never folded into
    the chain itself, since doing so would let them pick up a proportional
    share of *later* appreciation the next time compute_segments recomputes
    community_pct (that would be the pro_tanto theory, not reimbursement).
    segment_chain_total/reimbursement_total are both exposed so the UI/PDF
    can show the breakdown rather than just a single opaque number."""
    capital_improvements = capital_improvements or []
    segment_chain_total = enriched_segments[-1]["cumulative_cp"] if enriched_segments else 0.0
    reimbursement_total = sum(
        imp.get("amount") or 0.0
        for imp in capital_improvements
        if imp.get("funded_by") == "cp" and imp.get("treatment") == "reimbursement"
    )

    total = round(segment_chain_total + reimbursement_total, 2)
    owner_share = round(total / 2, 2)
    return WorksheetTotals(
        total_community_interest=total,
        owner_spouse_share=owner_share,
        non_owner_spouse_share=round(total - owner_share, 2),
        segment_chain_total=round(segment_chain_total, 2),
        reimbursement_total=round(reimbursement_total, 2),
    )
