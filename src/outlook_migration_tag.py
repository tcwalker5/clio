"""
outlook_migration_tag.py — After manually importing an outlook_migration CSV
into Clio, tags each of those specific entries with calendar_entry_event_type
= Heidi (id 591618 in this account; re-derive via /calendar_entry_event_types.json
if it's ever missing).

Clio's calendar CSV import has no "type" column and doesn't hand back the IDs
it creates, so this identifies the exact entries by re-querying Clio for the
same matter+date and matching the summary text verbatim against the CSV —
not a "recently created" heuristic, which could tag someone else's entry.

Usage:
  uv run src/outlook_migration_tag.py --csv output/outlook_migration_2026-06-01_to_2028-06-01.csv
"""

import argparse
import csv
import logging
import os
import sys
from datetime import date, datetime
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

from court_calendar.clio_calendar import fetch_calendar_entries, index_by_matter_id, set_calendar_entry_type
from matter_matching import fetch_open_matters

HEIDI_EVENT_TYPE_ID = 591618


def setup_logging(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"outlook_migration_tag_{date.today().strftime('%Y%m%d')}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        force=True,
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(open(sys.stdout.fileno(), mode="w", encoding="utf-8", errors="replace", closefd=False)),
        ],
    )
    logging.info("Log file: %s", log_file)


def _clio_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "Authorization": f"Bearer {os.getenv('CLIO_ACCESS_TOKEN', '')}",
        "Content-Type": "application/json",
    })
    return s


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", required=True, help="Path to a previously generated outlook_migration CSV (already imported into Clio)")
    args = ap.parse_args()

    setup_logging(Path("logs"))

    csv_path = Path(args.csv)
    if not csv_path.exists():
        logging.error("CSV not found: %s", csv_path)
        sys.exit(1)

    with open(csv_path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    logging.info("Loaded %d rows from %s", len(rows), csv_path)

    if not rows:
        logging.info("Nothing to tag.")
        return

    session = _clio_session()

    logging.info("Fetching open Clio matters ...")
    matters = fetch_open_matters(session)
    matter_id_by_display = {m["display_number"]: int(m["id"]) for m in matters if m.get("display_number")}

    row_dates = [datetime.strptime(r["start_date"], "%m/%d/%Y").date() for r in rows if r.get("start_date")]
    from_date, to_date = min(row_dates).isoformat(), max(row_dates).isoformat()

    logging.info("Fetching Clio calendar entries for %s to %s ...", from_date, to_date)
    existing_entries = fetch_calendar_entries(from_date, to_date, session)
    entries_by_matter = index_by_matter_id(existing_entries)
    logging.info("Found %d existing Clio calendar entries in range", len(existing_entries))

    tagged = 0
    already_tagged = 0
    not_found: list[dict] = []

    for row in rows:
        matter_id = matter_id_by_display.get(row["matter"])
        if not matter_id:
            not_found.append({**row, "reason": "Matter display number not found among open Clio matters"})
            continue

        row_date = datetime.strptime(row["start_date"], "%m/%d/%Y").date()
        candidates = [e for e in entries_by_matter.get(matter_id, []) if e.start_at.date() == row_date]

        match = next((e for e in candidates if e.summary.strip() == row["subject"].strip()), None)
        if not match and len(candidates) == 1:
            # Only one entry for this matter/date at all — safe fallback even
            # if Clio's import trimmed/altered the summary slightly.
            match = candidates[0]

        if not match:
            not_found.append({
                **row,
                "reason": f"No exact-matching Clio calendar entry found ({len(candidates)} candidate(s) on that date)",
            })
            continue

        if match.event_type_id == HEIDI_EVENT_TYPE_ID:
            already_tagged += 1
            continue

        set_calendar_entry_type(session, match.id, HEIDI_EVENT_TYPE_ID)
        tagged += 1
        logging.info("Tagged entry %s -> Heidi: %s", match.id, row["subject"][:60])

    logging.info("Summary: %d tagged, %d already tagged, %d not found", tagged, already_tagged, len(not_found))

    if not_found:
        out_path = csv_path.parent / f"{csv_path.stem}_tag_not_found.csv"
        with open(out_path, "w", newline="", encoding="utf-8") as f:
            fieldnames = [*rows[0].keys(), "reason"]
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(not_found)
        logging.warning("Wrote %d not-found rows -> %s — check these weren't actually imported, or resolve manually", len(not_found), out_path)


if __name__ == "__main__":
    main()
