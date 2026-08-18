"""
Regression fixture for moore_marsden/calc.py, reproducing the real 3-segment
scenario from the legacy Moore/Marsden report used to reverse-engineer this
formula (data/KC Moore-Marsden final_Redacted.pdf — labels here are generic,
not the client's name; see feedback_redacted_client_names in memory for why).

Figures below are the report's own displayed numbers, confirmed by hand
before this module existed: community_appreciation $5,618.08 (2nd segment)
and $62,881.04 (3rd segment), final community interest $171,686.44. This is
the load-bearing correctness check for a family-law dollar figure, so it's
checked to the penny, not just approximately.
"""

from moore_marsden import calc

SAMPLE_WORKSHEET = {
    "acquired_before_marriage": False,
    "value_at_date_of_marriage": None,
}

# Purchase occurred during marriage (no premarital period), so
# acquired_before_marriage is False and value_at_date_of_marriage never
# gets exercised — see calc.py's docstring for how that branch works when
# it does.
SAMPLE_SEGMENTS = [
    {"segment_type": "purchase", "property_value": 900244.10, "cp_contribution": 0},
    {"segment_type": "refinance", "property_value": 575000.00, "community_principal_reduction": 5131.96},
    {"segment_type": "refinance", "property_value": 770000.00, "community_principal_reduction": 11434.17},
    {"segment_type": "valuation", "property_value": 1215000.00, "community_principal_reduction": 86621.19},
]


def test_reproduces_sample_report_segment_figures():
    enriched = calc.compute_segments(SAMPLE_WORKSHEET, SAMPLE_SEGMENTS)
    assert len(enriched) == 4

    # Segment 1 (purchase -> 1st refi): property value fell, so the
    # community's appreciation share floors at $0 rather than going negative
    # — separate property absorbs the loss, not community.
    assert enriched[1]["appreciation"] == -325244.10
    assert enriched[1]["community_appreciation"] == 0.0

    # Segment 2 (1st refi -> 2nd refi): the report's own displayed figure.
    assert round(enriched[2]["community_appreciation"], 2) == 5618.08

    # Segment 3 (2nd refi -> current valuation): the report's own displayed
    # figure.
    assert round(enriched[3]["community_appreciation"], 2) == 62881.04


def test_reproduces_sample_report_final_total():
    enriched = calc.compute_segments(SAMPLE_WORKSHEET, SAMPLE_SEGMENTS)
    totals = calc.compute_final(enriched)

    assert totals.total_community_interest == 171686.44
    assert totals.owner_spouse_share == 85843.22
    assert totals.non_owner_spouse_share == 85843.22
    # The two shares must always sum exactly to the total, to the penny.
    assert totals.owner_spouse_share + totals.non_owner_spouse_share == totals.total_community_interest


def test_sp_total_reconciles_at_every_segment():
    """SP Total (property_value - cumulative_cp) should always reconcile to
    the property's own value at that point — verified against all 3 real
    segments in the sample report (their own Totals rows: $569,868.04,
    $747,815.79, $1,043,313.56)."""
    enriched = calc.compute_segments(SAMPLE_WORKSHEET, SAMPLE_SEGMENTS)

    assert round(enriched[1]["sp_total"], 2) == 569868.04
    assert round(enriched[2]["sp_total"], 2) == 747815.79
    assert round(enriched[3]["sp_total"], 2) == 1043313.56

    for row in enriched[1:]:
        assert round(row["cumulative_cp"] + row["sp_total"], 2) == round(row["property_value"], 2)


def test_acquired_before_marriage_uses_value_at_marriage_for_appreciation_only():
    """When the property predates the marriage, community percentage still
    anchors to the original purchase price, but appreciation is measured
    from the value at date of marriage — premarital appreciation stays
    separate property."""
    worksheet = {"acquired_before_marriage": True, "value_at_date_of_marriage": 700000.0}
    segments = [
        {"segment_type": "purchase", "property_value": 500000.0, "cp_contribution": 0},
        {"segment_type": "valuation", "property_value": 900000.0, "community_principal_reduction": 20000.0},
    ]

    enriched = calc.compute_segments(worksheet, segments)
    row = enriched[1]

    # Appreciation measured from value-at-marriage (700k), not purchase
    # price (500k): 900k - 700k = 200k.
    assert row["appreciation"] == 200000.0
    # Community percentage still anchored to the original purchase price:
    # segment_cpr (20000) / basis (500000) = 4%.
    assert row["community_pct"] == 0.04
    assert round(row["community_appreciation"], 2) == 8000.0  # 4% of 200k


# --- Capital improvements (added 2026-08-18) ---

