"""
event_parser.py — Matches Outlook events to Clio matters and extracts
purpose/department, reusing the same free-text extraction already built for
calendar-check-style subjects in court_calendar/normalizer.py.

Three event types are recognized, tried in this order (each only runs if the
previous one found nothing):
  - Hearings: FRC/RFO/MSC/etc. — goes through the normal purpose_mappings.
  - Calls: TCON (telephone) / OCON (office/in-person) — a deliberately
    separate, simpler path per project decision ("doesn't need the purpose
    mappings"). Real-data check found bare "OC" is unsafe to treat as a call
    marker — in practice it means "Opposing Counsel", not "office conference"
    (e.g. "SATTERLY OC's Responsive Dec due") — so only the unambiguous forms
    (TCON, OCON, TCN, T/C, T-C, T.C.) are recognized; a lone "OC"/"TC" is not.
  - Deadline reminders (purpose_code "DUE"): any subject containing "due" as
    a whole word, party read off the leading word (see extract_due_event()).
    csv_export.py forces these to all-day on import — a deadline note has no
    meaningful clock time. Shares the same manual-resolution/OC-OP pipeline
    as calls (see outlook_migration.py) when it can't auto-match a matter.

OP/CL (Opposing Party vs. Client) is deliberately NOT parsed here — per
project decision, this can't be reliably derived from the Outlook data and
is left as an explicit placeholder for a paralegal to fill in by hand.
"""

import re
from dataclasses import dataclass

from court_calendar.normalizer import (
    extract_all_purposes,
    extract_dept_from_text,
    extract_party_name,
    normalize_dept,
    normalize_party_name,
    normalize_purpose,
    party_names_match,
)
from outlook_calendar.graph_client import OutlookEvent

OP_CL_PLACEHOLDER = "OP/CL"

# T.../O... call markers — only forms confirmed unambiguous against real data
# (see module docstring). Two subject conventions seen:
#   "NAME TCON"                          -> party immediately precedes marker
#   "Last, First- [staff] t/c w/CL"      -> party precedes the first dash
_CALL_MARKER_ALT = r"TCON|OCON|TCN|OCN|T[/.\-]C"
_CALL_MARKER_RE = re.compile(rf"\b(?:{_CALL_MARKER_ALT})\b", re.IGNORECASE)
_CALL_PARTY_DASH_RE = re.compile(r"^([A-Z][A-Za-z,.\s'-]+?)\s*-\s*\S", re.IGNORECASE)
_CALL_PARTY_ADJACENT_RE = re.compile(rf"^([A-Z][A-Za-z\s,.'-]+?)\s+(?:{_CALL_MARKER_ALT})\b", re.IGNORECASE)


def extract_call_event(subject: str) -> tuple[str | None, str]:
    """Returns (raw_party, 'TCON'|'OCON'), or (None, '') if no call marker is present."""
    marker = _CALL_MARKER_RE.search(subject)
    if not marker:
        return None, ""
    call_type = "TCON" if marker.group(0).upper().startswith("T") else "OCON"

    prefix = subject[: marker.start()]
    party_match = _CALL_PARTY_DASH_RE.match(subject) if "-" in prefix else _CALL_PARTY_ADJACENT_RE.match(subject)
    party_raw = party_match.group(1).strip().rstrip(",- ").strip() if party_match else None
    return party_raw, call_type


# Deadline reminders ("WATERMAN ... DUE BY TODAY", "Metros docs due today") —
# a third, deliberately even simpler fallback than calls. Real-data check
# found the party is essentially always the leading word of the subject
# (unlike hearings/calls, there's no purpose code to anchor on) — a bare
# `[A-Za-z]+` run stops cleanly at the first space/dash/comma, which handles
# both "WATERMAN OP'S RESPONSE..." (space-separated) and "Pitters--Trial
# Brief..." (double-dash, no space). Requires the leading word not be "DUE"
# itself (e.g. "Due to rain...") — that's not a deadline-with-a-client-name.
_DUE_RE = re.compile(r"\bDUE\b", re.IGNORECASE)
_DUE_LEAD_WORD_RE = re.compile(r"^([A-Za-z]+(?:'[A-Za-z]+)?)")


def extract_due_event(subject: str) -> str | None:
    """Returns the raw party text if `subject` reads as a deadline reminder
    (contains "due" as a whole word, with a real leading name), else None."""
    if not _DUE_RE.search(subject):
        return None
    lead = _DUE_LEAD_WORD_RE.match(subject.strip())
    if not lead:
        return None
    word = lead.group(1)
    if word.upper() in ("DUE", "DEADLINE"):
        return None
    return word

