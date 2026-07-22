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
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

from court_calendar.clio_calendar import CalendarEntry, fetch_calendar_entries
from court_calendar.store import get_purpose_mappings
from matter_matching import fetch_open_matters, index_by_last_name, index_by_last_name_all
from outlook_calendar.call_overrides import load_overrides
from outlook_calendar.calls_report import CALLS_CSV_FIELDNAMES, build_calls_report, needs_manual_resolution
from outlook_calendar.csv_export import CSV_FIELDNAMES, build_export
from outlook_calendar.event_parser import MatchedEvent, looks_client_relevant, match_event
from outlook_calendar.graph_client import fetch_calendar_events, fetch_recurring_series
from outlook_calendar.interactive_resolve import event_key, resolve_calls_interactively
from outlook_calendar.relationships import (
    OcOpContact,
    build_oc_op_candidates,
    fetch_oc_op_contacts,
    match_exception_to_oc_op,
)

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


@dataclass
class MigrationRun:
    """Everything downstream consumers (main()'s CSV export, and
    outlook_exceptions_availability.py's bulk personal-calendar import) need
    from a single fetch+match pass — kept as one function so both scripts
    are guaranteed to agree on what counts as "matched" vs "exception"
    rather than maintaining two copies of this pipeline that could drift."""
    session: requests.Session
    matched: list[MatchedEvent]
    existing_entries: list[CalendarEntry]
    calls: list[MatchedEvent]
    resolved_keys: set
    skipped_keys: set
    oc_op_contacts: list[OcOpContact]
    total_events: int


