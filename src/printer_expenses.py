"""
printer_expenses.py — Printer/copier report → Clio ExpenseEntry API calls

Reads:
  data/print_copy_summary_by_account.csv  (Papercut export)
  Clio API — fetches open matters live at run time

Writes:
  output/expenses_YYYY-MM.json    API payloads (always written, even dry-run)
  output/exceptions_YYYY-MM.csv  Unmatched / ambiguous names needing manual resolution
  logs/printer_expenses_YYYYMMDD.log

Usage:
  uv run src/printer_expenses.py --dry-run          # preview, no API calls
  uv run src/printer_expenses.py                    # post to Clio

Options:
  --input PATH    Printer CSV (default: data/print_copy_summary_by_account.csv)
  --dry-run       Build payloads and log them, but do not POST
"""

import argparse
import calendar
import csv
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

from matter_matching import fetch_open_matters, index_by_display_name, normalize_name

load_dotenv()

# Overrides added live from the dashboard (matter-search "Save" button) — a
# data file rather than editing this source file from a web request, same
# pattern as data/bradford_manual_matter_map.csv / legs_manual_matter_map.csv.
# Merged with MANUAL_MATTER_MAP at run time; the code constant wins on
# conflict since it's the deliberately-reviewed one.
PERSISTED_MATTER_MAP_PATH = Path("data") / "printer_manual_matter_map.csv"

# ---------------------------------------------------------------------------
# Configuration — edit these as needed
# ---------------------------------------------------------------------------

PRICE_PER_PAGE = 0.10

# Clio ExpenseCategory id for "Printing/Scanning/Copying" (confirmed live
# 2026-09-09 via GET /expense_categories.json). Without this, Clio labels the
# entry "Reimbursable Expense: <note>" instead of showing it under its real
# category — Ted flagged this after noticing the generic prefix in Clio.
EXPENSE_CATEGORY_ID = 6218073

# Manual overrides: normalized printer name → Clio matter ID (int)
# Add entries here when auto-matching fails or resolves to the wrong matter.
MANUAL_MATTER_MAP: dict[str, int] = {
    # Name mismatches (printer strips apostrophes / uses nicknames / partial names)
    "ONEIL, SUSAN": 1787247618,       # Clio: O'NEIL, SUSAN
    "MCCLUSKEY, MIKE": 1786836843,    # Clio: MCCLUSKEY, MICHAEL
    "VERSTRAETE, PAULA": 1786847043,  # Clio: VERSTRAETE, MARY PAULA
    # Newer matters — printer account name didn't match Clio's display_number format
    "ALMEDA, TERESA": 1787471028,
    "KABBAN, LEYLA": 1789701108,
    # No first name on printer account — Colton, Ann disso matter
    "COLTON": 1787620173,
    # Joint client (mediation) — both clients billed; applied to Jennifer's matter
    "KRIDER, JENNIFER & JOHNATHAN": 1786834383,
    # Missing space vs Clio's display_number
    "ALMAGHAZAJI, RUSUL": 1786820883,  # Clio: AL MAGHAZAJI, RUSUL
    # Bare last name shared by two open matters (DONOVAN, BROOKE / DONOVAN, MEGAN)
    # — confirmed with Ted 2026-08-19 this was Megan's printing
    "DONOVAN": 1786827108,  # Clio: DONOVAN, MEGAN
}


