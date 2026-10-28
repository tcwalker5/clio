"""
Tests for Clio event-type ownership and double-booking (court_calendar/schedule.py,
plus compare_events(check_types=True)).

Real data behind these (2026-10-26): every entry is on the shared "Firm"
calendar, so the event type (Heidi / Dahann / Pam) is the only signal of whose
it is. The court lists Heidi on every hearing, but Dahann or Pam often cover —
their type on the entry is who actually appears. Live examples: Heidi has two hearings at 9:00 on 12/10 (MARTZ in NC-18,
CHAMBERS in N-19) — a real double-booking; Dahann's ROSS FRC and ROSS RFO
share 1:45 PM on 12/17 — the same matter, so not a conflict. Clio-only entries
such as a doctor's appointment must count against the attorney's time too.
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from court_calendar.clio_calendar import CalendarEntry
from court_calendar.matcher import compare_events
from court_calendar.normalizer import CourtEvent
from court_calendar.schedule import find_conflicts

PACIFIC = ZoneInfo("America/Los_Angeles")
HEIDI, DAHANN, STAFF = (591618, "Heidi"), (591633, "Dahann"), (591663, "Staff")
PURPOSE_MAPPINGS = {"FAMILY RESOLUTI": "FRC", "REQUEST FOR ORD": "RFO"}


def _court_event(party, hour=9, minute=0, purpose_raw="Family Resoluti", dept="N-17", attorney="Heidi D. Collier, ESQ"):
    dt = datetime(2026, 12, 10, hour, minute, tzinfo=PACIFIC)
    return CourtEvent(
        date="2026-12-10", start_time=dt.strftime("%H:%M"), dt=dt, dept_raw=dept.replace("-", ""), dept=dept,
        judge="", purpose_raw=purpose_raw, case_number=f"CASE-{party.upper()}", party=party, party_role="P",
        attorney=attorney, raw_line="",
    )


def _entry(entry_id, summary, event_type, matter_id=None, hour=9, minute=0, minutes=120, all_day=False):
    start = datetime(2026, 12, 10, hour, minute, tzinfo=PACIFIC)
    return CalendarEntry(
        id=entry_id, summary=summary, description="", location="", start_at=start,
        end_at=start + timedelta(minutes=minutes), all_day=all_day, matter_id=matter_id,
        matter_display_number=None, calendar_owner_name="Firm",
        event_type_id=event_type[0] if event_type else None,
        event_type_name=event_type[1] if event_type else None,
    )


def _compare(court_events, entries, matters_by_name):
    case_numbers = {ids[0]: f"CASE-{name}" for name, ids in matters_by_name.items()}
    return compare_events(court_events, entries, matters_by_name, PURPOSE_MAPPINGS, case_numbers, check_types=True)


def test_hearing_covered_by_another_attorney_is_not_flagged():
    [r] = _compare([_court_event("Martz")], [_entry(1, "MARTZ FRC", DAHANN, matter_id=10)], {"MARTZ": [10]})
    assert r.reason == ""


def test_untyped_and_staff_entries_are_flagged():
    for event_type, shown in ((None, "no type set"), (STAFF, "Staff")):
        [r] = _compare([_court_event("Martz")], [_entry(1, "MARTZ FRC", event_type, matter_id=10)], {"MARTZ": [10]})
        assert r.reason == "No attorney type"
        assert r.changes == [{"court": "Heidi", "clio": shown}]


def test_entry_with_the_right_type_is_preferred():
    entries = [_entry(1, "MARTZ FRC", DAHANN, matter_id=10), _entry(2, "MARTZ FRC", HEIDI, matter_id=10)]
    [r] = _compare([_court_event("Martz")], entries, {"MARTZ": [10]})
    assert r.clio_entry.id == 2
    assert r.reason == ""


def test_attorney_outside_the_mapping_is_not_type_checked():
    [r] = _compare([_court_event("Martz", attorney="JANE OTHERLAWYER, ESQ")],
                   [_entry(1, "MARTZ FRC", None, matter_id=10)], {"MARTZ": [10]})
    assert r.reason == ""


def test_two_hearings_same_time_different_matters_conflict():
    court = [_court_event("Martz", dept="NC-18"), _court_event("Chambers", dept="N-19")]
    entries = [_entry(1, "MARTZ FRC", HEIDI, matter_id=10), _entry(2, "CHAMBERS FRC", HEIDI, matter_id=20, minutes=150)]
    results = _compare(court, entries, {"MARTZ": [10], "CHAMBERS": [20]})
    [c] = find_conflicts(results, entries)
    assert c.owner == "Heidi"
    assert {c.first.source, c.second.source} == {"Court"}


def test_clio_only_appointment_overlapping_a_hearing_conflicts():
    court = [_court_event("Martz")]
    entries = [_entry(1, "MARTZ FRC", HEIDI, matter_id=10), _entry(2, "Dr. appointment", HEIDI, hour=10, minutes=60)]
    results = _compare(court, entries, {"MARTZ": [10]})
    [c] = find_conflicts(results, entries)
    assert (c.first.source, c.second.source) == ("Court", "Clio only")
    assert c.second.label == "Dr. appointment"


def test_two_clio_only_entries_overlapping_conflict():
    entries = [_entry(1, "Dr. appointment", HEIDI, hour=14, minutes=60),
               _entry(2, "Client meeting", HEIDI, matter_id=30, hour=14, minute=30, minutes=60)]
    assert len(find_conflicts([], entries)) == 1


def test_hearing_missing_from_clio_uses_default_length():
    # No Clio entry for the 9:00 hearing: counted as 9:00-10:00, so a 10:00
    # appointment touches it without overlapping, a 9:45 one overlaps.
    court = [_court_event("Martz")]
    touching = [_entry(2, "Dr. appointment", HEIDI, hour=10, minutes=60)]
    overlapping = [_entry(2, "Dr. appointment", HEIDI, hour=9, minute=45, minutes=60)]
    assert find_conflicts(_compare(court, touching, {"MARTZ": [10]}), touching) == []
    assert len(find_conflicts(_compare(court, overlapping, {"MARTZ": [10]}), overlapping)) == 1


def test_not_conflicts_same_matter_other_attorney_all_day_and_staff():
    court = [_court_event("Ross"), _court_event("Ross", purpose_raw="Request For Ord")]
    entries = [
        _entry(1, "ROSS FRC", HEIDI, matter_id=40),
        _entry(2, "ROSS RFO", HEIDI, matter_id=40),               # same matter: combined appearance
        _entry(3, "Deposition", DAHANN, matter_id=50),            # someone else's time
        _entry(4, "L/D to file reply", HEIDI, all_day=True),      # deadline, not a block of time
        _entry(5, "Staff meeting", STAFF),                        # Staff is out of scope
    ]
    results = _compare(court, entries, {"ROSS": [40]})
    assert find_conflicts(results, entries) == []


def test_covered_hearing_counts_against_the_covering_attorney():
    # Court says Heidi for both, but Dahann is covering Chambers: no clash for
    # Heidi; Dahann's doctor appointment clashes with Chambers instead.
    court = [_court_event("Martz"), _court_event("Chambers", dept="N-19")]
    entries = [_entry(1, "MARTZ FRC", HEIDI, matter_id=10), _entry(2, "CHAMBERS FRC", DAHANN, matter_id=20),
               _entry(3, "Dr. appointment", DAHANN, hour=10, minutes=60)]
    results = _compare(court, entries, {"MARTZ": [10], "CHAMBERS": [20]})
    [c] = find_conflicts(results, entries)
    assert c.owner == "Dahann"
    assert {c.first.label, c.second.label} == {"Chambers Family Resoluti (N-19)", "Dr. appointment"}
