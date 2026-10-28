"""
matcher.py — Compares parsed court calendar events against Clio calendar
entries. Ported from calendar-check's matcher.js compareEvents(), with a
change (see plan doc): matter-ID-first matching. A court event's party is
resolved to a Clio matter ID (reusing matter_matching.py), then we look for
a calendar entry already linked to that matter on the same date. Only when
that fails do we fall back to calendar-check's original approach — scanning
entries' free-text summary/description for a party-name substring match.

matter_owner_initials carries the matter's owner (Responsible Staff if set,
else Responsible Attorney — see matter_fields.index_matter_owner_by_matter_id)
— set whenever the party resolves to a matter, including "to_create" rows
that have no Clio calendar entry at all. This is the reliable routing signal
for missing/wrong rows; a matched entry's own calendar_owner/attendees were
tried as a second signal but dropped (2026-07) after confirming this firm's
hearings are entered on a shared "Firm" Clio calendar rather than assigned
to an individual, so that data was consistently empty in practice.
"""

import re
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
from court_calendar.schedule import ATTORNEY_TYPE_IDS, expected_event_type

TIME_TOLERANCE_SECONDS = 60  # matches matcher.js's 1-minute isSameTime tolerance


@dataclass
class MatchResult:
    court_event: CourtEvent
    status: str  # "matched" | "to_update" | "to_create"
    clio_entry: CalendarEntry | None
    matter_id: int | None
    match_method: str  # "matter_id" | "case_number" | "opposing_party" | "text_fallback" | "none"
    reason: str = ""  # short flag: "No matter in Clio" | "Ambiguous matter" |
    # "No calendar event" | "Wrong date" | "Time mismatch" | "Dept mismatch" |
    # "Purpose mismatch" | "No attorney type" (comma-joined when more than one) | "" when matched
    # One entry per reason, same order as the reason it belongs to would
    # appear in `reason`. Each entry is either {"court": ..., "clio": ...}
    # (a field-level Court-vs-Clio comparison) or {"detail": ...} (a
    # freeform explanation with no natural Court/Clio split, e.g. "Ambiguous
    # matter"). Rendered as 2 or 1 extra small lines respectively under the
    # reason label — see calendar_sync.html.
    changes: list[dict[str, str]] = field(default_factory=list)
    matter_owner_initials: str = ""
    case_number_status: str = "n/a"  # "match" | "mismatch" | "not_on_file" | "n/a"
    clio_case_number: str = ""  # on-file number in Clio, when case_number_status == "mismatch"
    match_note: str = ""  # how a case_number/opposing_party match was made, shown under "Matched via"


def _candidate_matter_ids(party: str, matters_by_name: dict[str, list[int]]) -> list[int]:
    if party in matters_by_name:
        return matters_by_name[party]
    candidates: set[int] = set()
    for name, ids in matters_by_name.items():
        if party_names_match(party, name):
            candidates.update(ids)
    return list(candidates)


def _name_words(name: str) -> set[str]:
    return set(re.split(r"[\s\-]+", name.upper())) - {""}


def _case_number_status(matter_id: int, case_number: str, case_numbers_by_matter: dict[int, str]) -> tuple[str, str]:
    on_file = case_numbers_by_matter.get(matter_id, "")
    if not on_file:
        # Not populated in Clio — resolution still succeeds (go with what the
        # court calendar shows), but compare_events() turns this into a
        # "Case number not on file" reason so the row shows as needing
        # attention rather than a false "In sync", until it's resolved.
        return "not_on_file", ""
    if on_file != case_number:
        return "mismatch", on_file
    return "match", ""


@dataclass
class Resolution:
    matter_id: int | None
    status: str  # "ok" | "ambiguous" | "not_found"
    case_number_status: str = "n/a"  # "match" | "mismatch" | "not_on_file" | "n/a"
    clio_case_number: str = ""  # the on-file number, when case_number_status == "mismatch"
    method: str = "matter_id"  # "matter_id" (client name) | "case_number" | "opposing_party"
    note: str = ""


