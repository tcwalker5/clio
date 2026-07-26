"""
outlook_recurring_availability.py — Imports Heidi Collier's PERSONAL
recurring Outlook series (dog pickup, workouts, Rotary, tax reminders,
birthdays — things that aren't client-related but do occupy real time on
her calendar) into Clio as true recurring CalendarEntry records via the API.

Why the API, not the CSV import the rest of Outlook Calendar Migration uses: Clio's CSV
"Calendar events" import template has no recurrence column at all —
recurrence only exists on the live API's `recurrence_rule` field (confirmed
against real data, see outlook_calendar/recurrence.py's docstring). That
means this is the first script in this project that writes to Clio
directly, so it follows the project's standard live-write safety rules:
--dry-run required, log every request, retry on 429, continue on individual
failures, never silently overwrite (dedup check by subject before creating).

Scope is deliberately narrow: only series that are NOT client-relevant.
A real check of Heidi's calendar (2026-06-01 to 2028-06-01) found 35
recurring series, all personal/administrative — zero overlap with anything
outlook_migration.py would match as a call/hearing/deadline. This script
still runs that same hearing/call/DUE detection as a guard and skips (with
a warning) any series that WOULD look client-relevant, so a future change in
Heidi's calendar habits doesn't silently import a recurring client call
under the wrong path — those belong in outlook_migration.py instead.

Prerequisite:
  uv run src/outlook_auth.py --refresh   (Outlook token)
  uv run src/clio_auth.py --refresh      (Clio token)

Usage:
  uv run src/outlook_recurring_availability.py --from-date 2026-06-01 --to-date 2028-06-01 --dry-run
  uv run src/outlook_recurring_availability.py --from-date 2026-06-01 --to-date 2028-06-01
"""

import argparse
import csv
import logging
import sys
from datetime import date
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

from outlook_calendar.clio_write import HEIDI_CALENDAR_ID, CALENDAR_ENTRIES_ENDPOINT, clio_session, create_calendar_entry
from outlook_calendar.event_parser import looks_client_relevant
from outlook_calendar.graph_client import fetch_recurring_series
from outlook_calendar.recurrence import describe_pattern, pattern_to_rrule

# Same convention as outlook_migration.py's SKIP_CATEGORIES.
SKIP_CATEGORIES: dict[str, str] = {
    "Purple category": "Dahann",
    "Green category": "Pam",
}

# How far back/forward to search Clio for an existing entry with the same
# subject before creating a new one — a recurring series' master row can be
# anchored years in the past (e.g. "NC Third Thursday" started 2021), so a
# narrow from/to window risks missing an already-imported series and
# creating a duplicate.
DEDUP_WINDOW_YEARS = 6