def load_persisted_matter_map(path: Path = PERSISTED_MATTER_MAP_PATH) -> dict[str, int]:
    if not path.exists():
        return {}
    out: dict[str, int] = {}
    with open(path, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            name = (row.get("name") or "").strip().upper()
            matter_id = (row.get("matter_id") or "").strip()
            if name and matter_id.isdigit():
                out[name] = int(matter_id)
    return out


def save_persisted_override(name: str, matter_id: int, note: str = "",
                             path: Path = PERSISTED_MATTER_MAP_PATH) -> None:
    """Appends one override row, writing a header (and BOM, for Excel) only if
    the file doesn't exist yet — utf-8-sig on every open() would otherwise
    write a fresh BOM into the middle of the file on each append."""
    path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not path.exists()
    with open(path, "a", newline="", encoding="utf-8") as f:
        if is_new:
            f.write("﻿")
            csv.writer(f).writerow(["name", "matter_id", "note", "added_at"])
        csv.writer(f).writerow([
            name.strip().upper(), matter_id, note,
            datetime.now().isoformat(timespec="seconds"),
        ])
    logging.info("Persisted override: %s -> matter %s", name.strip().upper(), matter_id)


def effective_manual_matter_map() -> dict[str, int]:
    combined = load_persisted_matter_map()
    combined.update(MANUAL_MATTER_MAP)
    return combined

# ---------------------------------------------------------------------------
# Clio API
# ---------------------------------------------------------------------------

BASE_URL = os.getenv("CLIO_BASE_URL", "https://app.clio.com").rstrip("/")
ACCESS_TOKEN = os.getenv("CLIO_ACCESS_TOKEN", "")
ACTIVITIES_ENDPOINT = f"{BASE_URL}/api/v4/activities.json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def extract_report_date(header_line: str) -> str:
    """
    Parse the Papercut comment line's From/To date range and return the
    ISO-8601 last day of whichever month has the most days inside that
    range — the billing month.

    Not simply the "To date"'s own month: Papercut's export window doesn't
    reliably land on a clean calendar-month boundary. Real example that
    mislabeled a whole month's expenses: 'From date = Aug 2, 2026 ...,
    To date = Sep 1, 2026 ...' — the report was pulled one day into
    September, but 30 of its 31 days are August's. Taking "To date" alone
    would post it as "Sep 2026". Comparing how many days of the range fall
    in each month picks August here, while still correctly picking June for
    a cleanly-aligned range like 'From date = May 31 ..., To date = Jun 30'
    (1 day in May, 30 in June).
    """
    match = re.search(
        r"From date\s*=\s*([A-Za-z]+\s+\d+,\s*\d{4}).*?To date\s*=\s*([A-Za-z]+\s+\d+,\s*\d{4})",
        header_line,
    )
    if match:
        try:
            from_dt = datetime.strptime(match.group(1).strip(), "%b %d, %Y")
            to_dt = datetime.strptime(match.group(2).strip(), "%b %d, %Y")
        except ValueError:
            pass
        else:
            month_end_from = datetime(from_dt.year, from_dt.month,
                                       calendar.monthrange(from_dt.year, from_dt.month)[1])
            days_in_from_month = (min(to_dt, month_end_from) - from_dt).days + 1
            month_start_to = datetime(to_dt.year, to_dt.month, 1)
            days_in_to_month = (to_dt - max(from_dt, month_start_to)).days + 1
            anchor = from_dt if days_in_from_month >= days_in_to_month else to_dt
            last_day = calendar.monthrange(anchor.year, anchor.month)[1]
            return datetime(anchor.year, anchor.month, last_day).strftime("%Y-%m-%d")
    logging.warning("Could not parse report date from header; using today.")
    return datetime.today().strftime("%Y-%m-%d")


def check_report_period(header_line: str) -> tuple[bool, str]:
    """Sanity-checks the header's From/To range against the full prior
    calendar month — this import always represents last month's usage, run
    early the following month, so anything else (a partial pull, the wrong
    month, a stale re-upload of an old file) is worth flagging loudly before
    posting rather than silently billing the wrong period. Separate from
    extract_report_date()'s own tolerant fallback (which still produces a
    best-guess date even from an odd range) — this is purely an FYI check on
    top of that, same relationship as Legs' reconciliation check."""
    today = datetime.today()
    first_of_this_month = datetime(today.year, today.month, 1)
    last_of_prev_month = first_of_this_month - timedelta(days=1)
    first_of_prev_month = datetime(last_of_prev_month.year, last_of_prev_month.month, 1)
    expected_label = first_of_prev_month.strftime("%b %Y")

    match = re.search(
        r"From date\s*=\s*([A-Za-z]+\s+\d+,\s*\d{4}).*?To date\s*=\s*([A-Za-z]+\s+\d+,\s*\d{4})",
        header_line,
    )
    if not match:
        return False, f"Could not read the report period from the file header — expected {expected_label}."

    try:
        from_dt = datetime.strptime(match.group(1).strip(), "%b %d, %Y")
        to_dt = datetime.strptime(match.group(2).strip(), "%b %d, %Y")
    except ValueError:
        return False, f"Could not read the report period from the file header — expected {expected_label}."

    if from_dt != first_of_prev_month or to_dt != last_of_prev_month:
        return False, (
            f"Report period is {from_dt.strftime('%b %d, %Y')} to {to_dt.strftime('%b %d, %Y')} — "
            f"expected the full prior month, {first_of_prev_month.strftime('%b %d')} to "
            f"{last_of_prev_month.strftime('%b %d, %Y')}. Double-check this is the right file "
            f"before posting."
        )
    return True, f"Report period matches the expected prior month ({expected_label})."


def setup_logging(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"printer_expenses_{datetime.today().strftime('%Y%m%d')}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        force=True,  # override any handler Python auto-added before this call
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(open(sys.stdout.fileno(), mode="w", encoding="utf-8", errors="replace", closefd=False)),
        ],
    )
    logging.info("Log file: %s", log_file)


