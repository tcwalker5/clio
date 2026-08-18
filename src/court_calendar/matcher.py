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
    matter_owner_initials: str = ""
    case_number_status: str = "n/a"  # "match" | "mismatch" | "not_on_file" | "n/a"
    clio_case_number: str = ""  # on-file number in Clio, when case_number_status == "mismatch"


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
        # Not populated in Clio — resolution still succeeds (go with what the
        # court calendar shows), but compare_events() turns this into a
        # "Case number not on file" reason so the row shows as needing
        # attention rather than a false "In sync", until it's resolved.
        return matter_id, "ok", "not_on_file", ""
    if on_file != case_number:
        return matter_id, "ok", "mismatch", on_file
    return matter_id, "ok", "match", ""


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

    entry_purposes = _entry_purposes(entry, purpose_mappings)
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

    # Resolve every event's matter/purpose up front so same-matter/same-day
    # groups can be sized before any entry gets claimed — needed to tell a
    # combined appearance (real example: a DVRO and an FRC heard together,
    # same date/time/dept/case, staff-entered as ONE Clio calendar entry)
    # apart from two genuinely separate hearings that each need their own
    # entry. See the group_size/shared-entry branch below.
    resolved: list[tuple[str, int | None, str, str, str] | None] = []
    group_sizes: dict[tuple[int, str], int] = {}
    for ce in court_events:
        if not ce.party:
            resolved.append(None)
            continue
        purpose_code = normalize_purpose(ce.purpose_raw, purpose_mappings)
        party_norm = normalize_party_name(ce.party)
        matter_id, resolution_status, case_number_status, clio_case_number = _resolve_matter(
            party_norm, ce.case_number, matters_by_name, case_numbers_by_matter,
        )
        resolved.append((purpose_code, matter_id, resolution_status, case_number_status, clio_case_number))
        if matter_id:
            key = (matter_id, ce.date)
            group_sizes[key] = group_sizes.get(key, 0) + 1

    for ce, r in zip(court_events, resolved):
        if r is None:
            continue
        purpose_code, matter_id, resolution_status, case_number_status, clio_case_number = r

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
                entry = next(
                    (e for e in same_day if purpose_code in _entry_purposes(e, purpose_mappings)),
                    same_day[0],
                )
                reasons, changes = _diff_entry(ce, entry, purpose_code, purpose_mappings)
                group_purposes = {
                    other_r[0] for other_ce, other_r in zip(court_events, resolved)
                    if other_r and other_r[1] == matter_id and other_ce.date == ce.date
                }
                entry_purposes = _entry_purposes(entry, purpose_mappings)
                if "Purpose mismatch" in reasons and entry_purposes & group_purposes:
                    idx = reasons.index("Purpose mismatch")
                    reasons.pop(idx)
                    changes.pop(idx)
                if case_number_status == "not_on_file":
                    reasons.append("Case number not on file")
                    changes.append(f"Case number: not on file in Clio, court calendar has {ce.case_number}")
                elif case_number_status == "mismatch":
                    reasons.append("Case number mismatch")
                    changes.append(f"Case number: Clio has {clio_case_number}, court calendar has {ce.case_number}")
                results.append(MatchResult(
                    court_event=ce, status="matched" if not changes else "to_update",
                    clio_entry=entry, matter_id=matter_id, match_method="matter_id",
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
                # Prefer whichever available entry already mentions this
                # event's purpose, if more than one candidate exists.
                entry = next(
                    (e for e in same_day_available if purpose_code in _entry_purposes(e, purpose_mappings)),
                    same_day_available[0],
                )
                reasons, changes = _diff_entry(ce, entry, purpose_code, purpose_mappings)
                # A row isn't really "in sync" if its case number still
                # needs attention in Clio — without this, a matter with no
                # case number on file (or a mismatched one) could show a
                # green "In sync" badge and get hidden by the "needing
                # attention" filter, even though the "Update in Clio"
                # button is sitting right there unresolved on that row.
                if case_number_status == "not_on_file":
                    reasons.append("Case number not on file")
                    changes.append(f"Case number: not on file in Clio, court calendar has {ce.case_number}")
                elif case_number_status == "mismatch":
                    reasons.append("Case number mismatch")
                    changes.append(f"Case number: Clio has {clio_case_number}, court calendar has {ce.case_number}")
                matched_entry_ids.add(entry.id)
                results.append(MatchResult(
                    court_event=ce, status="matched" if not changes else "to_update",
                    clio_entry=entry, matter_id=matter_id, match_method="matter_id",
                    reason=", ".join(reasons), changes=changes, matter_owner_initials=owner_initials,
                    case_number_status=case_number_status, clio_case_number=clio_case_number,
                ))
            elif same_day:
                results.append(MatchResult(
                    court_event=ce, status="to_create", clio_entry=None, matter_id=matter_id,
                    match_method="matter_id", matter_owner_initials=owner_initials,
                    reason="No calendar event",
                    changes=[f"Matter has a Clio entry on {ce.date}, but it's already matched to a "
                             f"different hearing that day — this one needs its own entry"],
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
            results.append(MatchResult(
                court_event=ce, status="matched" if not changes else "to_update",
                clio_entry=entry, matter_id=entry.matter_id, match_method="text_fallback",
                reason=", ".join(reasons), changes=changes,
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