def setup_logging(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"outlook_recurring_availability_{date.today().strftime('%Y%m%d')}.log"
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


def _existing_heidi_series(session: requests.Session) -> set[tuple[str, str]]:
    """Every (subject, recurrence_rule) pair already on Heidi's Clio calendar
    within a wide window — used for dedup so re-running this script doesn't
    create duplicates. Keyed on the pair, not subject alone: real data has
    two distinct series both literally named "PAY PROPERTY TAXES BY THE
    10TH" (two separate property tax installments, different schedules) —
    subject-only dedup would wrongly treat the second as a repeat of the
    first on any future re-run."""
    from datetime import datetime, time, timedelta
    from zoneinfo import ZoneInfo

    pacific = ZoneInfo("America/Los_Angeles")
    today = datetime.now()
    from_dt = datetime.combine((today - timedelta(days=365 * DEDUP_WINDOW_YEARS)).date(), time.min, tzinfo=pacific)
    to_dt = datetime.combine((today + timedelta(days=365 * DEDUP_WINDOW_YEARS)).date(), time.max, tzinfo=pacific)

    series: set[tuple[str, str]] = set()
    next_url: str | None = None
    page = 1
    while True:
        if next_url:
            resp = session.get(next_url)
        else:
            resp = session.get(CALENDAR_ENTRIES_ENDPOINT, params={
                "fields": "id,summary,recurrence_rule,calendar_owner{id}",
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
                series.add(((r.get("summary") or "").strip(), (r.get("recurrence_rule") or "").strip()))
        next_url = body.get("meta", {}).get("paging", {}).get("next")
        logging.info("Fetched existing Clio entries page %d", page)
        page += 1
        if not next_url:
            break
    return series


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from-date", required=True, help="Start of range to scan for recurring series, YYYY-MM-DD")
    ap.add_argument("--to-date", required=True, help="End of range to scan for recurring series, YYYY-MM-DD")
    ap.add_argument("--dry-run", action="store_true", help="Preview without creating anything in Clio")
    args = ap.parse_args()

    setup_logging(Path("logs"))

    logging.info("Fetching recurring series from Outlook (%s to %s) ...", args.from_date, args.to_date)
    all_series = fetch_recurring_series(args.from_date, args.to_date)
    logging.info("Found %d distinct recurring series", len(all_series))

    session = clio_session()
    existing_series = _existing_heidi_series(session)
    logging.info("Found %d existing series on Heidi's Clio calendar (dedup window: +/-%d years)", len(existing_series), DEDUP_WINDOW_YEARS)

    rows: list[dict] = []
    for series in all_series:
        skip_owner = next((owner for cat, owner in SKIP_CATEGORIES.items() if cat in series.categories), None)
        if skip_owner:
            rows.append({"subject": series.subject, "action": "skipped", "reason": f"categorized for {skip_owner}", "recurrence_rule": "", "clio_entry_id": ""})
            continue

        if looks_client_relevant(series.subject):
            logging.warning("Skipping '%s' — looks client-relevant (hearing/call/DUE), belongs in outlook_migration.py instead", series.subject)
            rows.append({"subject": series.subject, "action": "skipped", "reason": "looks client-relevant", "recurrence_rule": "", "clio_entry_id": ""})
            continue

        try:
            rrule = pattern_to_rrule(series.pattern)
        except ValueError as exc:
            logging.error("Could not translate recurrence for '%s': %s", series.subject, exc)
            rows.append({"subject": series.subject, "action": "error", "reason": str(exc), "recurrence_rule": "", "clio_entry_id": ""})
            continue

        if (series.subject.strip(), rrule) in existing_series:
            rows.append({"subject": series.subject, "action": "already exists", "reason": "", "recurrence_rule": rrule, "clio_entry_id": ""})
            continue

        description = describe_pattern(series.pattern)
        logging.info("%s'%s' -> %s (%s)", "[DRY RUN] " if args.dry_run else "", series.subject, rrule, description)

        if args.dry_run:
            rows.append({"subject": series.subject, "action": "would create", "reason": description, "recurrence_rule": rrule, "clio_entry_id": ""})
            continue

        try:
            created = create_calendar_entry(
                session,
                summary=series.subject,
                start_at=series.start,
                end_at=series.end,
                all_day=series.is_all_day,
                location=series.location,
                description=f'Migrated from Outlook (recurring): "{series.subject}"',
                recurrence_rule=rrule,
            )
            entry_id = created.get("data", {}).get("id", "")
            rows.append({"subject": series.subject, "action": "created", "reason": description, "recurrence_rule": rrule, "clio_entry_id": entry_id})
        except RuntimeError as exc:
            logging.error("Failed to create '%s': %s", series.subject, exc)
            rows.append({"subject": series.subject, "action": "error", "reason": str(exc), "recurrence_rule": rrule, "clio_entry_id": ""})

    output_dir = Path("output")
    output_dir.mkdir(exist_ok=True)
    stem = f"outlook_recurring_availability_{args.from_date}_to_{args.to_date}"
    csv_path = output_dir / f"{stem}{'_dry_run' if args.dry_run else ''}.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["subject", "action", "reason", "recurrence_rule", "clio_entry_id"])
        writer.writeheader()
        writer.writerows(rows)
    logging.info("Wrote %d rows -> %s", len(rows), csv_path)

    from collections import Counter
    counts = Counter(r["action"] for r in rows)
    logging.info("Summary: %s", dict(counts))


if __name__ == "__main__":
    main()