# ---------------------------------------------------------------------------
# Parse printer report
# ---------------------------------------------------------------------------

def parse_printer_report(csv_path: Path) -> tuple[str, bool, str, dict[str, dict]]:
    """
    Returns (report_date_iso, period_ok, period_note, aggregated) where
    aggregated is:
      { normalized_name: { "print": int, "scan": int, "copy": int, "total": int } }
    """
    if not csv_path.exists():
        raise FileNotFoundError(f"Printer CSV not found: {csv_path}")

    report_date = datetime.today().strftime("%Y-%m-%d")
    period_ok, period_note = False, "Could not read the report period from the file header."
    aggregated: dict[str, dict] = {}

    with open(csv_path, encoding="utf-8-sig") as f:
        lines = f.readlines()

    # First two lines are comments; extract date from line 2
    for line in lines[:2]:
        if "To date" in line:
            report_date = extract_report_date(line)
            period_ok, period_note = check_report_period(line)

    # Find the header row — skip comment lines (may be bare or quoted with #)
    data_lines = [l for l in lines if not l.strip().lstrip('"').lstrip("'").startswith("#")]
    reader = csv.DictReader(data_lines)

    for row in reader:
        raw_name = row.get("Shared Account Parent Name", "").strip()
        if not raw_name:
            continue
        name = normalize_name(raw_name)
        job_type = row.get("Job Type", "").strip().upper()
        try:
            pages = int(row.get("Total Printed Pages", 0))
        except ValueError:
            pages = 0

        if name not in aggregated:
            aggregated[name] = {"print": 0, "scan": 0, "copy": 0, "total": 0}
        if job_type == "PRINT":
            aggregated[name]["print"] += pages
        elif job_type == "SCAN":
            aggregated[name]["scan"] += pages
        elif job_type == "COPY":
            aggregated[name]["copy"] += pages
        aggregated[name]["total"] += pages

    logging.info(
        "Parsed %d client entries from %s (report date: %s)",
        len(aggregated), csv_path, report_date,
    )
    if period_ok:
        logging.info("Report period OK: %s", period_note)
    else:
        logging.warning("Report period MISMATCH: %s", period_note)
    return report_date, period_ok, period_note, aggregated


# ---------------------------------------------------------------------------
# Match and build payloads
# ---------------------------------------------------------------------------

def build_note(name: str, data: dict, report_date: str) -> str:
    """Note text alongside the entry's Printing/Scanning/Copying expense
    category (EXPENSE_CATEGORY_ID) — no category-name prefix needed here
    since Clio already shows the category, so this is just the month total
    and its breakdown by job type."""
    month_label = datetime.strptime(report_date, "%Y-%m-%d").strftime("%b %Y")
    parts = []
    if data["print"]:
        parts.append(f"Print: {data['print']}")
    if data["scan"]:
        parts.append(f"Scan: {data['scan']}")
    if data["copy"]:
        parts.append(f"Copy: {data['copy']}")
    breakdown = ", ".join(parts)
    return f"{month_label}: {data['total']} pages ({breakdown})"