def _resolve_matter(
    party: str,
    case_number: str,
    matters_by_name: dict[str, list[int]],
    case_numbers_by_matter: dict[int, str],
    opposing_parties: list | None = None,
    matters_with_entry_that_day: set[int] | None = None,
) -> Resolution:
    """
    Resolves a court event's party to an open Clio matter. The court calendar's
    party is the petitioner (last name only), which is usually our client but
    not always — when we represent the respondent it's the other side. So:
      1. Case number: exactly one open matter has this Court Case Number on
         file -> that matter, even over a client-name match.
      2. Client name (matter display_number last name), using the case
         number to pick between a client's several open matters.
      3. Opposing Party: no client has this name, but an open matter has an
         Opposing Party contact whose name contains every word of it. A common
         last name can hit several matters (live 2026-10-26: JOHNSON was the
         OP on COHEN and SAWKA) — keep the ones with a Clio entry that day.
    `opposing_parties` is relationships.fetch_oc_op_contacts()'s OP contacts
    (name, matter_id, matter_display_number), already limited to open matters.
    """
    case_number = (case_number or "").strip().upper()
    candidates = _candidate_matter_ids(party, matters_by_name)

    by_case = [mid for mid, on_file in case_numbers_by_matter.items() if case_number and on_file == case_number]
    if len(by_case) == 1:
        if by_case[0] in candidates:
            return Resolution(by_case[0], "ok", "match")
        return Resolution(by_case[0], "ok", "match", method="case_number",
                          note=f"Court party {party} isn't the client name; case number {case_number} is on this matter")

    if len(candidates) > 1:
        # A client with two open matters (e.g. two case numbers) — the case
        # number check above already failed to pick one.
        return Resolution(None, "ambiguous")
    if candidates:
        return Resolution(candidates[0], "ok", *_case_number_status(candidates[0], case_number, case_numbers_by_matter))

    words = _name_words(party)
    op_hits = [c for c in opposing_parties or [] if words and words <= _name_words(c.name)]
    op_matters = sorted({c.matter_id for c in op_hits})
    if len(op_matters) > 1 and matters_with_entry_that_day:
        same_day = [mid for mid in op_matters if mid in matters_with_entry_that_day]
        if len(same_day) == 1:
            op_hits = [c for c in op_hits if c.matter_id == same_day[0]]
            op_matters = same_day
    if len(op_matters) == 1:
        names = ", ".join(sorted({c.name for c in op_hits}))
        return Resolution(op_matters[0], "ok", *_case_number_status(op_matters[0], case_number, case_numbers_by_matter),
                          method="opposing_party", note=f"Court party is the opposing party: {names}")
    if op_matters:
        listing = "; ".join(sorted({f"{c.name} (OP on {c.matter_display_number})" for c in op_hits}))
        return Resolution(None, "ambiguous", method="opposing_party",
                          note=f"Matches the opposing party on more than one open matter: {listing} — add the Court "
                               f"Case Number to the right matter in Clio")
    return Resolution(None, "not_found")


def _entry_purposes(entry: CalendarEntry, purpose_mappings: dict[str, str]) -> set[str]:
    return {
        normalize_purpose(p, purpose_mappings)
        for p in extract_all_purposes(entry.summary) + extract_all_purposes(entry.description)
    }


def _diff_entry(
    court_event: CourtEvent,
    entry: CalendarEntry,
    purpose_code: str,
    purpose_mappings: dict[str, str],
) -> tuple[list[str], list[dict[str, str]]]:
    """Returns (reasons, changes) — parallel lists, one entry per field that differs."""
    reasons = []
    changes = []

    if not entry.all_day:
        delta = abs((entry.start_at - court_event.dt).total_seconds())
        if delta > TIME_TOLERANCE_SECONDS:
            reasons.append("Time mismatch")
            changes.append({"court": court_event.start_time, "clio": entry.start_at.strftime('%H:%M')})

    entry_dept = extract_dept_from_text(entry.summary) or extract_dept_from_text(entry.description)
    if entry_dept and entry_dept != court_event.dept:
        reasons.append("Dept mismatch")
        changes.append({"court": court_event.dept, "clio": entry_dept})

    entry_purposes = _entry_purposes(entry, purpose_mappings)
    if entry_purposes and purpose_code not in entry_purposes:
        reasons.append("Purpose mismatch")
        changes.append({"court": purpose_code, "clio": ', '.join(sorted(entry_purposes))})

    return reasons, changes


