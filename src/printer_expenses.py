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
import csv
import json
import logging
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration — edit these as needed
# ---------------------------------------------------------------------------

PRICE_PER_PAGE = 0.10

# Manual overrides: normalized printer name → Clio matter ID (int)
# Add entries here when auto-matching fails or resolves to the wrong matter.
MANUAL_MATTER_MAP: dict[str, int] = {
    # Name mismatches (printer strips apostrophes / uses nicknames / partial names)
    "ONEIL, SUSAN": 1787247618,       # Clio: O'NEIL, SUSAN
    "MCCLUSKEY, MIKE": 1786836843,    # Clio: MCCLUSKEY, MICHAEL
    "VERSTRAETE, PAULA": 1786847043,  # Clio: VERSTRAETE, MARY PAULA
    # Newer matters not yet in clio-matters.csv export
    "ALMEDA, TERESA": 1787471028,
    "KABBAN, LEYLA": 1789701108,
    # No first name on printer account — Colton, Ann disso matter
    "COLTON": 1787620173,
    # Joint client (mediation) — both clients billed; applied to Jennifer's matter
    "KRIDER, JENNIFER & JOHNATHAN": 1786834383,
}

# ---------------------------------------------------------------------------
# Clio API
# ---------------------------------------------------------------------------

BASE_URL = os.getenv("CLIO_BASE_URL", "https://app.clio.com").rstrip("/")
ACCESS_TOKEN = os.getenv("CLIO_ACCESS_TOKEN", "")
ACTIVITIES_ENDPOINT = f"{BASE_URL}/api/v4/activities.json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def normalize_name(raw: str) -> str:
    """Strip extra quotes/whitespace, normalize comma spacing, uppercase."""
    s = raw.strip().strip('"').strip("'").strip()
    s = re.sub(r",\s*", ", ", s)
    return s.upper()


def extract_report_date(header_line: str) -> str:
    """
    Parse the Papercut comment line to extract the report end date as ISO-8601.
    Expected format: '# From date = May 31, 2026 ..., To date = Jun 30, 2026 ...'
    Returns e.g. '2026-06-30'. Falls back to today if not parseable.
    """
    match = re.search(r"To date\s*=\s*([A-Za-z]+\s+\d+,\s*\d{4})", header_line)
    if match:
        try:
            dt = datetime.strptime(match.group(1).strip(), "%b %d, %Y")
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            pass
    logging.warning("Could not parse report date from header; using today.")
    return datetime.today().strftime("%Y-%m-%d")


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
# Fetch Clio matters via API
# ---------------------------------------------------------------------------

MATTERS_ENDPOINT = f"{BASE_URL}/api/v4/matters.json"
MATTERS_FIELDS = "id,display_number,custom_number,status"
MATTERS_PAGE_SIZE = 200


def fetch_matters(session: requests.Session) -> dict[str, int | None]:
    """
    Fetch all open matters from Clio API and build a mapping:
      normalized display_number → matter ID

    If multiple open matters share the same display_number:
      - Prefer the one with a custom_number (MUID).
      - If still ambiguous, store None to flag for manual resolution.
    """
    raw_index: dict[str, list[tuple[int, bool]]] = {}
    page_token: str | None = None
    page = 1

    while True:
        params: dict = {
            "status": "open",
            "fields": MATTERS_FIELDS,
            "limit": MATTERS_PAGE_SIZE,
        }
        if page_token:
            params["page_token"] = page_token

        resp = session.get(MATTERS_ENDPOINT, params=params)
        if resp.status_code != 200:
            logging.error("Failed to fetch matters (page %d): %s %s", page, resp.status_code, resp.text[:200])
            sys.exit(1)

        body = resp.json()
        records = body.get("data", [])
        for m in records:
            display = (m.get("display_number") or "").strip()
            if not display:
                continue
            matter_id = int(m["id"])
            has_ref = bool((m.get("custom_number") or "").strip())
            key = normalize_name(display)
            raw_index.setdefault(key, []).append((matter_id, has_ref))

        paging = body.get("meta", {}).get("paging", {})
        page_token = paging.get("next")
        logging.info("Fetched matters page %d — %d records", page, len(records))
        page += 1
        if not page_token:
            break

    result: dict[str, int | None] = {}
    for name, entries in raw_index.items():
        if len(entries) == 1:
            result[name] = entries[0][0]
        else:
            with_ref = [mid for mid, has_ref in entries if has_ref]
            if len(with_ref) == 1:
                result[name] = with_ref[0]
            else:
                result[name] = None  # ambiguous — needs manual override

    logging.info("Loaded %d distinct open matter names from Clio API", len(result))
    return result


# ---------------------------------------------------------------------------
# Parse printer report
# ---------------------------------------------------------------------------

