"""
outlook_migration.py — One-time backfill: read Heidi Collier's Outlook
calendar, match events to Clio matters, and generate a Clio-importable
calendar events CSV (Clio: Settings > Data Import > Calendar Events).

This script only READS from Outlook (Microsoft Graph) and Clio (matters +
existing calendar entries, for a duplicate check) — it never writes
anything to Clio directly. All the actual writing happens when you import
the generated CSV through Clio's own UI, which Clio lets you undo there if
something's wrong — deliberately chosen over the API for this migration.

Prerequisite: uv run src/outlook_auth.py   (one-time browser consent)

Usage:
  uv run src/outlook_migration.py --from-date 2024-01-01 --to-date 2026-07-14
  uv run src/outlook_migration.py --from-date 2024-01-01 --to-date 2026-07-14 --matter mcghee
"""

import argparse
import csv
import logging
import os
import sys
from datetime import date
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

from court_calendar.clio_calendar import fetch_calendar_entries
from court_calendar.store import get_purpose_mappings
from matter_matching import fetch_open_matters, index_by_last_name
from outlook_calendar.csv_export import CSV_FIELDNAMES, build_export
from outlook_calendar.event_parser import match_event
from outlook_calendar.graph_client import fetch_calendar_events

# Invoice/event last-name -> Clio matter ID overrides, same convention as the
# other subprojects. Add entries here after a run surfaces exceptions.
MANUAL_MATTER_MAP: dict[str, int] = {}

# Outlook category -> staff member whose events are out of scope for this
# migration (handled separately / by them directly) — skipped entirely,
# before matching, based on Heidi's actual color-coding convention.
SKIP_CATEGORIES: dict[str, str] = {
    "Purple category": "Dahann",
    "Green category": "Pam",
}


def setup_logging(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"outlook_migration_{date.today().strftime('%Y%m%d')}.log"
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
    ap.add_argument("--from-date", required=True, help="Start of range, YYYY-MM-DD")
    ap.add_argument("--to-date", required=True, help="End of range, YYYY-MM-DD")
    ap.add_argument("--matter", default="", help="Process only events whose matched client name contains this string")
    args = ap.parse_args()

    setup_logging(Path("logs"))

    logging.info("Fetching Outlook events for %s to %s ...", args.from_date, args.to_date)
    all_events = fetch_calendar_events(args.from_date, args.to_date)
    logging.info("Fetched %d Outlook events", len(all_events))

    outlook_events = []
    skipped_by_category = {name: 0 for name in SKIP_CATEGORIES.values()}
    for event in all_events:
        skip_owner = next((owner for cat, owner in SKIP_CATEGORIES.items() if cat in event.categories), None)
        if skip_owner:
            skipped_by_category[skip_owner] += 1
        else:
            outlook_events.append(event)

    for owner, count in skipped_by_category.items():
        logging.info("Skipped %d events categorized for %s", count, owner)
    logging.info("%d events remain after category filtering", len(outlook_events))

    session = _clio_session()

    logging.info("Fetching open Clio matters ...")
    matters = fetch_open_matters(session)
    matters_index = index_by_last_name(matters)
    display_by_id = {int(m["id"]): m.get("display_number", "") for m in matters if m.get("display_number")}
    logging.info("Indexed %d open matters", len(display_by_id))

    logging.info("Fetching existing Clio calendar entries (duplicate check) ...")
    existing_entries = fetch_calendar_entries(args.from_date, args.to_date, session)
    logging.info("Found %d existing Clio calendar entries in range", len(existing_entries))

    purpose_mappings = get_purpose_mappings()

    matched = []
    for event in outlook_events:
        if event.start is None:
            continue
        m = match_event(event, matters_index, display_by_id, purpose_mappings)

        if m.matter_id is None:
            last_name = (m.party or "").upper()
            if last_name in MANUAL_MATTER_MAP:
                m.matter_id = MANUAL_MATTER_MAP[last_name]
                m.matter_display_number = display_by_id.get(m.matter_id, last_name)
                m.reason = ""

        matched.append(m)

    if args.matter:
        f = args.matter.upper()
        matched = [m for m in matched if m.party and f in m.party]
        logging.info("--matter '%s' matched %d events", args.matter, len(matched))
        if not matched:
            logging.error("No events matched --matter '%s'", args.matter)
            sys.exit(1)

    result = build_export(matched, existing_entries)

    output_dir = Path("output")
    output_dir.mkdir(exist_ok=True)
    stem = f"outlook_migration_{args.from_date}_to_{args.to_date}"

    csv_path = output_dir / f"{stem}.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDNAMES)
        writer.writeheader()
        writer.writerows(result.rows)
    logging.info("Wrote %d rows -> %s", len(result.rows), csv_path)
    logging.info("Every 'OP/CL' in the subject column needs manual review before importing — see CLAUDE.md.")

    if result.exceptions:
        exc_path = output_dir / f"{stem}_exceptions.csv"
        with open(exc_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["subject", "date", "party", "reason"])
            writer.writeheader()
            writer.writerows(result.exceptions)
        logging.warning("Wrote %d exceptions -> %s", len(result.exceptions), exc_path)

    if result.already_in_clio:
        skip_path = output_dir / f"{stem}_already_in_clio.csv"
        with open(skip_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["subject", "date", "matter"])
            writer.writeheader()
            writer.writerows(result.already_in_clio)
        logging.info("Wrote %d already-in-Clio skips -> %s", len(result.already_in_clio), skip_path)

    logging.info(
        "Summary: %d Outlook events, %d rows for import, %d exceptions, %d already in Clio",
        len(outlook_events), len(result.rows), len(result.exceptions), len(result.already_in_clio),
    )


if __name__ == "__main__":
    main()
