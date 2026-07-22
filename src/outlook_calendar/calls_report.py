"""
calls_report.py — A review CSV covering every TCON/OCON call *and* DUE
deadline reminder the migration found, regardless of how it ended up
resolved, so they can be visually compared as one set instead of picked out
by eye from the general exceptions/import files. DUE events share this same
file/pipeline (not a separate one) since both are "purpose codes with no
purpose_mappings entry, resolved through the same manual/OC-OP fallback" —
the file is still named `_calls.csv` for continuity with what's already
documented in CLAUDE.md, even though it now covers a second event type too.
"""

from outlook_calendar.event_parser import MatchedEvent
from outlook_calendar.interactive_resolve import MatchKey
from outlook_calendar.relationships import OcOpContact, match_exception_to_oc_op

# Purpose codes resolved through the shared manual/OC-OP pipeline instead of
# purpose_mappings — calls (TCON/OCON) and deadline reminders (DUE).
MANUAL_RESOLUTION_PURPOSE_CODES = {"TCON", "OCON", "DUE"}


def needs_manual_resolution(m: MatchedEvent) -> bool:
    return m.purpose_code in MANUAL_RESOLUTION_PURPOSE_CODES


CALLS_CSV_FIELDNAMES = ["subject", "date", "purpose_code", "resolution", "matter", "note"]


def build_calls_report(
    calls: list[MatchedEvent],
    oc_op_contacts: list[OcOpContact],
    resolved_keys: set[MatchKey],
    skipped_keys: set[MatchKey],
) -> list[dict]:
    rows: list[dict] = []
    for m in calls:
        event = m.outlook_event
        date_str = event.start.date().isoformat() if event.start else ""
        key: MatchKey = (event.subject, date_str)

        if m.matter_id is not None:
            resolution = "Manually resolved" if key in resolved_keys else "Matched to client matter"
            matter, note = m.matter_display_number or "", ""
        else:
            oc_op_matches = match_exception_to_oc_op({"subject": event.subject, "party": m.party or ""}, oc_op_contacts)
            if oc_op_matches:
                primary = oc_op_matches[0]
                resolution = primary.role
                matter = primary.matter_display_number
                note = f"{primary.name} ({primary.matter_status})"
                if len(oc_op_matches) > 1:
                    note += f" — {len(oc_op_matches) - 1} other possible match(es)"
            elif key in skipped_keys:
                resolution, matter, note = "Skipped", "", m.reason
            else:
                resolution, matter, note = "Unresolved", "", m.reason

        rows.append({
            "subject": event.subject,
            "date": date_str,
            "purpose_code": m.purpose_code,
            "resolution": resolution,
            "matter": matter,
            "note": note,
        })
    return rows
