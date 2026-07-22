"""
interactive_resolve.py — Last-resort interactive resolution for TCON/OCON
calls and DUE deadline reminders that matched neither a client matter nor an
OC/OP contact. Only these purpose codes reach this (not hearings) — a
hearing still just lands in the ordinary exceptions file. Runs automatically
whenever such events exist; skipped entirely when stdin isn't a terminal
(e.g. an automated/non-interactive invocation) so it can't hang unattended.
"""

import logging
import sys

from outlook_calendar.call_overrides import append_override
from outlook_calendar.event_parser import MatchedEvent

MatchKey = tuple[str, str]  # (subject, date isoformat)


def _search_matters(query: str, matters: list[dict]) -> list[dict]:
    q = query.strip().upper()
    if not q:
        return []
    return [m for m in matters if q in (m.get("display_number") or "").upper()][:10]


def resolve_calls_interactively(
    unresolved_calls: list[MatchedEvent],
    matters: list[dict],
    display_by_id: dict[int, str],
    shared_last_names: set[str],
) -> tuple[set[MatchKey], set[MatchKey]]:
    """Prompts once per event: search open matters by typed text, pick a
    number, or press Enter to skip. Mutates matter_id/matter_display_number/
    ambiguous/reason/shared_last_name in place on whatever the user resolves
    — the same fields match_event() would have set, so build_export()
    downstream treats a manually-resolved call exactly like an auto-matched
    one. Returns (resolved_keys, skipped_keys) for the calls report.

    Each decision is persisted via append_override() the instant it's made,
    not batched until the whole session finishes — so Ctrl+C (or Ctrl+D/EOF)
    partway through only loses whatever hasn't been answered yet, never
    answers already given. A KeyboardInterrupt or EOFError here is caught and
    treated as "stop asking, leave the rest for next run" rather than
    crashing the script before the caller gets a chance to write any output
    files at all."""
    resolved: set[MatchKey] = set()
    skipped: set[MatchKey] = set()

    if not unresolved_calls:
        return resolved, skipped

    if not sys.stdin.isatty():
        logging.warning(
            "%d calls need manual resolution but stdin isn't a terminal — skipping interactive resolution.",
            len(unresolved_calls),
        )
        return resolved, skipped

    print(f"\n{len(unresolved_calls)} calls need manual resolution (no client or OC/OP match found).")
    print("For each: type part of a name to search open matters, pick a number, or press Enter to skip.\n")

    try:
        for m in unresolved_calls:
            event = m.outlook_event
            date_str = event.start.date().isoformat() if event.start else "?"
            key: MatchKey = (event.subject, date_str)

            print(f'--- {date_str}  "{event.subject}"  (location: {event.location or "-"})')
            while True:
                query = input("  Search matter (Enter to skip): ").strip()
                if not query:
                    skipped.add(key)
                    append_override(m.party or "", "skipped", None, "")
                    logging.info("Skipped call in interactive resolution: %s (%s)", event.subject, date_str)
                    break

                candidates = _search_matters(query, matters)
                if not candidates:
                    print("  No matches — try again or press Enter to skip.")
                    continue

                for i, c in enumerate(candidates, start=1):
                    print(f"    {i}. {c.get('display_number')}")
                choice = input("  Pick a number (Enter to search again, 's' to skip): ").strip()
                if not choice:
                    continue
                if choice.lower() == "s":
                    skipped.add(key)
                    append_override(m.party or "", "skipped", None, "")
                    logging.info("Skipped call in interactive resolution: %s (%s)", event.subject, date_str)
                    break
                if choice.isdigit() and 1 <= int(choice) <= len(candidates):
                    picked = candidates[int(choice) - 1]
                    matter_id = int(picked["id"])
                    m.matter_id = matter_id
                    m.matter_display_number = display_by_id.get(matter_id, picked.get("display_number", ""))
                    m.ambiguous = False
                    m.reason = ""
                    last_name = (m.matter_display_number or "").split(",")[0].strip().upper()
                    m.shared_last_name = last_name in shared_last_names
                    resolved.add(key)
                    append_override(m.party or "", "matched", m.matter_id, m.matter_display_number or "")
                    logging.info(
                        "Manually resolved call: %s (%s) -> matter %s",
                        event.subject, date_str, m.matter_display_number,
                    )
                    break
                print("  Not a valid choice — try again.")
    except (EOFError, KeyboardInterrupt) as exc:
        # No more input available (piped/redirected stdin ran dry, the user
        # hit Ctrl+D/Ctrl+Z, or a real Ctrl+C) — isatty() alone isn't a
        # reliable guard for the first case in every shell, so this is the
        # actual backstop. Whatever wasn't answered yet is left unresolved
        # (NOT persisted as skipped — there was no real decision) so it comes
        # back up for a real answer next run; anything already answered this
        # session is already on disk via append_override() above.
        reason = "Ctrl+C" if isinstance(exc, KeyboardInterrupt) else "EOF"
        remaining = [ev for ev in unresolved_calls if event_key(ev) not in resolved and event_key(ev) not in skipped]
        print()
        logging.warning(
            "Interactive resolution stopped early (%s) — %d already-answered call(s) saved, "
            "%d remaining call(s) left for next run.",
            reason, len(resolved) + len(skipped), len(remaining),
        )
        for ev in remaining:
            skipped.add(event_key(ev))

    return resolved, skipped


def event_key(m: MatchedEvent) -> MatchKey:
    event = m.outlook_event
    date_str = event.start.date().isoformat() if event.start else "?"
    return (event.subject, date_str)
