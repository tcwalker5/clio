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

Separately from assigned_attorney/staff_initials above (who's already on the
Clio calendar entry, when one exists), matter_owner_initials carries the
matter's owner (Responsible Staff if set, else Responsible Attorney — see
matters_csv.index_matter_owner_by_matter_id) — set whenever the party
resolves to a matter, including "to_create" rows that have no Clio calendar
entry at all to read attendees from. That's the gap this field closes: it's
how a missing-from-Clio hearing gets routed to the right person to fix.
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
    reason: str = ""  # short flag: "No matter in Clio" | "Ambiguous matter" |
    # "No calendar event" | "Wrong date" | "Time mismatch" | "Dept mismatch" |
    # "Purpose mismatch" (comma-joined when more than one) | "" when matched
    changes: list[str] = field(default_factory=list)  # one detailed sentence per reason, same order
    assigned_attorney_initials: list[str] = field(default_factory=list)
    assigned_staff_initials: list[str] = field(default_factory=list)
    matter_owner_initials: str = ""
    case_number_status: str = "n/a"  # "match" | "mismatch" | "not_on_file" | "n/a"
    clio_case_number: str = ""  # on-file number in Clio, when case_number_status == "mismatch"


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


def _candidate_matter_ids(party: str, matters_by_name: dict[str, list[int]]) -> list[int]:
    if party in matters_by_name:
        return matters_by_name[party]
    candidates: set[int] = set()
    for name, ids in matters_by_name.items():
        if party_names_match(party, name):
            candidates.update(ids)
    return list(candidates)


def _resolve_matter(
    party: str,
    case_number: str,
    matters_by_name: dict[str, list[int]],
    case_numbers_by_matter: dict[int, str],
) -> tuple[int | None, str, str, str]:
    """
    Resolves a court event's party to a Clio matter, using the court
    calendar's case number to disambiguate a client with more than one open
    matter/case number, and to flag when Clio's on-file case number doesn't
    match what the court calendar shows.

    Returns (matter_id, resolution_status, case_number_status, clio_case_number):
      resolution_status:   "ok" | "ambiguous" | "not_found"
      case_number_status:   "match" | "mismatch" | "not_on_file" | "n/a"
      clio_case_number:     the on-file number, when case_number_status == "mismatch"
    """
    candidates = _candidate_matter_ids(party, matters_by_name)
    if not candidates:
        return None, "not_found", "n/a", ""

    case_number = (case_number or "").strip().upper()

    if len(candidates) > 1:
        # A client with two open matters (e.g. two case numbers) — try to
        # disambiguate using which candidate's on-file case number matches.
        matches = [mid for mid in candidates if case_numbers_by_matter.get(mid) == case_number]
        if len(matches) == 1:
            return matches[0], "ok", "match", ""
        return None, "ambiguous", "n/a", ""

    matter_id = candidates[0]
    on_file = case_numbers_by_matter.get(matter_id, "")
    if not on_file:
        # Not populated in Clio — go with what the court calendar shows, no flag.
        return matter_id, "ok", "not_on_file", ""
    if on_file != case_number:
        return matter_id, "ok", "mismatch", on_file
    return matter_id, "ok", "match", ""