# Outlook's location field is often the department code verbatim ("603", "N17",
# "D-10"), but sometimes a phone number or a note ("OC TO INITIATE") — only trust
# it as a dept when the WHOLE field matches, not a substring (a phone number like
# "619-433-9374" contains plenty of bare 3-digit-looking chunks). Any single
# letter is accepted (not just N/E/S/C) since SD family court also uses "D"
# departments; separator may be a hyphen or a space ("N 18").
_BARE_DEPT_RE = re.compile(r"^\d{2,3}$")
_LETTER_DEPT_RE = re.compile(r"^[A-Z][-\s]?\d{1,2}$", re.IGNORECASE)


def _dept_from_location(location: str) -> str:
    cleaned = location.strip()
    if _BARE_DEPT_RE.match(cleaned) or _LETTER_DEPT_RE.match(cleaned):
        return normalize_dept(re.sub(r"\s+", "-", cleaned))
    return ""


@dataclass
class MatchedEvent:
    outlook_event: OutlookEvent
    party: str | None
    purpose_code: str
    dept: str
    matter_id: int | None
    matter_display_number: str | None
    ambiguous: bool
    shared_last_name: bool = False  # matched client's last name also has another open matter
    reason: str = ""  # set when matter_id is None, explains why (for the exceptions file)


def _resolve_matter(
    party: str | None,
    matters_index: dict[str, int | None],
) -> tuple[int | None, bool]:
    if not party:
        return None, False
    if party in matters_index:
        return matters_index[party], matters_index[party] is None

    candidates = {mid for name, mid in matters_index.items() if mid and party_names_match(party, name)}
    if len(candidates) == 1:
        return next(iter(candidates)), False
    if len(candidates) > 1:
        return None, True
    return None, False


def match_event(
    event: OutlookEvent,
    matters_index: dict[str, int | None],
    display_by_id: dict[int, str],
    purpose_mappings: dict[str, str],
    shared_last_names: set[str] = frozenset(),
) -> MatchedEvent:
    text = f"{event.subject} {event.body_preview}"

    # Hearings first (FRC/RFO/MSC/etc., via purpose_mappings); calls (TCON/OCON)
    # are a fallback only when nothing hearing-like was found — the two
    # conventions haven't been seen to overlap in the same subject in practice.
    party_raw = extract_party_name(event.subject) or extract_party_name(event.body_preview)
    purposes = extract_all_purposes(text)
    purpose_code = normalize_purpose(purposes[0], purpose_mappings) if purposes else ""

    if not party_raw:
        call_party, call_type = extract_call_event(event.subject)
        if call_party:
            party_raw = call_party
            purpose_code = call_type

    if not party_raw:
        due_party = extract_due_event(event.subject)
        if due_party:
            party_raw = due_party
            purpose_code = "DUE"

    party = normalize_party_name(party_raw) if party_raw else None
    matter_id, ambiguous = _resolve_matter(party, matters_index)

    dept = (
        _dept_from_location(event.location)
        or extract_dept_from_text(event.subject)
        or extract_dept_from_text(event.body_preview)
        or ""
    )

    reason = ""
    if matter_id is None:
        if not party:
            reason = "Could not extract a party/client name from the event subject"
        elif ambiguous:
            reason = "Client has multiple open matters — no case number available here to disambiguate"
        else:
            reason = "No matching open matter found"

    matter_display_number = display_by_id.get(matter_id) if matter_id else None
    last_name = (matter_display_number or "").split(",")[0].strip().upper()

    return MatchedEvent(
        outlook_event=event,
        party=party,
        purpose_code=purpose_code,
        dept=dept,
        matter_id=matter_id,
        matter_display_number=matter_display_number,
        ambiguous=ambiguous,
        shared_last_name=bool(last_name) and last_name in shared_last_names,
        reason=reason,
    )


def looks_client_relevant(subject: str) -> bool:
    """Would any of the three detectors above (hearing/call/DUE) find
    something in this subject? Shared between outlook_migration.py (which
    only wants client-relevant events) and outlook_recurring_availability.py
    (which explicitly excludes them — recurring client events, if any ever
    show up, belong in the per-occurrence matching path instead)."""
    if extract_all_purposes(subject) and extract_party_name(subject):
        return True
    if extract_call_event(subject)[0]:
        return True
    if extract_due_event(subject):
        return True
    return False
