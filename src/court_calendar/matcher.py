"""
matcher.py — Compares parsed court calendar events against Clio calendar
entries. Ported from calendar-check's matcher.js compareEvents(), with two
changes (see plan doc):

  1. Matter-ID-first matching: a court event's party is resolved to a Clio
     matter ID (reusing matter_matching.py), then we look for a calendar
     entry already linked to that matter on the same date. Only when that
     fails do we fall back to calendar-check's original approach — scanning
     entries' free-text summary/description for a party-name substring match.
  2. Staff assignment comes from the matched entry's calendar_owner/attendees
     (resolved against the Clio staff directory), not a name found in text.
"""

from dataclasses import dataclass, field
from datetime import datetime

from court_calendar.clio_calendar import CalendarEntry, index_by_matter_id
from court_calendar.normalizer import (
    CourtEvent,
    extract_all_purposes,
    extract_dept_from_text,
    initials,
    normalize_party_name,
    normalize_purpose,
    party_names_match,
)

TIME_TOLERANCE_SECONDS = 60  # matches matcher.js's 1-minute isSameTime tolerance


@dataclass
class MatchResult:
    court_event: CourtEvent
    status: str  # "matched" | "to_update" | "to_create"
    clio_entry: CalendarEntry | None
    matter_id: int | None
    match_method: str  # "matter_id" | "text_fallback" | "none"
    changes: list[str] = field(default_factory=list)
    assigned_attorney_initials: list[str] = field(default_factory=list)
    assigned_staff_initials: list[str] = field(default_factory=list)


def _classify_assigned(entry: CalendarEntry, staff_directory: dict[int, dict]) -> tuple[list[str], list[str]]:
    """
    Splits an entry's calendar_owner/attendees into (attorney initials, staff
    initials), using Clio's own subscription_type (is_attorney) as the source
    of truth. Matched by email where available (attendees), else by name
    (calendar_owner, which has no email) — see clio_calendar.py for why there's
    no shared numeric ID to join on directly.
    """
    by_email = {info["email"].lower(): info for info in staff_directory.values() if info.get("email")}
    by_name = {info["name"]: info for info in staff_directory.values() if info.get("name")}

    attorneys: list[str] = []
    staff: list[str] = []

    def _add(name: str, is_attorney: bool) -> None:
        target = attorneys if is_attorney else staff
        ini = initials(name)
        if ini and ini not in target:
            target.append(ini)

    for attendee in entry.attendees:
        name = attendee.get("name")
        if not name:
            continue
        email = (attendee.get("email") or "").lower()
        info = by_email.get(email) or by_name.get(name)
        _add(name, info["is_attorney"] if info else False)

    if entry.calendar_owner_name:
        info = by_name.get(entry.calendar_owner_name)
        if info:
            _add(entry.calendar_owner_name, info["is_attorney"])

    return attorneys, staff


def _resolve_matter_id(party: str, matters_index: dict[str, int | None]) -> tuple[int | None, bool]:
    """
    Returns (matter_id, ambiguous). matter_id is None with ambiguous=True when
    the party matched multiple candidates and couldn't be disambiguated.
    """
    if party in matters_index:
        return matters_index[party], matters_index[party] is None

    candidates = {mid for name, mid in matters_index.items() if mid and party_names_match(party, name)}
    if len(candidates) == 1:
        return next(iter(candidates)), False
    if len(candidates) > 1:
        return None, True
    return None, False


def _diff_entry(
    court_event: CourtEvent,
    entry: CalendarEntry,
    purpose_code: str,
    purpose_mappings: dict[str, str],
) -> list[str]:
    changes = []

    if not entry.all_day:
        delta = abs((entry.start_at - court_event.dt).total_seconds())
        if delta > TIME_TOLERANCE_SECONDS:
            changes.append(f"Time: Clio has {entry.start_at.strftime('%H:%M')}, court calendar has {court_event.start_time}")

    entry_dept = extract_dept_from_text(entry.summary) or extract_dept_from_text(entry.description)
    if entry_dept and entry_dept != court_event.dept:
        changes.append(f"Dept: Clio entry mentions {entry_dept}, court calendar has {court_event.dept}")

    entry_purposes = {
        normalize_purpose(p, purpose_mappings)
        for p in extract_all_purposes(entry.summary) + extract_all_purposes(entry.description)
    }
    if entry_purposes and purpose_code not in entry_purposes:
        changes.append(f"Purpose: Clio entry mentions {', '.join(sorted(entry_purposes))}, court calendar has {purpose_code}")

    return changes


