"""
call_overrides.py — Persists interactive_resolve.py's manual call-resolution
decisions across runs, so re-running the migration with a wider --to-date
(the normal periodic workflow — see CLAUDE.md) doesn't re-prompt for the
same person's calls every time.

Keyed by the extracted party text (same convention as outlook_migration.py's
own MANUAL_MATTER_MAP) — a person's calls recur across dates, but the party
text extracted from their subject line is stable.

CSV, not JSON: matches this repo's audit-everything-via-CSV convention, and
is directly hand-editable if a bad match slips through (fix or delete the row).
"""

import csv
import logging
from datetime import datetime, timezone
from pathlib import Path

OVERRIDES_PATH = Path("data/outlook_call_overrides.csv")
FIELDNAMES = ["party", "decision", "matter_id", "matter_display_number", "resolved_at"]


def load_overrides() -> tuple[dict[str, int], set[str]]:
    """Returns (party -> matter_id for 'matched' rows, set of party for 'skipped' rows).
    Last row wins per party if the file has been hand-edited with duplicates."""
    matched: dict[str, int] = {}
    skipped: set[str] = set()
    if not OVERRIDES_PATH.exists():
        return matched, skipped

    with open(OVERRIDES_PATH, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            party = (row.get("party") or "").strip().upper()
            if not party:
                continue
            if row.get("decision") == "matched" and row.get("matter_id"):
                matched[party] = int(row["matter_id"])
                skipped.discard(party)
            elif row.get("decision") == "skipped":
                skipped.add(party)
                matched.pop(party, None)

    return matched, skipped


def append_override(party: str, decision: str, matter_id: int | None, matter_display_number: str) -> None:
    OVERRIDES_PATH.parent.mkdir(parents=True, exist_ok=True)
    is_new = not OVERRIDES_PATH.exists()
    with open(OVERRIDES_PATH, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        if is_new:
            writer.writeheader()
        writer.writerow({
            "party": party.strip().upper(),
            "decision": decision,
            "matter_id": matter_id or "",
            "matter_display_number": matter_display_number or "",
            "resolved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        })
    logging.info("Recorded call override: %r -> %s", party, decision)
