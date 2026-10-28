"""
schedule.py — Who owns a court hearing in Clio, and who's booked twice.

Clio is the master calendar. Ownership is the calendar entry's *event type*
(calendar_entry_event_type), not calendar_owner — every entry sits on the
shared "Firm" calendar, so calendar_owner is always "Firm". Each attorney has
their own type; "Staff" is a catch-all for everyone else and deliberately
not mapped here (out of scope for ownership and conflicts).

The court calendar's attorney field is the attorney of record, not who
appears: live check 2026-10-26, all 68 upcoming hearings listed Heidi, while
26 of their Clio entries were typed Dahann or Pam (covering for her). So the
Clio type decides who a hearing belongs to; the court attorney is only the
fallback when the hearing has no typed Clio entry.

find_conflicts() lays out each attorney's day from two sources and flags
any two items that overlap in time on different matters:
  - court hearings (spanning their matched Clio entry's times, or
    COURT_EVENT_DEFAULT_MINUTES when Clio has no entry yet)
  - Clio entries of that attorney's type that no hearing matched — personal
    appointments (doctor, etc.), office meetings, hearings missing from the
    court calendar
All-day entries (L/D deadline reminders) never block time and are skipped.
Overlaps on the same matter are skipped too: a combined appearance (e.g. an
FRC and an RFO heard together) is one trip to court, not a conflict.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from court_calendar.clio_calendar import CalendarEntry
from court_calendar.court_fetch import is_target_attorney
from court_calendar.normalizer import CourtEvent

PACIFIC = ZoneInfo("America/Los_Angeles")

# Staff name (as in the /calendar attorney dropdown) -> Clio event type
# (id, name), ids from /calendar_entry_event_types.json, checked 2026-10-26.
ATTORNEY_EVENT_TYPES: dict[str, tuple[int, str]] = {
    "Heidi Collier": (591618, "Heidi"),
    "Dahann Bowers": (591633, "Dahann"),
    "Pamela Bradford": (591648, "Pam"),
}

# Court calendar lines carry a start time only. Used for a hearing with no
# Clio entry yet; matched hearings use the Clio entry's own start/end.
COURT_EVENT_DEFAULT_MINUTES = 60
# Clio entries saved with no end time (or end == start).
CLIO_ENTRY_DEFAULT_MINUTES = 30


ATTORNEY_TYPE_IDS = {type_id for type_id, _ in ATTORNEY_EVENT_TYPES.values()}


def expected_event_type(court_event: CourtEvent) -> tuple[int, str] | None:
    """The court calendar's attorney of record as a Clio event type — None
    for attorneys outside the mapping."""
    for name, event_type in ATTORNEY_EVENT_TYPES.items():
        if is_target_attorney(court_event.attorney, name):
            return event_type
    return None


@dataclass
class Booking:
    owner: str  # event type name, e.g. "Heidi"
    start: datetime
    end: datetime
    label: str
    source: str  # "Court" (on the court calendar) | "Clio only"
    matter_id: int | None
    clio_entry_id: int | None

    @property
    def time_range(self) -> str:
        fmt = lambda dt: dt.astimezone(PACIFIC).strftime("%I:%M %p").lstrip("0")
        return f"{fmt(self.start)}–{fmt(self.end)}"


@dataclass
class Conflict:
    owner: str
    first: Booking
    second: Booking

    @property
    def date(self) -> str:
        return self.first.start.astimezone(PACIFIC).strftime("%Y-%m-%d")


def _entry_span(entry: CalendarEntry) -> tuple[datetime, datetime]:
    end = entry.end_at
    if not end or end <= entry.start_at:
        end = entry.start_at + timedelta(minutes=CLIO_ENTRY_DEFAULT_MINUTES)
    return entry.start_at, end


def _court_label(ce: CourtEvent) -> str:
    return f"{ce.party} {ce.purpose_raw} ({ce.dept})".strip()


def find_conflicts(results: list, clio_entries: list[CalendarEntry]) -> list[Conflict]:
    """`results` is compare_events()'s MatchResult list."""
    owner_by_type_id = {type_id: name for type_id, name in ATTORNEY_EVENT_TYPES.values()}
    bookings: list[Booking] = []
    hearing_entry_ids: set[int] = set()

    for r in results:
        ce = r.court_event
        if r.clio_entry and r.clio_entry.event_type_id in owner_by_type_id:
            owner = owner_by_type_id[r.clio_entry.event_type_id]
        elif event_type := expected_event_type(ce):
            owner = event_type[1]
        else:
            continue
        if r.clio_entry and not r.clio_entry.all_day:
            start, end = _entry_span(r.clio_entry)
        else:
            start, end = ce.dt, ce.dt + timedelta(minutes=COURT_EVENT_DEFAULT_MINUTES)
        if r.clio_entry:
            hearing_entry_ids.add(r.clio_entry.id)
        bookings.append(Booking(
            owner=owner, start=start, end=end, label=_court_label(ce), source="Court",
            matter_id=r.matter_id, clio_entry_id=r.clio_entry.id if r.clio_entry else None,
        ))

    for e in clio_entries:
        owner = owner_by_type_id.get(e.event_type_id)
        if not owner or e.all_day or e.id in hearing_entry_ids:
            continue
        start, end = _entry_span(e)
        bookings.append(Booking(
            owner=owner, start=start, end=end, label=e.summary, source="Clio only",
            matter_id=e.matter_id, clio_entry_id=e.id,
        ))

    bookings.sort(key=lambda b: (b.owner, b.start))
    conflicts: list[Conflict] = []
    for i, a in enumerate(bookings):
        for b in bookings[i + 1:]:
            if b.owner != a.owner or b.start >= a.end:
                break
            if a.matter_id and a.matter_id == b.matter_id:
                continue
            # Two hearings combined into one Clio entry share its span.
            if a.clio_entry_id and a.clio_entry_id == b.clio_entry_id:
                continue
            conflicts.append(Conflict(owner=a.owner, first=a, second=b))
    conflicts.sort(key=lambda c: (c.first.start, c.owner))
    return conflicts