def _text_fallback_match(
    court_event: CourtEvent,
    purpose_code: str,
    unlinked_entries: list[CalendarEntry],
    purpose_mappings: dict[str, str],
) -> CalendarEntry | None:
    """
    Ported from calendar-check's substring party-name matching against free
    text, used only when the court event's party doesn't resolve to a matter.
    """
    party_norm = normalize_party_name(court_event.party)
    candidates = []
    for entry in unlinked_entries:
        if entry.start_at.date() != court_event.dt.date():
            continue
        text = f"{entry.summary} {entry.description}"
        entry_party = normalize_party_name(entry.summary.split(" ")[0]) if entry.summary else ""
        if not party_names_match(party_norm, entry_party) and party_norm not in text.upper():
            continue
        entry_purposes = {normalize_purpose(p, purpose_mappings) for p in extract_all_purposes(text)}
        if entry_purposes and purpose_code not in entry_purposes:
            continue
        candidates.append(entry)

    return candidates[0] if candidates else None


def compare_events(
    court_events: list[CourtEvent],
    clio_entries: list[CalendarEntry],
    matters_index: dict[str, int | None],
    purpose_mappings: dict[str, str],
    staff_directory: dict[int, dict],
) -> tuple[list[MatchResult], list[CalendarEntry]]:
    """
    Read-only diff (no writes to Clio) — mirrors calendar-check's sync/diff:
    "shows what would change" only. Returns (results, orphaned_entries) where
    orphaned_entries are Clio entries with a recognized court purpose code
    that never matched any court event (possibly stale/cancelled hearings).
    """
    entries_by_matter = index_by_matter_id(clio_entries)
    matched_entry_ids: set[int] = set()
    results: list[MatchResult] = []

    for ce in court_events:
        if not ce.party:
            continue

        purpose_code = normalize_purpose(ce.purpose_raw, purpose_mappings)
        party_norm = normalize_party_name(ce.party)
        matter_id, ambiguous = _resolve_matter_id(party_norm, matters_index)

        if matter_id:
            candidates = entries_by_matter.get(matter_id, [])
            same_day = [e for e in candidates if e.start_at.date() == ce.dt.date()]

            if same_day:
                entry = same_day[0]
                changes = _diff_entry(ce, entry, purpose_code, purpose_mappings)
                matched_entry_ids.add(entry.id)
                attorney_initials, staff_initials = _classify_assigned(entry, staff_directory)
                results.append(MatchResult(
                    court_event=ce, status="matched" if not changes else "to_update",
                    clio_entry=entry, matter_id=matter_id, match_method="matter_id",
                    changes=changes, assigned_attorney_initials=attorney_initials,
                    assigned_staff_initials=staff_initials,
                ))
            elif candidates:
                results.append(MatchResult(
                    court_event=ce, status="to_update", clio_entry=None, matter_id=matter_id,
                    match_method="matter_id",
                    changes=[f"No Clio calendar entry on {ce.date} — matter has {len(candidates)} other entries in range"],
                ))
            else:
                results.append(MatchResult(
                    court_event=ce, status="to_create", clio_entry=None,
                    matter_id=matter_id, match_method="matter_id",
                ))
            continue

        unlinked = [e for e in clio_entries if not e.matter_id and e.id not in matched_entry_ids]
        entry = _text_fallback_match(ce, purpose_code, unlinked, purpose_mappings)
        if entry:
            changes = _diff_entry(ce, entry, purpose_code, purpose_mappings)
            matched_entry_ids.add(entry.id)
            attorney_initials, staff_initials = _classify_assigned(entry, staff_directory)
            results.append(MatchResult(
                court_event=ce, status="matched" if not changes else "to_update",
                clio_entry=entry, matter_id=entry.matter_id, match_method="text_fallback",
                changes=changes, assigned_attorney_initials=attorney_initials,
                assigned_staff_initials=staff_initials,
            ))
        else:
            note = "Ambiguous matter match — add to MANUAL_MATTER_MAP" if ambiguous else "No matching matter or calendar entry found"
            results.append(MatchResult(
                court_event=ce, status="to_create", clio_entry=None,
                matter_id=None, match_method="none", changes=[note],
            ))

    known_codes = set(purpose_mappings.values())
    orphaned = [
        e for e in clio_entries
        if e.id not in matched_entry_ids
        and known_codes & {normalize_purpose(p, purpose_mappings) for p in extract_all_purposes(f"{e.summary} {e.description}")}
    ]

    return results, orphaned
