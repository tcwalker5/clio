"""
csv_export.py — Builds a Clio-importable calendar events CSV from matched
Outlook events. Matches Clio Manage's "Calendar events" import template
exactly (Settings > Data Import > Calendar Events):

  Required: start_date (MM/DD/YYYY), end_date (MM/DD/YYYY),
            start_time (H:MM:SS AM/PM), end_time (H:MM:SS AM/PM), subject
  Optional: matter (must exactly match a matter display number), location,
            description

This module only reads from Clio (to check for entries that already exist,
so re-running this doesn't suggest duplicates) — it never writes to Clio.
All writing happens when the generated CSV is imported through Clio's own
UI, which can be undone there if something's wrong — the whole point of
using the CSV path instead of the API for this one-time migration.
"""

from dataclasses import dataclass

from court_calendar.clio_calendar import CalendarEntry, index_by_matter_id
from outlook_calendar.event_parser import OP_CL_PLACEHOLDER, MatchedEvent

CSV_FIELDNAMES = ["start_date", "end_date", "start_time", "end_time", "subject", "matter", "location", "description"]


@dataclass
class ExportResult:
    rows: list[dict]              # ready to write via csv.DictWriter
    exceptions: list[dict]        # unmatched / ambiguous — needs manual resolution
    already_in_clio: list[dict]   # matched, but Clio already has an entry that day — skipped


def _format_date(dt) -> str:
    return dt.strftime("%m/%d/%Y")


def _format_clio_time(dt) -> str:
    return dt.strftime("%I:%M:%S %p").lstrip("0")


def _format_title_time(dt) -> str:
    return dt.strftime("%I:%M %p").lstrip("0")


def _is_all_day(m: MatchedEvent) -> bool:
    """A deadline reminder (purpose_code "DUE") is always treated as all-day
    on import regardless of how Outlook recorded it — a deadline note has no
    meaningful clock time, and forcing it avoids importing e.g. a reminder
    Heidi set for 9am as if 9am were the actual deadline."""
    return m.outlook_event.is_all_day or m.purpose_code == "DUE"


def _party_display(m: MatchedEvent) -> str:
    """Last name from the matched Clio matter (authoritative — not whatever text
    was parsed off the Outlook subject, which can attach the wrong first name to
    a shared last name, e.g. a "BONNIE LARSEN" call matching client LARSEN, NOEL).
    Only widened to "Last, F" when another open matter shares the same last name;
    otherwise the bare last name matches the existing hearing-title convention."""
    last = (m.matter_display_number or m.party or "").split(",")[0].strip().upper()
    if m.shared_last_name:
        first = (m.matter_display_number or "").split(",")[1].strip() if "," in (m.matter_display_number or "") else ""
        if first:
            return f"{last}, {first[0].upper()}"
    return last


def build_title(m: MatchedEvent) -> str:
    """{client last[, First Initial]} {purpose} [{OP/CL}] {Time} — OP/CL only
    applies to RFOs (it indicates who's asking for the order); every other
    purpose omits that token entirely rather than carrying a meaningless
    placeholder. Department isn't in the title at all — see build_export()'s
    description line, which is where it lives now (normalized, and only when
    present, instead of a "?" placeholder that can't distinguish "not parsed"
    from "doesn't apply", e.g. to a phone call)."""
    time_str = "12:00 PM" if _is_all_day(m) else _format_title_time(m.outlook_event.start)
    purpose = m.purpose_code or "?"
    parts = [_party_display(m), purpose]
    if m.purpose_code == "RFO":
        parts.append(OP_CL_PLACEHOLDER)
    parts.append(time_str)
    return " ".join(parts)


def _already_in_clio(m: MatchedEvent, entries_by_matter: dict[int, list[CalendarEntry]]) -> bool:
    candidates = entries_by_matter.get(m.matter_id, [])
    return any(e.start_at.date() == m.outlook_event.start.date() for e in candidates)


def build_export(matched_events: list[MatchedEvent], existing_clio_entries: list[CalendarEntry]) -> ExportResult:
    entries_by_matter = index_by_matter_id(existing_clio_entries)

    rows: list[dict] = []
    exceptions: list[dict] = []
    already_in_clio: list[dict] = []

    for m in matched_events:
        event = m.outlook_event

        if m.matter_id is None:
            exceptions.append({
                "subject": event.subject,
                "date": event.start.date().isoformat() if event.start else "",
                "party": m.party or "",
                "reason": m.reason,
            })
            continue

        if _already_in_clio(m, entries_by_matter):
            already_in_clio.append({
                "subject": event.subject,
                "date": event.start.date().isoformat(),
                "matter": m.matter_display_number,
            })
            continue

        start = event.start
        end = event.end or event.start
        if _is_all_day(m):
            # Must match Clio's H:MM:SS AM/PM format like every other row —
            # a bare "12:00 PM" (no seconds) is what caused the real
            # "Invalid Date" import failures on all-day/DUE rows.
            start_time = end_time = "12:00:00 PM"
        else:
            start_time = _format_clio_time(start)
            end_time = _format_clio_time(end)

        description = f"Migrated from Outlook: \"{event.subject}\""
        if m.dept:
            description += f" — {m.dept}"

        rows.append({
            "start_date": _format_date(start),
            "end_date": _format_date(end),
            "start_time": start_time,
            "end_time": end_time,
            "subject": build_title(m),
            "matter": m.matter_display_number or "",
            "location": event.location,
            "description": description,
        })

    return ExportResult(rows=rows, exceptions=exceptions, already_in_clio=already_in_clio)