def match_and_build(
    aggregated: dict,
    report_date: str,
    matters: dict[str, int | None],
    manual_map: dict[str, int] | None = None,
) -> tuple[list[dict], list[dict]]:
    """
    Returns (payloads, exceptions).
    payloads: list of Clio API request body dicts
    exceptions: list of dicts describing unresolved names
    """
    manual_map = MANUAL_MATTER_MAP if manual_map is None else manual_map
    payloads: list[dict] = []
    exceptions: list[dict] = []

    for name, data in sorted(aggregated.items()):
        total = data["total"]
        if total == 0:
            continue

        # Manual override takes precedence
        if name in manual_map:
            matter_id = manual_map[name]
            logging.info("%-35s  %4d pages  manual override -> matter %s", name, total, matter_id)
        elif " & " in name:
            exceptions.append({
                "name": name,
                "total_pages": total,
                "cost": round(total * PRICE_PER_PAGE, 2),
                "reason": "Joint client — add matter ID to MANUAL_MATTER_MAP",
            })
            logging.warning("%-35s  %4d pages  JOINT CLIENT — skipped", name, total)
            continue
        elif name not in matters:
            exceptions.append({
                "name": name,
                "total_pages": total,
                "cost": round(total * PRICE_PER_PAGE, 2),
                "reason": "No matching open matter found",
            })
            logging.warning("%-35s  %4d pages  NO MATCH", name, total)
            continue
        elif matters[name] is None:
            exceptions.append({
                "name": name,
                "total_pages": total,
                "cost": round(total * PRICE_PER_PAGE, 2),
                "reason": "Multiple open matters — add matter ID to MANUAL_MATTER_MAP",
            })
            logging.warning("%-35s  %4d pages  AMBIGUOUS", name, total)
            continue
        else:
            matter_id = matters[name]
            logging.info("%-35s  %4d pages  -> matter %s", name, total, matter_id)

        payloads.append({
            "data": {
                "type": "ExpenseEntry",
                "date": report_date,
                "matter": {"id": matter_id},
                "expense_category": {"id": EXPENSE_CATEGORY_ID},
                "quantity": total,
                "price": PRICE_PER_PAGE,
                "note": build_note(name, data, report_date),
            }
        })

    return payloads, exceptions


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

POST_DELAY_SECONDS = 1.5  # stay well under Clio's 50 requests/minute limit


def post_expense(session: requests.Session, payload: dict, dry_run: bool) -> bool:
    note = payload["data"]["note"]
    matter_id = payload["data"]["matter"]["id"]
    if dry_run:
        logging.info("DRY-RUN  matter=%s  %s", matter_id, note)
        return True

    for attempt in range(1, 3):  # try twice
        resp = session.post(ACTIVITIES_ENDPOINT, json=payload)
        if resp.status_code in (200, 201):
            activity_id = resp.json().get("data", {}).get("id", "?")
            logging.info("POSTED   matter=%s  activity=%s  %s", matter_id, activity_id, note)
            return True
        elif resp.status_code == 429:
            # Parse "Retry in X seconds" from the response body
            wait = 60  # default fallback
            match = re.search(r"Retry in (\d+) seconds", resp.text)
            if match:
                wait = int(match.group(1)) + 2  # small buffer
            logging.warning("RATE LIMITED  matter=%s  waiting %ds (attempt %d/2)", matter_id, wait, attempt)
            time.sleep(wait)
        else:
            logging.error(
                "FAILED   matter=%s  status=%s  body=%s",
                matter_id, resp.status_code, resp.text[:300],
            )
            return False

    logging.error("FAILED   matter=%s  gave up after 2 attempts", matter_id)
    return False


# ---------------------------------------------------------------------------
# Pipeline (shared by the CLI and the web dashboard)
# ---------------------------------------------------------------------------

@dataclass
class RunResult:
    report_date: str
    period: str
    payloads: list[dict] = field(default_factory=list)
    exceptions: list[dict] = field(default_factory=list)
    period_ok: bool = True
    period_note: str = ""
    total_clients: int = 0
    posted: int = 0
    failed: int = 0
    payloads_path: Path | None = None
    exceptions_path: Path | None = None
    matter_names: dict[int, str] = field(default_factory=dict)  # matter_id -> display name, for UI
    all_matters: list[dict] = field(default_factory=list)  # [{"id":, "name":}], for the dashboard's matter-name search


