"""
event_parser.py — Matches Outlook events to Clio matters and extracts
purpose/department, reusing the same free-text extraction already built for
calendar-check-style subjects in court_calendar/normalizer.py.

Two event types are recognized:
  - Hearings: FRC/RFO/MSC/etc. — goes through the normal purpose_mappings.
  - Calls: TCON (telephone) / OCON (office/in-person) — a deliberately
    separate, simpler path per project decision ("doesn't need the purpose
    mappings"). Real-data check found bare "OC" is unsafe to treat as a call
    marker — in practice it means "Opposing Counsel", not "office conference"
    (e.g. "SATTERLY OC's Responsive Dec due") — so only the unambiguous forms
    (TCON, OCON, TCN, T/C, T-C, T.C.) are recognized; a lone "OC"/"TC" is not.

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

# Outlook's location field is often the department code verbatim ("603", "N17"),
# but sometimes a phone number or a note ("OC TO INITIATE") — only trust it as
# a dept when the WHOLE field matches, not a substring (a phone number like
# "619-433-9374" contains plenty of bare 3-digit-looking chunks).
_BARE_DEPT_RE = re.compile(r"^\d{2,3}$")
_LETTER_DEPT_RE = re.compile(r"^[NESC]-?\d{1,2}$", re.IGNORECASE)


def _dept_from_location(location: str) -> str:
    cleaned = location.strip()
    if _BARE_DEPT_RE.match(cleaned) or _LETTER_DEPT_RE.match(cleaned):
        return normalize_dept(cleaned)
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

    return MatchedEvent(
        outlook_event=event,
        party=party,
        purpose_code=purpose_code,
        dept=dept,
        matter_id=matter_id,
        matter_display_number=display_by_id.get(matter_id) if matter_id else None,
        ambiguous=ambiguous,
        reason=reason,
    )