def _type_diff(court_event: CourtEvent, entry: CalendarEntry, check_types: bool) -> tuple[list[str], list[dict[str, str]]]:
    """Clio event type is who's appearing (see schedule.py) — flag a hearing
    whose entry isn't typed for any attorney (no type, or "Staff"). Another
    attorney's type is fine: they're covering for the attorney of record."""
    expected = expected_event_type(court_event) if check_types else None
    if not expected or entry.event_type_id in ATTORNEY_TYPE_IDS:
        return [], []
    return ["No attorney type"], [{"court": expected[1], "clio": entry.event_type_name or "no type set"}]


def _pick_entry(
    entries: list[CalendarEntry],
    court_event: CourtEvent,
    purpose_code: str,
    purpose_mappings: dict[str, str],
    check_types: bool,
) -> CalendarEntry:
    """Best same-day entry: the attorney of record's type first, then any
    attorney's type, then one already naming this purpose, then the first
    one. max() keeps the first of any tie."""
    expected = expected_event_type(court_event) if check_types else None
    return max(entries, key=lambda e: (
        bool(expected) and e.event_type_id == expected[0],
        bool(expected) and e.event_type_id in ATTORNEY_TYPE_IDS,
        purpose_code in _entry_purposes(e, purpose_mappings),
    ))


