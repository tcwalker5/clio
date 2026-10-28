"""
Regression test for court_calendar/matcher.py's combined-appearance handling.

Real example (2026-08-18, matter Bassett, case 26FL001421N): the court
calendar lists a DVRO hearing and an FRC (Family Resolution Conference) at
the same date/time/dept, entered in Clio as ONE calendar entry (staff
convention for a combined appearance — common, not a data-entry gap). Before
this fix, compare_events() treated Clio entries as one-hearing-each: the
first-processed event claimed the entry exclusively, producing a false
"Purpose mismatch" on it and a false "No calendar event" (missing) on the
other, even though nothing was actually wrong in Clio.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from court_calendar.clio_calendar import CalendarEntry
from court_calendar.matcher import compare_events
from court_calendar.normalizer import CourtEvent

PACIFIC = ZoneInfo("America/Los_Angeles")
MATTER_ID = 1786800000
CASE_NUMBER = "26FL001421N"


def _court_event(purpose_raw: str) -> CourtEvent:
    dt = datetime(2026, 8, 18, 9, 0, tzinfo=PACIFIC)
    return CourtEvent(
        date="2026-08-18",
        start_time="09:00",
        dt=dt,
        dept_raw="N17",
        dept="N-17",
        judge="CATHERINE A. RICHARDSON",
        purpose_raw=purpose_raw,
        case_number=CASE_NUMBER,
        party="Bassett",
        party_role="P",
        attorney="Heidi D. Collier, ESQ",
        raw_line=f"08/18/26 09:00AM N-17    DM CATHERINE A. RICHARDSON        {purpose_raw} {CASE_NUMBER}",
    )


def _clio_entry(summary: str) -> CalendarEntry:
    return CalendarEntry(
        id=555,
        summary=summary,
        description="",
        location="",
        start_at=datetime(2026, 8, 18, 9, 0, tzinfo=PACIFIC),
        end_at=None,
        all_day=False,
        matter_id=MATTER_ID,
        matter_display_number="BASSETT, JOHN",
        calendar_owner_name=None,
        event_type_id=None,
        event_type_name=None,
    )


MATTERS_BY_NAME = {"BASSETT": [MATTER_ID]}
CASE_NUMBERS_BY_MATTER = {MATTER_ID: CASE_NUMBER}
PURPOSE_MAPPINGS = {"RESTRAINING ORD": "DVRO", "FAMILY RESOLUTI": "FRC"}


def test_two_hearings_sharing_one_clio_entry_both_match_cleanly():
    court_events = [_court_event("Restraining Ord"), _court_event("Family Resoluti")]
    clio_entries = [_clio_entry("DVRO hearing")]  # entry only names one of the two purposes

    results = compare_events(
        court_events, clio_entries, MATTERS_BY_NAME, PURPOSE_MAPPINGS, CASE_NUMBERS_BY_MATTER,
    )

    assert len(results) == 2
    for r in results:
        assert r.status == "matched", r.reason
        assert r.reason == ""
        assert r.clio_entry is not None
        assert r.clio_entry.id == 555


def test_entry_matching_neither_purpose_still_flags_mismatch():
    court_events = [_court_event("Restraining Ord"), _court_event("Family Resoluti")]
    clio_entries = [_clio_entry("Trial Setting Conference")]  # unrelated purpose

    results = compare_events(
        court_events, clio_entries, MATTERS_BY_NAME, PURPOSE_MAPPINGS, CASE_NUMBERS_BY_MATTER,
    )

    assert len(results) == 2
    dvro_result = next(r for r in results if r.court_event.purpose_raw == "Restraining Ord")
    frc_result = next(r for r in results if r.court_event.purpose_raw == "Family Resoluti")

    for r in results:
        assert r.status == "to_update"
        assert "Purpose mismatch" in r.reason

    # Structured court/clio pairs, not pre-joined sentences — the template
    # renders each as separate "Court:"/"Clio:" lines.
    assert {"court": "DVRO", "clio": "TRIAL, TRIAL SETTING CONFERENCE"} in dvro_result.changes
    assert {"court": "FRC", "clio": "TRIAL, TRIAL SETTING CONFERENCE"} in frc_result.changes


def test_two_hearings_with_two_separate_entries_still_pair_one_to_one():
    court_events = [_court_event("Restraining Ord"), _court_event("Family Resoluti")]
    dvro_entry = _clio_entry("DVRO hearing")
    dvro_entry.id = 601
    frc_entry = _clio_entry("FRC hearing")
    frc_entry.id = 602
    clio_entries = [dvro_entry, frc_entry]

    results = compare_events(
        court_events, clio_entries, MATTERS_BY_NAME, PURPOSE_MAPPINGS, CASE_NUMBERS_BY_MATTER,
    )

    assert len(results) == 2
    matched_ids = {r.clio_entry.id for r in results if r.clio_entry}
    assert matched_ids == {601, 602}
    for r in results:
        assert r.status == "matched", r.reason


# --- Court party isn't our client (2026-10-26) --------------------------------
# The court calendar's party is the petitioner, last name only. When we
# represent the respondent, that's the opposing party, so no client name
# matches; the case number or the matter's Opposing Party contact must.

from outlook_calendar.relationships import OcOpContact  # noqa: E402

OTHER_MATTER_ID = 1786800099


def _op(name, matter_id=MATTER_ID, display="BASSETT, JOHN"):
    return OcOpContact(name=name, role="OP", raw_description="Opposing Party", matter_id=matter_id,
                       matter_display_number=display, matter_status="Open")


def _petitioner_event(party):
    ce = _court_event("Family Resoluti")
    ce.party = party
    return ce


def test_case_number_finds_matter_when_party_is_not_our_client():
    results = compare_events(
        [_petitioner_event("Ramirez")], [_clio_entry("FRC hearing")], MATTERS_BY_NAME, PURPOSE_MAPPINGS,
        CASE_NUMBERS_BY_MATTER,
    )
    [r] = results
    assert r.matter_id == MATTER_ID
    assert r.match_method == "case_number"
    assert r.status == "matched", r.reason


def test_case_number_beats_a_coincidental_client_name():
    # "Bassett" is also the last name of another client, but the case number
    # is on file on a different matter.
    matters = {"BASSETT": [OTHER_MATTER_ID], "RAMIREZ": [MATTER_ID]}
    case_numbers = {MATTER_ID: CASE_NUMBER, OTHER_MATTER_ID: "25FL000001N"}
    [r] = compare_events([_court_event("Family Resoluti")], [_clio_entry("FRC hearing")], matters,
                         PURPOSE_MAPPINGS, case_numbers)
    assert r.matter_id == MATTER_ID
    assert r.match_method == "case_number"


def test_opposing_party_finds_matter_without_a_case_number_on_file():
    [r] = compare_events(
        [_petitioner_event("Ramirez")], [_clio_entry("FRC hearing")], MATTERS_BY_NAME, PURPOSE_MAPPINGS,
        {MATTER_ID: ""}, opposing_parties=[_op("MARIA RAMIREZ"), _op("JOHN RAMIREZ-SOTO")],
    )
    assert r.matter_id == MATTER_ID
    assert r.match_method == "opposing_party"
    assert "MARIA RAMIREZ" in r.match_note
    assert r.reason == "Case number not on file"


def test_opposing_party_on_two_matters_is_ambiguous_and_lists_both():
    ops = [_op("PRIYA JOHNSON", 1, "COHEN, SEAN"), _op("SHAUNN JOHNSON", 2, "SAWKA, JESSICA")]
    [r] = compare_events([_petitioner_event("JOHNSON")], [], MATTERS_BY_NAME, PURPOSE_MAPPINGS,
                         {1: "", 2: ""}, opposing_parties=ops)
    assert r.reason == "Ambiguous matter"
    assert "COHEN, SEAN" in r.changes[0]["detail"] and "SAWKA, JESSICA" in r.changes[0]["detail"]


def test_opposing_party_needs_a_whole_word_match():
    [r] = compare_events([_petitioner_event("Lee")], [], MATTERS_BY_NAME, PURPOSE_MAPPINGS,
                         {MATTER_ID: ""}, opposing_parties=[_op("KATHLEEN KLEEN")])
    assert r.reason == "No matter in Clio"


def test_opposing_party_on_two_matters_narrowed_by_entry_that_day():
    ops = [_op("PRIYA JOHNSON", 1, "COHEN, SEAN"), _op("SHAUNN JOHNSON", MATTER_ID, "SAWKA, JESSICA")]
    [r] = compare_events([_petitioner_event("JOHNSON")], [_clio_entry("Sawka OP XP FRC")], MATTERS_BY_NAME,
                         PURPOSE_MAPPINGS, {1: "", MATTER_ID: ""}, opposing_parties=ops)
    assert r.matter_id == MATTER_ID
    assert r.match_method == "opposing_party"
    assert "SHAUNN JOHNSON" in r.match_note
    assert r.reason == "Case number not on file"


def test_unmatched_party_suggests_entry_at_same_time_and_dept():
    ce = _petitioner_event("Lorraine")
    ce.case_number = "26FL009712N"
    entry = _clio_entry("Ahlquist FRC (N17)")
    entry.matter_display_number = "AHLQUIST, LORRAINE"
    elsewhere = _clio_entry("Other FRC (N19)")  # same time, other courtroom
    elsewhere.id, elsewhere.matter_id = 556, OTHER_MATTER_ID
    [r] = compare_events([ce], [entry, elsewhere], MATTERS_BY_NAME, PURPOSE_MAPPINGS, {MATTER_ID: ""})
    assert r.reason == "No matter in Clio"
    hints = [c["detail"] for c in r.changes if c["detail"].startswith("Possible match")]
    assert len(hints) == 1 and "AHLQUIST, LORRAINE" in hints[0]