IMPROVEMENT_WORKSHEET = {"acquired_before_marriage": False, "value_at_date_of_marriage": None}
IMPROVEMENT_SEGMENTS = [
    {"segment_type": "purchase", "property_value": 500000.0, "cp_contribution": 0, "event_date": "2015-01-01"},
    {"segment_type": "valuation", "property_value": 700000.0, "community_principal_reduction": 0, "event_date": "2020-01-01"},
]


def test_pro_tanto_improvement_shares_in_appreciation():
    """A $50,000 CP-funded pro-tanto improvement, dated inside the only
    period, becomes that period's own_paydown (like cp_contribution) and
    earns a proportional share of the period's appreciation: 50,000/500,000
    = 10% community share x $200,000 appreciation = $20,000, on top of the
    $50,000 itself."""
    improvements = [{"event_date": "2016-01-01", "amount": 50000.0, "funded_by": "cp", "treatment": "pro_tanto"}]
    enriched = calc.compute_segments(IMPROVEMENT_WORKSHEET, IMPROVEMENT_SEGMENTS, improvements)
    totals = calc.compute_final(enriched, improvements)

    row = enriched[1]
    assert row["community_pct"] == 0.1
    assert round(row["community_appreciation"], 2) == 20000.0
    assert totals.segment_chain_total == 70000.0  # 50,000 + 20,000
    assert totals.reimbursement_total == 0.0
    assert totals.total_community_interest == 70000.0
    assert totals.owner_spouse_share == 35000.0


def test_reimbursement_improvement_has_no_appreciation_share():
    """The same $50,000, tagged 'reimbursement' instead, adds flat to the
    final total with zero effect on the segment chain's own math — no
    community_pct, no appreciation share, unlike pro_tanto."""
    improvements = [{"event_date": "2016-01-01", "amount": 50000.0, "funded_by": "cp", "treatment": "reimbursement"}]
    enriched = calc.compute_segments(IMPROVEMENT_WORKSHEET, IMPROVEMENT_SEGMENTS, improvements)
    totals = calc.compute_final(enriched, improvements)

    row = enriched[1]
    assert row["community_pct"] == 0.0
    assert row["community_appreciation"] == 0.0
    assert totals.segment_chain_total == 0.0
    assert totals.reimbursement_total == 50000.0
    assert totals.total_community_interest == 50000.0
    assert totals.owner_spouse_share == 25000.0


def test_sp_funded_improvement_has_zero_effect():
    """An SP-funded improvement to the owner spouse's own separate property
    creates no community interest, regardless of its treatment tag — it's
    logged for the record only."""
    for treatment in ("pro_tanto", "reimbursement"):
        improvements = [{"event_date": "2016-01-01", "amount": 50000.0, "funded_by": "sp", "treatment": treatment}]
        enriched = calc.compute_segments(IMPROVEMENT_WORKSHEET, IMPROVEMENT_SEGMENTS, improvements)
        totals = calc.compute_final(enriched, improvements)
        assert totals.total_community_interest == 0.0, treatment


def test_unbucketed_improvement_excluded_and_flagged():
    """A pro-tanto improvement dated before the purchase date can't be
    placed in any period — it must not silently vanish into the total
    unexplained; unbucketed_improvements() surfaces it for the UI instead."""
    improvements = [{"event_date": "2010-01-01", "amount": 50000.0, "funded_by": "cp", "treatment": "pro_tanto"}]
    enriched = calc.compute_segments(IMPROVEMENT_WORKSHEET, IMPROVEMENT_SEGMENTS, improvements)
    totals = calc.compute_final(enriched, improvements)

    assert totals.total_community_interest == 0.0
    flagged = calc.unbucketed_improvements(IMPROVEMENT_SEGMENTS, improvements)
    assert len(flagged) == 1
    assert flagged[0]["amount"] == 50000.0


def test_spans_separation_flag():
    """A period's date range straddling date_of_separation is flagged, so
    staff know to double check the principal-reduction figure they entered
    only reflects pre-separation community activity."""
    worksheet_with_sep = {**IMPROVEMENT_WORKSHEET, "date_of_separation": "2018-01-01"}
    enriched = calc.compute_segments(worksheet_with_sep, IMPROVEMENT_SEGMENTS)
    assert enriched[1]["spans_separation"] is True

    worksheet_sep_after = {**IMPROVEMENT_WORKSHEET, "date_of_separation": "2021-01-01"}
    enriched_after = calc.compute_segments(worksheet_sep_after, IMPROVEMENT_SEGMENTS)
    assert enriched_after[1]["spans_separation"] is False

    enriched_no_sep = calc.compute_segments(IMPROVEMENT_WORKSHEET, IMPROVEMENT_SEGMENTS)
    assert enriched_no_sep[1]["spans_separation"] is False
