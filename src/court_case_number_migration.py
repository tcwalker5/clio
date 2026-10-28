"""
court_case_number_migration.py — One-time consolidation of the matter custom
field "Court Case #" into "Court Case Number", before "Court Case #" is deleted.

Both fields exist on Matters (2026-10-28 check of open matters: 91 had
"Court Case Number", 37 had "Court Case #", 16 had only "Court Case #", 2 had
different numbers in each). Court Calendar Sync only reads "Court Case Number"
(court_calendar/matter_fields.py), so numbers that live only in "Court Case #"
are invisible to it.

For every matter of every status (closed matters too: deleting the field
loses their values just the same) with "Court Case #" filled:
  - "Court Case Number" blank       -> copy "Court Case #" into it
  - both filled, same number        -> nothing to do
  - both filled, different numbers  -> flagged, never overwritten; fix by hand
Numbers compare ignoring case and spaces. "Court Case #" itself is left as-is.

Writes output/court_case_number_migration_YYYYMMDD.csv with every such
matter and what happened to it, so the conflicts can be worked through.

Usage:
  uv run src/court_case_number_migration.py --dry-run   # always first
  uv run src/court_case_number_migration.py
"""

import argparse
import csv
import logging
import sys
from datetime import date
from pathlib import Path

import requests
from dotenv import load_dotenv

from clio_token import CLIO_AUTH

load_dotenv()

from court_calendar.clio_matter_update import update_matter_case_number
from matter_matching import fetch_open_matters

OLD_FIELD = "Court Case #"
NEW_FIELD = "Court Case Number"
ALL_STATUSES = "open,pending,closed"
MATTER_FIELDS = "id,display_number,status,custom_field_values{field_name,value}"
PROJECT_DIR = Path(__file__).resolve().parent.parent


def setup_logging(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"court_case_number_migration_{date.today().strftime('%Y%m%d')}.log"
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


def _field_values(matter: dict) -> dict[str, str]:
    # str(): some other custom fields come back as booleans/numbers.
    return {v.get("field_name"): str(v.get("value") or "").strip() for v in matter.get("custom_field_values") or []}


def _same_number(a: str, b: str) -> bool:
    return "".join(a.split()).upper() == "".join(b.split()).upper()


def plan(matters: list[dict]) -> list[dict]:
    """One row per matter with "Court Case #" filled, with the action to take."""
    rows = []
    for m in matters:
        values = _field_values(m)
        old, new = values.get(OLD_FIELD, ""), values.get(NEW_FIELD, "")
        if not old:
            continue
        if not new:
            action = "copy"
        elif _same_number(old, new):
            action = "same"
        else:
            action = "conflict"
        rows.append({
            "matter_id": int(m["id"]),
            "display_number": m.get("display_number", ""),
            "status": m.get("status", ""),
            OLD_FIELD: old,
            NEW_FIELD: new,
            "action": action,
        })
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="Report what would change; write nothing to Clio")
    args = ap.parse_args()

    setup_logging(PROJECT_DIR / "logs")
    logging.info("Mode: %s", "DRY RUN" if args.dry_run else "LIVE")

    session = requests.Session()
    session.headers.update({"Content-Type": "application/json"})
    session.auth = CLIO_AUTH

    matters = fetch_open_matters(session, fields=MATTER_FIELDS, status=ALL_STATUSES)
    rows = plan(matters)
    logging.info("Matters checked: %d — with %s filled: %d", len(matters), OLD_FIELD, len(rows))

    for row in rows:
        if row["action"] == "conflict":
            logging.warning("CONFLICT  matter %s %s (%s): %s=%r vs %s=%r — not changed",
                            row["matter_id"], row["display_number"], row["status"],
                            OLD_FIELD, row[OLD_FIELD], NEW_FIELD, row[NEW_FIELD])
        elif row["action"] == "copy":
            if args.dry_run:
                logging.info("WOULD COPY matter %s %s (%s): %s=%r",
                             row["matter_id"], row["display_number"], row["status"], NEW_FIELD, row[OLD_FIELD])
                continue
            try:
                update_matter_case_number(session, row["matter_id"], row[OLD_FIELD])
                row["action"] = "copied"
                logging.info("COPIED    matter %s %s (%s): %s=%r",
                             row["matter_id"], row["display_number"], row["status"], NEW_FIELD, row[OLD_FIELD])
            except (RuntimeError, requests.RequestException) as e:
                row["action"] = "failed"
                logging.error("FAILED    matter %s %s: %s", row["matter_id"], row["display_number"], e)

    out_dir = PROJECT_DIR / "output"
    out_dir.mkdir(exist_ok=True)
    suffix = "_dry_run" if args.dry_run else ""
    out_file = out_dir / f"court_case_number_migration_{date.today().strftime('%Y%m%d')}{suffix}.csv"
    with out_file.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["action", "matter_id", "display_number", "status", OLD_FIELD, NEW_FIELD])
        writer.writeheader()
        writer.writerows(sorted(rows, key=lambda r: (r["action"], r["display_number"])))

    counts: dict[str, int] = {}
    for row in rows:
        counts[row["action"]] = counts.get(row["action"], 0) + 1
    logging.info("Summary: %s", ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "nothing to do")
    logging.info("Report: %s", out_file)


if __name__ == "__main__":
    main()