def gather_matched_events(from_date: str, to_date: str, matter_filter: str = "") -> MigrationRun:
    logging.info("Fetching Outlook events for %s to %s ...", from_date, to_date)
    all_events = fetch_calendar_events(from_date, to_date)
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

    # Personal recurring series (dog pickup, workouts, birthdays, etc.) are
    # imported once each as true recurring entries by
    # outlook_recurring_availability.py instead — excluded here so their
    # individually-expanded occurrences (up to 100+ for a daily/weekly
    # series) don't flood the exceptions file. A recurring series is only
    # excluded here if it's NOT client-relevant (mirrors that script's own
    # scope exactly, via the same looks_client_relevant() check) and isn't
    # already being dropped by the category filter above.
    logging.info("Fetching recurring series from Outlook (for personal-series exclusion) ...")
    recurring_series = fetch_recurring_series(from_date, to_date)
    personal_recurring_ids = {
        s.series_master_id for s in recurring_series
        if not any(cat in s.categories for cat in SKIP_CATEGORIES)
        and not looks_client_relevant(s.subject)
    }
    before = len(outlook_events)
    outlook_events = [e for e in outlook_events if e.series_master_id not in personal_recurring_ids]
    logging.info(
        "Excluded %d occurrences from %d personal recurring series (handled via outlook_recurring_availability.py instead)",
        before - len(outlook_events), len(personal_recurring_ids),
    )

    session = _clio_session()

    logging.info("Fetching open Clio matters ...")
    matters = fetch_open_matters(session)
    matters_index = index_by_last_name(matters)
    display_by_id = {int(m["id"]): m.get("display_number", "") for m in matters if m.get("display_number")}
    shared_last_names = {name for name, ids in index_by_last_name_all(matters).items() if len(ids) > 1}
    logging.info("Indexed %d open matters", len(display_by_id))

    logging.info("Fetching existing Clio calendar entries (duplicate check) ...")
    existing_entries = fetch_calendar_entries(from_date, to_date, session)
    logging.info("Found %d existing Clio calendar entries in range", len(existing_entries))

    purpose_mappings = get_purpose_mappings()

    learned_matched, learned_skipped = load_overrides()
    logging.info(
        "Loaded %d learned call match(es) and %d learned skip(s) from %s",
        len(learned_matched), len(learned_skipped), "data/outlook_call_overrides.csv",
    )
    combined_manual_map = {**learned_matched, **MANUAL_MATTER_MAP}  # hardcoded entries win on conflict

    matched = []
    for event in outlook_events:
        if event.start is None:
            continue
        m = match_event(event, matters_index, display_by_id, purpose_mappings, shared_last_names)

        if m.matter_id is None:
            last_name = (m.party or "").upper()
            if last_name in combined_manual_map:
                m.matter_id = combined_manual_map[last_name]
                m.matter_display_number = display_by_id.get(m.matter_id, last_name)
                m.shared_last_name = m.matter_display_number.split(",")[0].strip().upper() in shared_last_names
                m.reason = ""

        matched.append(m)

    if matter_filter:
        f = matter_filter.upper()
        matched = [m for m in matched if m.party and f in m.party]
        logging.info("--matter '%s' matched %d events", matter_filter, len(matched))
        if not matched:
            logging.error("No events matched --matter '%s'", matter_filter)
            sys.exit(1)

    logging.info("Fetching Opposing Counsel / Opposing Party contacts (relationships) ...")
    oc_op_contacts = fetch_oc_op_contacts(session)

    calls = [m for m in matched if needs_manual_resolution(m)]

    # matter_id is already set for calls whose party matched combined_manual_map
    # above (including learned_matched) — only genuinely new cases reach the
    # interactive prompt. Previously-skipped parties are excluded too, and
    # pre-recorded here so the calls report still shows them as skipped rather
    # than blank/unresolved.
    still_unresolved = []
    pre_skipped_keys: set = set()
    for m in calls:
        if m.matter_id is not None:
            continue
        if (m.party or "").upper() in learned_skipped:
            pre_skipped_keys.add(event_key(m))
            continue
        if match_exception_to_oc_op({"subject": m.outlook_event.subject, "party": m.party or ""}, oc_op_contacts):
            continue
        still_unresolved.append(m)

    # Group by party so someone who called twice in this range (e.g. two TCONs
    # from the same client) is only asked about once — one representative event
    # per party goes through the prompt, and the decision is applied to every
    # event sharing that party.
    groups: dict[str, list] = {}
    for m in still_unresolved:
        groups.setdefault((m.party or "").upper(), []).append(m)
    representatives = [members[0] for members in groups.values()]

    resolved_reps, skipped_reps = resolve_calls_interactively(representatives, matters, display_by_id, shared_last_names)

    # resolve_calls_interactively() already persisted each real decision to
    # data/outlook_call_overrides.csv the instant it was made (so Ctrl+C/EOF
    # partway through only loses whatever wasn't answered yet) — this loop
    # just propagates an already-made decision to sibling events sharing the
    # same party (duplicates within this run only; no further persistence).
    resolved_keys: set = set()
    skipped_keys: set = set()
    for party, members in groups.items():
        rep = members[0]
        rep_key = event_key(rep)
        if rep_key in resolved_reps:
            for m in members:
                if m is not rep:
                    m.matter_id = rep.matter_id
                    m.matter_display_number = rep.matter_display_number
                    m.ambiguous = False
                    m.reason = ""
                    m.shared_last_name = rep.shared_last_name
                resolved_keys.add(event_key(m))
        elif rep_key in skipped_reps:
            for m in members:
                skipped_keys.add(event_key(m))

    skipped_keys |= pre_skipped_keys

    return MigrationRun(
        session=session, matched=matched, existing_entries=existing_entries,
        calls=calls, resolved_keys=resolved_keys, skipped_keys=skipped_keys,
        oc_op_contacts=oc_op_contacts, total_events=len(outlook_events),
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from-date", required=True, help="Start of range, YYYY-MM-DD")
    ap.add_argument("--to-date", required=True, help="End of range, YYYY-MM-DD")
    ap.add_argument("--matter", default="", help="Process only events whose matched client name contains this string")
    args = ap.parse_args()

    setup_logging(Path("logs"))

    run = gather_matched_events(args.from_date, args.to_date, args.matter)
    result = build_export(run.matched, run.existing_entries)

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

        oc_op_rows = build_oc_op_candidates(result.exceptions, run.oc_op_contacts)
        if oc_op_rows:
            oc_op_path = output_dir / f"{stem}_oc_op_candidates.csv"
            with open(oc_op_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=["subject", "date", "role", "contact", "matter", "matter_status", "note"])
                writer.writeheader()
                writer.writerows(oc_op_rows)
            logging.info(
                "Wrote %d exceptions that are likely OC/OP calls, not missing clients -> %s",
                len(oc_op_rows), oc_op_path,
            )

    if run.calls:
        calls_rows = build_calls_report(run.calls, run.oc_op_contacts, run.resolved_keys, run.skipped_keys)
        calls_path = output_dir / f"{stem}_calls.csv"
        with open(calls_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=CALLS_CSV_FIELDNAMES)
            writer.writeheader()
            writer.writerows(calls_rows)
        logging.info("Wrote %d calls/deadline reminders (TCON/OCON/DUE) -> %s", len(calls_rows), calls_path)

    if result.already_in_clio:
        skip_path = output_dir / f"{stem}_already_in_clio.csv"
        with open(skip_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["subject", "date", "matter"])
            writer.writeheader()
            writer.writerows(result.already_in_clio)
        logging.info("Wrote %d already-in-Clio skips -> %s", len(result.already_in_clio), skip_path)

    logging.info(
        "Summary: %d Outlook events, %d rows for import, %d exceptions, %d already in Clio, "
        "%d calls total (%d resolved manually, %d skipped manually)",
        run.total_events, len(result.rows), len(result.exceptions), len(result.already_in_clio),
        len(run.calls), len(run.resolved_keys), len(run.skipped_keys),
    )


if __name__ == "__main__":
    main()