def _diff_entry(
    court_event: CourtEvent,
    entry: CalendarEntry,
    purpose_code: str,
    purpose_mappings: dict[str, str],
) -> tuple[list[str], list[str]]:
    """Returns (reasons, changes) — parallel lists, one entry per field that differs."""
    reasons = []
    changes = []

    if not entry.all_day:
        delta = abs((entry.start_at - court_event.dt).total_seconds())
        if delta > TIME_TOLERANCE_SECONDS:
            reasons.append("Time mismatch")
            changes.append(f"Time: Clio has {entry.start_at.strftime('%H:%M')}, court calendar has {court_event.start_time}")

    entry_dept = extract_dept_from_text(entry.summary) or extract_dept_from_text(entry.description)
    if entry_dept and entry_dept != court_event.dept:
        reasons.append("Dept mismatch")
        changes.append(f"Dept: Clio entry mentions {entry_dept}, court calendar has {court_event.dept}")

    entry_purposes = {
        normalize_purpose(p, purpose_mappings)
        for p in extract_all_purposes(entry.summary) + extract_all_purposes(entry.description)
    }
    if entry_purposes and purpose_code not in entry_purposes:
        reasons.append("Purpose mismatch")
        changes.append(f"Purpose: Clio entry mentions {', '.join(sorted(entry_purposes))}, court calendar has {purpose_code}")

    return reasons, changes


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
    matters_by_name: dict[str, list[int]],
    purpose_mappings: dict[str, str],
    staff_directory: dict[int, dict],
    case_numbers_by_matter: dict[int, str],
    matter_owner_by_matter: dict[int, str] | None = None,
) -> list[MatchResult]:
    """
    Read-only diff (no writes to Clio) — mirrors calendar-check's sync/diff:
    "shows what would change" only.
    """
    entries_by_matter = index_by_matter_id(clio_entries)
    matched_entry_ids: set[int] = set()
    results: list[MatchResult] = []

    for ce in court_events:
        if not ce.party:
            continue

        purpose_code = normalize_purpose(ce.purpose_raw, purpose_mappings)
        party_norm = normalize_party_name(ce.party)
        matter_id, resolution_status, case_number_status, clio_case_number = _resolve_matter(
            party_norm, ce.case_number, matters_by_name, case_numbers_by_matter,
        )

        if matter_id:
            owner_initials = initials((matter_owner_by_matter or {}).get(matter_id, ""))
            candidates = entries_by_matter.get(matter_id, [])
            same_day = [e for e in candidates if e.start_at.date() == ce.dt.date()]

            if same_day:
                entry = same_day[0]
                reasons, changes = _diff_entry(ce, entry, purpose_code, purpose_mappings)
                matched_entry_ids.add(entry.id)
                attorney_initials, staff_initials = _classify_assigned(entry, staff_directory)
                results.append(MatchResult(
                    court_event=ce, status="matched" if not changes else "to_update",
                    clio_entry=entry, matter_id=matter_id, match_method="matter_id",
                    reason=", ".join(reasons), changes=changes,
                    assigned_attorney_initials=attorney_initials,
                    assigned_staff_initials=staff_initials, matter_owner_initials=owner_initials,
                    case_number_status=case_number_status, clio_case_number=clio_case_number,
                ))
            elif candidates:
                results.append(MatchResult(
                    court_event=ce, status="to_update", clio_entry=None, matter_id=matter_id,
                    match_method="matter_id", matter_owner_initials=owner_initials,
                    reason="Wrong date",
                    changes=[f"No Clio calendar entry on {ce.date} — matter has {len(candidates)} other entries in range"],
                    case_number_status=case_number_status, clio_case_number=clio_case_number,
                ))
            else:
                results.append(MatchResult(
                    court_event=ce, status="to_create", clio_entry=None,
                    matter_id=matter_id, match_method="matter_id", matter_owner_initials=owner_initials,
                    reason="No calendar event",
                    changes=[f"Matter {matter_id} has no Clio calendar entry at all in this date range"],
                    case_number_status=case_number_status, clio_case_number=clio_case_number,
                ))
            continue

        unlinked = [e for e in clio_entries if not e.matter_id and e.id not in matched_entry_ids]
        entry = _text_fallback_match(ce, purpose_code, unlinked, purpose_mappings)
        if entry:
            reasons, changes = _diff_entry(ce, entry, purpose_code, purpose_mappings)
            matched_entry_ids.add(entry.id)
            attorney_initials, staff_initials = _classify_assigned(entry, staff_directory)
            results.append(MatchResult(
                court_event=ce, status="matched" if not changes else "to_update",
                clio_entry=entry, matter_id=entry.matter_id, match_method="text_fallback",
                reason=", ".join(reasons), changes=changes,
                assigned_attorney_initials=attorney_initials,
                assigned_staff_initials=staff_initials,
            ))
        elif resolution_status == "ambiguous":
            results.append(MatchResult(
                court_event=ce, status="to_create", clio_entry=None,
                matter_id=None, match_method="none", reason="Ambiguous matter",
                changes=["Multiple open matters share this last name and the court case number "
                         "isn't on file in Clio to disambiguate — add the Court Case Number to the "
                         "right matter in Clio, or resolve manually"],
            ))
        else:
            results.append(MatchResult(
                court_event=ce, status="to_create", clio_entry=None,
                matter_id=None, match_method="none", reason="No matter in Clio",
                changes=[f'No open Clio matter found for "{ce.party}"'],
            ))

    return results