def parse_printer_report(csv_path: Path) -> tuple[str, dict[str, dict]]:
    """
    Returns (report_date_iso, aggregated) where aggregated is:
      { normalized_name: { "print": int, "scan": int, "copy": int, "total": int } }
    """
    if not csv_path.exists():
        logging.error("Printer CSV not found: %s", csv_path)
        sys.exit(1)

    report_date = datetime.today().strftime("%Y-%m-%d")
    aggregated: dict[str, dict] = {}

    with open(csv_path, encoding="utf-8-sig") as f:
        lines = f.readlines()

    # First two lines are comments; extract date from line 2
    for line in lines[:2]:
        if "To date" in line:
            report_date = extract_report_date(line)

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
    return report_date, aggregated


# ---------------------------------------------------------------------------
# Match and build payloads
# ---------------------------------------------------------------------------

def build_note(name: str, data: dict, report_date: str) -> str:
    month_label = datetime.strptime(report_date, "%Y-%m-%d").strftime("%b %Y")
    parts = []
    if data["print"]:
        parts.append(f"Print: {data['print']}")
    if data["scan"]:
        parts.append(f"Scan: {data['scan']}")
    if data["copy"]:
        parts.append(f"Copy: {data['copy']}")
    breakdown = ", ".join(parts)
    return f"Copies/Printing — {month_label}: {data['total']} pages ({breakdown})"


def match_and_build(
    aggregated: dict,
    report_date: str,
    matters: dict[str, int | None],
) -> tuple[list[dict], list[dict]]:
    """
    Returns (payloads, exceptions).
    payloads: list of Clio API request body dicts
    exceptions: list of dicts describing unresolved names
    """
    payloads: list[dict] = []
    exceptions: list[dict] = []

    for name, data in sorted(aggregated.items()):
        total = data["total"]
        if total == 0:
            continue

        # Manual override takes precedence
        if name in MANUAL_MATTER_MAP:
            matter_id = MANUAL_MATTER_MAP[name]
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
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", default="data/print_copy_summary_by_account.csv", help="Printer CSV path")
    parser.add_argument("--dry-run", action="store_true", help="Fetch matters and build payloads, but do not POST expenses")
    parser.add_argument("--matter", default="", help="Process only entries whose name contains this string (case-insensitive)")
    args = parser.parse_args()

    setup_logging(Path("logs"))

    if not ACCESS_TOKEN:
        logging.error("CLIO_ACCESS_TOKEN not set in .env")
        sys.exit(1)

    session = requests.Session()
    session.headers.update({
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json",
    })

    printer_csv = Path(args.input)
    report_date, aggregated = parse_printer_report(printer_csv)

    if args.matter:
        filter_key = args.matter.upper()
        aggregated = {k: v for k, v in aggregated.items() if filter_key in k}
        if not aggregated:
            logging.error("--matter filter '%s' matched no entries", args.matter)
            sys.exit(1)
        logging.info("--matter filter '%s' matched %d entry/entries", args.matter, len(aggregated))

    matters = fetch_matters(session)
    payloads, exceptions = match_and_build(aggregated, report_date, matters)

    output_dir = Path("output")
    output_dir.mkdir(exist_ok=True)
    period = report_date[:7]  # YYYY-MM

    payloads_path = output_dir / f"expenses_{period}.json"
    with open(payloads_path, "w", encoding="utf-8") as f:
        json.dump(payloads, f, indent=2)
    logging.info("Wrote %d payloads to %s", len(payloads), payloads_path)

    if exceptions:
        exc_path = output_dir / f"exceptions_{period}.csv"
        with open(exc_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["name", "total_pages", "cost", "reason"])
            writer.writeheader()
            writer.writerows(exceptions)
        logging.warning("Wrote %d exceptions to %s — these need manual resolution", len(exceptions), exc_path)

    logging.info(
        "Summary: %d matched, %d exceptions, %d total clients",
        len(payloads), len(exceptions), len(aggregated),
    )

    if not payloads:
        logging.info("Nothing to post.")
        return

    if args.dry_run:
        logging.info("--- DRY RUN: no expense entries posted ---")
        for p in payloads:
            d = p["data"]
            logging.info("  matter=%-12s  qty=%-4s  total=$%.2f  %s",
                         d["matter"]["id"], d["quantity"],
                         d["quantity"] * d["price"], d["note"])
        return

    ok = err = 0
    for payload in payloads:
        if post_expense(session, payload, dry_run=False):
            ok += 1
        else:
            err += 1
        time.sleep(POST_DELAY_SECONDS)

    logging.info("Done: %d posted, %d failed", ok, err)
    if err:
        sys.exit(1)


if __name__ == "__main__":
    main()