def run_pipeline(
    input_path: Path,
    dry_run: bool,
    matter_filter: str = "",
    output_dir: Path = Path("output"),
) -> RunResult:
    """
    Parse the printer CSV, match to Clio matters, write payload/exception
    output files, and (unless dry_run) POST expense entries.

    Raises FileNotFoundError / RuntimeError on hard failures instead of
    exiting the process, so it's safe to call from a long-running server.
    """
    if not ACCESS_TOKEN:
        raise RuntimeError("CLIO_ACCESS_TOKEN not set in .env")

    session = requests.Session()
    session.headers.update({
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json",
    })

    report_date, period_ok, period_note, aggregated = parse_printer_report(input_path)

    if matter_filter:
        filter_key = matter_filter.upper()
        aggregated = {k: v for k, v in aggregated.items() if filter_key in k}
        if not aggregated:
            raise RuntimeError(f"--matter filter '{matter_filter}' matched no entries")
        logging.info("--matter filter '%s' matched %d entry/entries", matter_filter, len(aggregated))

    matters_raw = fetch_open_matters(session)
    matters = index_by_display_name(matters_raw)
    payloads, exceptions = match_and_build(aggregated, report_date, matters, effective_manual_matter_map())

    output_dir.mkdir(exist_ok=True)
    period = report_date[:7]  # YYYY-MM
    matter_names = {mid: name for name, mid in matters.items() if mid}
    all_matters = sorted(
        (
            {"id": int(m["id"]), "name": m["display_number"]}
            for m in matters_raw if m.get("display_number")
        ),
        key=lambda m: m["name"],
    )
    result = RunResult(report_date=report_date, period=period, payloads=payloads,
                        exceptions=exceptions, period_ok=period_ok, period_note=period_note,
                        total_clients=len(aggregated),
                        matter_names=matter_names, all_matters=all_matters)

    result.payloads_path = output_dir / f"expenses_{period}.json"
    with open(result.payloads_path, "w", encoding="utf-8") as f:
        json.dump(payloads, f, indent=2)
    logging.info("Wrote %d payloads to %s", len(payloads), result.payloads_path)

    if exceptions:
        result.exceptions_path = output_dir / f"exceptions_{period}.csv"
        with open(result.exceptions_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=["name", "total_pages", "cost", "reason"])
            writer.writeheader()
            writer.writerows(exceptions)
        logging.warning("Wrote %d exceptions to %s — these need manual resolution", len(exceptions), result.exceptions_path)

    logging.info(
        "Summary: %d matched, %d exceptions, %d total clients",
        len(payloads), len(exceptions), len(aggregated),
    )

    if not payloads or dry_run:
        if not payloads:
            logging.info("Nothing to post.")
        else:
            logging.info("--- DRY RUN: no expense entries posted ---")
            for p in payloads:
                d = p["data"]
                logging.info("  matter=%-12s  qty=%-4s  total=$%.2f  %s",
                             d["matter"]["id"], d["quantity"],
                             d["quantity"] * d["price"], d["note"])
        return result

    for payload in payloads:
        if post_expense(session, payload, dry_run=False):
            result.posted += 1
        else:
            result.failed += 1
        time.sleep(POST_DELAY_SECONDS)

    logging.info("Done: %d posted, %d failed", result.posted, result.failed)
    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", default="data/print_copy_summary_by_account.csv", help="Printer CSV path")
    parser.add_argument("--dry-run", action="store_true", help="Fetch matters and build payloads, but do not POST expenses")
    parser.add_argument("--matter", default="", help="Process only entries whose name contains this string (case-insensitive)")
    args = parser.parse_args()

    setup_logging(Path("logs"))

    try:
        result = run_pipeline(Path(args.input), dry_run=args.dry_run, matter_filter=args.matter)
    except (FileNotFoundError, RuntimeError) as e:
        logging.error(str(e))
        sys.exit(1)

    if result.failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
