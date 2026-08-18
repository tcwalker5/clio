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
    for r in results:
        assert r.status == "to_update"
        assert "Purpose mismatch" in r.reason


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
