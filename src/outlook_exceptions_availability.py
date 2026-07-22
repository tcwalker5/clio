"""
outlook_exceptions_availability.py — Imports every current outlook_migration.py
"exception" (an event that didn't match any open Clio matter) as a one-off
CalendarEntry on Heidi's PERSONAL Clio calendar via the API — same rationale
as outlook_recurring_availability.py's recurring series: these still occupy
real time on Heidi's calendar and should block her availability in Clio
too, even though they're not tied to a client matter.

Deliberately unfiltered by design (2026-07-15, explicit decision): every
remaining exception goes in as-is, including ones where a party WAS
extracted but didn't match a matter (those might really be unresolved
client work, not personal time — e.g. "DAVID PALMIOTT TCON") and genuinely
private items (a dinner, a birthday party). Rather than guess which is
which, everything goes in — if something's later resolved to a real client
matter through outlook_migration.py, having a duplicate, matter-less copy
on Heidi's personal calendar is harmless.

Reuses outlook_migration.gather_matched_events() directly instead of
reimplementing the fetch/match pipeline, so the two scripts can't define
"exception" differently and drift apart. This also means running this
script triggers the same interactive call-resolution prompts as a normal
outlook_migration.py run for anything not yet resolved — expected, and
already-resolved parties (from data/outlook_call_overrides.csv) won't
re-prompt.

Prerequisite:
  uv run src/outlook_auth.py --refresh
  uv run src/clio_auth.py --refresh

Usage:
  uv run src/outlook_exceptions_availability.py --from-date 2026-06-01 --to-date 2028-06-01 --dry-run
  uv run src/outlook_exceptions_availability.py --from-date 2026-06-01 --to-date 2028-06-01
"""

import argparse
import csv
import logging
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

load_dotenv()

from outlook_calendar.clio_write import CALENDAR_ENTRIES_ENDPOINT, HEIDI_CALENDAR_ID, create_calendar_entry
from outlook_migration import gather_matched_events

# How far back/forward to search Clio for an existing entry with the same
# (subject, start_at) before creating a new one — matches
# outlook_recurring_availability.py's dedup window convention.
DEDUP_WINDOW_YEARS = 6


def setup_logging(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"outlook_exceptions_availability_{date.today().strftime('%Y%m%d')}.log"
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


def _existing_heidi_entries(session: requests.Session) -> set[tuple[str, str]]:
    """Every (subject, start_at isoformat) pair already on Heidi's Clio
    calendar within a wide window — used for dedup so re-running this
    script doesn't create duplicates."""
    pacific = ZoneInfo("America/Los_Angeles")
    today = datetime.now()
    from_dt = datetime.combine((today - timedelta(days=365 * DEDUP_WINDOW_YEARS)).date(), time.min, tzinfo=pacific)
    to_dt = datetime.combine((today + timedelta(days=365 * DEDUP_WINDOW_YEARS)).date(), time.max, tzinfo=pacific)

    entries: set[tuple[str, str]] = set()
    next_url: str | None = None
    page = 1
    while True:
        if next_url:
            resp = session.get(next_url)
        else:
            resp = session.get(CALENDAR_ENTRIES_ENDPOINT, params={
                "fields": "id,summary,start_at,calendar_owner{id}",
                "from": from_dt.isoformat(),
                "to": to_dt.isoformat(),
                "limit": 200,
            })
        if resp.status_code != 200:
            raise RuntimeError(f"Failed to fetch existing Clio calendar entries (page {page}): {resp.status_code} {resp.text[:200]}")
        body = resp.json()
        for r in body.get("data", []):
            owner = r.get("calendar_owner") or {}
            if owner.get("id") == HEIDI_CALENDAR_ID:
                entries.add(((r.get("summary") or "").strip(), (r.get("start_at") or "").strip()))
        next_url = body.get("meta", {}).get("paging", {}).get("next")
        logging.info("Fetched existing Clio entries page %d", page)
        page += 1
        if not next_url:
            break
    return entries


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from-date", required=True, help="Start of range, YYYY-MM-DD")
    ap.add_argument("--to-date", required=True, help="End of range, YYYY-MM-DD")
    ap.add_argument("--dry-run", action="store_true", help="Preview without creating anything in Clio")
    args = ap.parse_args()

    setup_logging(Path("logs"))

    run = gather_matched_events(args.from_date, args.to_date)
    exceptions = [m for m in run.matched if m.matter_id is None]
    logging.info("%d exceptions found (unfiltered — all go to Heidi's personal calendar)", len(exceptions))

    session = run.session  # already-authenticated Clio session from gather_matched_events
    existing = _existing_heidi_entries(session)
    logging.info("Found %d existing entries on Heidi's Clio calendar (dedup window: +/-%d years)", len(existing), DEDUP_WINDOW_YEARS)

    rows: list[dict] = []
    for m in exceptions:
        event = m.outlook_event
        subject = event.subject
        if event.start is None:
            rows.append({"subject": subject, "date": "", "action": "error", "reason": "missing start time", "clio_entry_id": ""})
            continue

        key = (subject.strip(), event.start.isoformat())
        if key in existing:
            rows.append({"subject": subject, "date": event.start.date().isoformat(), "action": "already exists", "reason": "", "clio_entry_id": ""})
            continue

        # A DUE reminder that never resolved to a client still has no
        # meaningful clock time — same all-day forcing as csv_export.py's
        # _is_all_day() for the main import path.
        all_day = event.is_all_day or m.purpose_code == "DUE"
        end = event.end or event.start
        description = f'Migrated from Outlook: "{subject}"'

        logging.info("%s'%s' (%s)", "[DRY RUN] " if args.dry_run else "", subject, event.start.date().isoformat())

        if args.dry_run:
            rows.append({"subject": subject, "date": event.start.date().isoformat(), "action": "would create", "reason": "", "clio_entry_id": ""})
            continue

        try:
            created = create_calendar_entry(
                session,
                summary=subject,
                start_at=event.start,
                end_at=end,
                all_day=all_day,
                location=event.location,
                description=description,
            )
            entry_id = created.get("data", {}).get("id", "")
            rows.append({"subject": subject, "date": event.start.date().isoformat(), "action": "created", "reason": "", "clio_entry_id": entry_id})
        except RuntimeError as exc:
            logging.error("Failed to create '%s': %s", subject, exc)
            rows.append({"subject": subject, "date": event.start.date().isoformat(), "action": "error", "reason": str(exc), "clio_entry_id": ""})

    output_dir = Path("output")
    output_dir.mkdir(exist_ok=True)
    stem = f"outlook_exceptions_availability_{args.from_date}_to_{args.to_date}"
    csv_path = output_dir / f"{stem}{'_dry_run' if args.dry_run else ''}.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["subject", "date", "action", "reason", "clio_entry_id"])
        writer.writeheader()
        writer.writerows(rows)
    logging.info("Wrote %d rows -> %s", len(rows), csv_path)

    from collections import Counter
    counts = Counter(r["action"] for r in rows)
    logging.info("Summary: %s", dict(counts))


if __name__ == "__main__":
    main()