def _possible_entries(
    court_event: CourtEvent,
    clio_entries: list[CalendarEntry],
    taken_matter_ids: set[int],
) -> list[CalendarEntry]:
    """Clio entries at the same time (and same dept, when the entry names
    one) as a hearing no matter was found for — a hint, never a match.
    Real example: court party "Lorraine" (the petitioner) at 1:30 in N-17,
    Clio "Ahlquist FRC (N17)" at 1:30 on our client's matter, with no
    Opposing Party contact or case number on file to link them."""
    found = []
    for e in clio_entries:
        if e.all_day or not e.matter_id or e.matter_id in taken_matter_ids:
            continue
        if abs((e.start_at - court_event.dt).total_seconds()) > TIME_TOLERANCE_SECONDS:
            continue
        entry_dept = extract_dept_from_text(e.summary) or extract_dept_from_text(e.description)
        if entry_dept and entry_dept != court_event.dept:
            continue
        found.append(e)
    return found


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
    case_numbers_by_matter: dict[int, str],
    matter_owner_by_matter: dict[int, str] | None = None,
    check_types: bool = False,
    opposing_parties: list | None = None,
) -> list[MatchResult]:
    """
    Read-only diff (no writes to Clio) — mirrors calendar-check's sync/diff:
    "shows what would change" only. check_types also matches each hearing's
    Clio entry against its attorney's event type (schedule.ATTORNEY_EVENT_TYPES).
    opposing_parties enables the Opposing Party fallback (see _resolve_matter).
    """
    entries_by_matter = index_by_matter_id(clio_entries)
    matters_by_day: dict[str, set[int]] = {}
    for mid, entries in entries_by_matter.items():
        for e in entries:
            matters_by_day.setdefault(e.start_at.date().isoformat(), set()).add(mid)
    matched_entry_ids: set[int] = set()
    results: list[MatchResult] = []

    # Resolve every event's matter/purpose up front so same-matter/same-day
    # groups can be sized before any entry gets claimed — needed to tell a
    # combined appearance (real example: a DVRO and an FRC heard together,
    # same date/time/dept/case, staff-entered as ONE Clio calendar entry)
    # apart from two genuinely separate hearings that each need their own
    # entry. See the group_size/shared-entry branch below.
    resolved: list[tuple[str, Resolution] | None] = []
    group_sizes: dict[tuple[int, str], int] = {}
    for ce in court_events:
        if not ce.party:
            resolved.append(None)
            continue
        purpose_code = normalize_purpose(ce.purpose_raw, purpose_mappings)
        party_norm = normalize_party_name(ce.party)
        resolution = _resolve_matter(
            party_norm, ce.case_number, matters_by_name, case_numbers_by_matter, opposing_parties,
            matters_by_day.get(ce.date),
        )
        resolved.append((purpose_code, resolution))
        if resolution.matter_id:
            key = (resolution.matter_id, ce.date)
            group_sizes[key] = group_sizes.get(key, 0) + 1

    for ce, r in zip(court_events, resolved):
        if r is None:
            continue
        purpose_code, resolution = r
        matter_id = resolution.matter_id
        case_number_status = resolution.case_number_status
        clio_case_number = resolution.clio_case_number
        method, note = resolution.method, resolution.note

        if matter_id:
            owner_initials = initials((matter_owner_by_matter or {}).get(matter_id, ""))
            candidates = entries_by_matter.get(matter_id, [])
            same_day = [e for e in candidates if e.start_at.date() == ce.dt.date()]
            group_size = group_sizes[(matter_id, ce.date)]

            if group_size > 1 and 0 < len(same_day) < group_size:
                # Fewer Clio entries than hearings this matter/day — the
                # common real-world cause is a combined appearance entered
                # as one entry, not several distinct hearings each missing
                # their own. Every hearing in the group shares whichever
                # entry best fits its own purpose text, and a hearing is
                # only flagged if NONE of the group's purposes appear in
                # that entry at all — not just because the entry doesn't
                # separately spell out this one specific hearing type.
                entry = _pick_entry(same_day, ce, purpose_code, purpose_mappings, check_types)
                reasons, changes = _diff_entry(ce, entry, purpose_code, purpose_mappings)
                group_purposes = {
                    other_r[0] for other_ce, other_r in zip(court_events, resolved)
                    if other_r and other_r[1].matter_id == matter_id and other_ce.date == ce.date
                }
                entry_purposes = _entry_purposes(entry, purpose_mappings)
                if "Purpose mismatch" in reasons and entry_purposes & group_purposes:
                    idx = reasons.index("Purpose mismatch")
                    reasons.pop(idx)
                    changes.pop(idx)
                type_reasons, type_changes = _type_diff(ce, entry, check_types)
                reasons += type_reasons
                changes += type_changes
                if case_number_status == "not_on_file":
                    reasons.append("Case number not on file")
                    changes.append({"court": ce.case_number, "clio": "not on file"})
                elif case_number_status == "mismatch":
                    reasons.append("Case number mismatch")
                    changes.append({"court": ce.case_number, "clio": clio_case_number})
                results.append(MatchResult(
                    court_event=ce, status="matched" if not changes else "to_update",
                    clio_entry=entry, matter_id=matter_id, match_method=method, match_note=note,
                    reason=", ".join(reasons), changes=changes, matter_owner_initials=owner_initials,
                    case_number_status=case_number_status, clio_case_number=clio_case_number,
                ))
                continue

            # Excludes entries already claimed by an earlier court event for
            # this same matter/day — only reached when this matter/day has
            # at least as many Clio entries as hearings (the group_size
            # branch above already handled the undersupplied/combined case),
            # so any leftover claim here means a hearing genuinely needs its
            # own separate entry rather than sharing one.
            same_day_available = [e for e in same_day if e.id not in matched_entry_ids]

            if same_day_available:
                # Prefer the available entry typed for this hearing's
                # attorney, then one already mentioning its purpose.
                entry = _pick_entry(same_day_available, ce, purpose_code, purpose_mappings, check_types)
                reasons, changes = _diff_entry(ce, entry, purpose_code, purpose_mappings)
                type_reasons, type_changes = _type_diff(ce, entry, check_types)
                reasons += type_reasons
                changes += type_changes
                # A row isn't really "in sync" if its case number still
                # needs attention in Clio — without this, a matter with no
                # case number on file (or a mismatched one) could show a
                # green "In sync" badge and get hidden by the "needing
                # attention" filter, even though the "Update in Clio"
                # button is sitting right there unresolved on that row.
                if case_number_status == "not_on_file":
                    reasons.append("Case number not on file")
                    changes.append({"court": ce.case_number, "clio": "not on file"})
                elif case_number_status == "mismatch":
                    reasons.append("Case number mismatch")
                    changes.append({"court": ce.case_number, "clio": clio_case_number})
                matched_entry_ids.add(entry.id)
                results.append(MatchResult(
                    court_event=ce, status="matched" if not changes else "to_update",
                    clio_entry=entry, matter_id=matter_id, match_method=method, match_note=note,
                    reason=", ".join(reasons), changes=changes, matter_owner_initials=owner_initials,
                    case_number_status=case_number_status, clio_case_number=clio_case_number,
                ))
            elif same_day:
                results.append(MatchResult(
                    court_event=ce, status="to_create", clio_entry=None, matter_id=matter_id,
                    match_method=method, match_note=note, matter_owner_initials=owner_initials,
                    reason="No calendar event",
                    changes=[{"detail": f"Matter has a Clio entry on {ce.date}, but it's already matched to a "
                                         f"different hearing that day — this one needs its own entry"}],
                    case_number_status=case_number_status, clio_case_number=clio_case_number,
                ))
            elif candidates:
                results.append(MatchResult(
                    court_event=ce, status="to_update", clio_entry=None, matter_id=matter_id,
                    match_method=method, match_note=note, matter_owner_initials=owner_initials,
                    reason="Wrong date",
                    changes=[{"detail": f"No Clio calendar entry on {ce.date} — matter has {len(candidates)} other entries in range"}],
                    case_number_status=case_number_status, clio_case_number=clio_case_number,
                ))
            else:
                results.append(MatchResult(
                    court_event=ce, status="to_create", clio_entry=None,
                    matter_id=matter_id, match_method=method, match_note=note, matter_owner_initials=owner_initials,
                    reason="No calendar event",
                    changes=[{"detail": f"Matter {matter_id} has no Clio calendar entry at all in this date range"}],
                    case_number_status=case_number_status, clio_case_number=clio_case_number,
                ))
            continue

        unlinked = [e for e in clio_entries if not e.matter_id and e.id not in matched_entry_ids]
        entry = _text_fallback_match(ce, purpose_code, unlinked, purpose_mappings)
        if entry:
            reasons, changes = _diff_entry(ce, entry, purpose_code, purpose_mappings)
            type_reasons, type_changes = _type_diff(ce, entry, check_types)
            reasons += type_reasons
            changes += type_changes
            matched_entry_ids.add(entry.id)
            results.append(MatchResult(
                court_event=ce, status="matched" if not changes else "to_update",
                clio_entry=entry, matter_id=entry.matter_id, match_method="text_fallback",
                reason=", ".join(reasons), changes=changes,
            ))
        elif resolution.status == "ambiguous":
            results.append(MatchResult(
                court_event=ce, status="to_create", clio_entry=None,
                matter_id=None, match_method="none", reason="Ambiguous matter",
                changes=[{"detail": resolution.note or (
                    "Multiple open matters share this last name and the court case number "
                    "isn't on file in Clio to disambiguate — add the Court Case Number to the "
                    "right matter in Clio, or resolve manually")}],
            ))
        else:
            changes = [{"detail": f'No open Clio matter found for "{ce.party}" — not as a client, '
                                  f'opposing party, or case number {ce.case_number}'}]
            taken = {other[1].matter_id for other_ce, other in zip(court_events, resolved)
                     if other and other[1].matter_id and other_ce.date == ce.date}
            for e in _possible_entries(ce, clio_entries, taken):
                changes.append({"detail": f'Possible match: {e.matter_display_number} — "{e.summary}" is at the '
                                          f'same time. If so, add {ce.party} as its Opposing Party or add case '
                                          f'number {ce.case_number}'})
            results.append(MatchResult(
                court_event=ce, status="to_create", clio_entry=None,
                matter_id=None, match_method="none", reason="No matter in Clio", changes=changes,
            ))

    return results
