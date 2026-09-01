"""
papercut_balance_migration.py — ONE-TIME migration helper for switching
PaperCut's Shared Accounts over to the Clio-driven sync
(generate_papercut_accounts.py), without losing currently-accumulated
account balances (this month's not-yet-billed printer charges).

Why this exists: PaperCut's Shared Account Sync (Text file source) matches
existing accounts by NAME. This firm's current accounts were created by
hand over time and don't all match Clio's own "LAST, FIRST" display_number
exactly (the same divergence printer_expenses.py's MANUAL_MATTER_MAP already
works around in the other direction) — a name that doesn't match exactly
would sync as a brand-new, separate account rather than updating the
existing one, stranding its balance under the old name. Separately,
PaperCut's own documentation is genuinely ambiguous/contradictory (checked
2026-08-19 against two different manual mirrors) about whether a blank
Balance field on a MATCHED existing account resets it to 0 or leaves it
alone — rather than gamble on which is correct, this script always writes
the account's real current balance explicitly.

Reads:
  Clio API — open matters, live (same as generate_papercut_accounts.py)
  <existing account list CSV> — PaperCut's own "Shared account list" export
    (columns: Shared Account Parent Name, ..., Code, Balance, ...) — e.g.
    data/shared_account_list.csv, copied from the real PaperCut export

Writes:
  output/papercut_accounts_migration.tsv — same shape as
    generate_papercut_accounts.py's normal output, EXCEPT Credit Balance is
    explicitly populated with the matched existing account's real current
    balance (blank only for genuinely new accounts with no existing match)
  output/papercut_balance_mismatches.csv — existing accounts that could NOT
    be matched to any current Clio open matter, sorted by |balance|
    descending — these need manual review BEFORE the live sync: either the
    matter's Clio name needs fixing, a rename in PaperCut, or the account is
    legitimately stale and safe to ignore

This is a one-time, hand-reviewed migration step, not part of the regular
ongoing sync — nothing here writes to Clio or PaperCut. Review both output
files, resolve every real mismatch, and only then upload
papercut_accounts_migration.tsv AS the Shared Account Sync source
(instead of the regular generate_papercut_accounts.py output) for the one
run that performs the switch-over. Every future run can go back to the
plain generate_papercut_accounts.py output, since by then account names
will already be aligned.

Usage:
  uv run src/papercut_balance_migration.py --existing data/shared_account_list.csv
"""

import argparse
import csv
import difflib
import logging
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(ENV_PATH)

from generate_papercut_accounts import ACCESS_TOKEN, TSV_COLUMNS, build_rows  # noqa: E402
from matter_matching import fetch_open_matters, index_by_last_name_all, normalize_name  # noqa: E402

# Below this difflib similarity ratio, no last-resort suggestion is offered.
# Set high (not bradford_invoice.py's 0.6) — real-data check found 0.6 on
# whole "LAST, FIRST" strings mostly matches on a shared common first name
# (JAMES, MICHAEL, JESSICA, ...) across two totally unrelated clients, not
# genuine similarity. The primary matching path below is last-name-index
# based instead (same approach court_calendar/matcher.py and Legs Expenses
# already use for bare-last-name identifiers), which is precise enough that
# this difflib fallback is only reached, and only needed, for genuine
# leftover spelling drift on a last name itself.
SUGGESTION_CUTOFF = 0.8


def setup_logging(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "papercut_balance_migration.log"
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


def load_existing_balances(path: Path) -> dict[str, tuple[str, float]]:
    """normalized name -> (raw name as it appears in PaperCut today, balance).
    PaperCut's export uses "LAST,FIRST" with no space after the comma —
    normalize_name() already re-inserts it, same as every other name-matching
    path in this codebase."""
    if not path.exists():
        raise FileNotFoundError(f"Existing account export not found: {path}")

    balances: dict[str, tuple[str, float]] = {}
    with open(path, encoding="utf-8-sig") as f:
        lines = f.readlines()
    # First line is a "# Shared account list" comment, same convention as
    # printer_expenses.py's Papercut usage export.
    data_lines = [l for l in lines if not l.lstrip().startswith("#")]
    reader = csv.DictReader(data_lines)
    for row in reader:
        raw_name = (row.get("Shared Account Parent Name") or "").strip()
        if not raw_name:
            continue
        try:
            balance = float((row.get("Balance") or "0").strip() or "0")
        except ValueError:
            balance = 0.0
        balances[normalize_name(raw_name)] = (raw_name, balance)

    logging.info("Loaded %d existing PaperCut shared accounts from %s", len(balances), path)
    return balances


def build_migration_rows(
    new_rows: list[dict],
    existing: dict[str, tuple[str, float]],
) -> tuple[list[dict], set[str]]:
    """Returns (migration_rows, matched_keys). matched_keys lets the caller
    figure out which existing accounts were NOT matched (the mismatch report)."""
    migration_rows: list[dict] = []
    matched_keys: set[str] = set()

    for row in new_rows:
        key = normalize_name(row["Parent Account Name"])
        row = dict(row)  # don't mutate the caller's row
        if key in existing:
            _, balance = existing[key]
            row["Credit Balance"] = f"{balance:.2f}"
            matched_keys.add(key)
        migration_rows.append(row)

    return migration_rows, matched_keys


def _last_name_candidate(raw_name: str) -> str:
    """PaperCut account names are "LAST" or "LAST,FIRST" — the same convention
    Bradford invoices and court-calendar text use, so this is the same
    "text before the first comma" extraction matter_matching.py's own
    index_by_last_name_all() expects as a lookup key."""
    return normalize_name(raw_name).split(",")[0].strip()


def build_mismatch_report(
    existing: dict[str, tuple[str, float]],
    matched_keys: set[str],
    open_last_names: dict[str, list[int]],
    all_last_names: dict[str, list[int]],
    id_to_display: dict[int, str],
) -> list[dict]:
    """Classifies every unmatched existing account so a human can tell "just a
    closed matter, ignore" apart from "genuine naming drift on a still-open
    matter, fix this" without checking each of potentially hundreds by hand.

    Primary signal is last-name-index matching (matter_matching.py's own
    approach for bare-last-name identifiers, e.g. a printer/invoice account
    with no first name at all) rather than whole-string difflib similarity —
    a real-data check found whole-string difflib mostly false-positives on a
    shared common first name (two different "JAMES" or "MICHAEL" clients),
    not genuine name similarity. difflib is kept only as a last-resort, high-
    cutoff hint when no last-name match exists at all."""
    open_last_name_keys = list(open_last_names.keys())
    mismatches = []

    for key, (raw_name, balance) in existing.items():
        if key in matched_keys:
            continue

        last = _last_name_candidate(raw_name)
        open_ids = open_last_names.get(last, [])
        all_ids = all_last_names.get(last, [])

        if len(open_ids) == 1:
            status = "Likely match by last name on a still-OPEN matter — REVIEW"
            suggestion = id_to_display.get(open_ids[0], "")
        elif len(open_ids) > 1:
            status = f"Ambiguous — {len(open_ids)} open matters share this last name — REVIEW"
            suggestion = "; ".join(id_to_display.get(i, str(i)) for i in open_ids)
        elif all_ids:
            status = "Matches a closed/non-open Clio matter by last name — expected, no action needed"
            suggestion = "; ".join(id_to_display.get(i, str(i)) for i in all_ids)
        else:
            candidates = difflib.get_close_matches(last, open_last_name_keys, n=1, cutoff=SUGGESTION_CUTOFF)
            if candidates:
                status = "Weak spelling-similarity match on a still-OPEN matter — REVIEW CAREFULLY"
                matched_ids = open_last_names.get(candidates[0], [])
                suggestion = "; ".join(id_to_display.get(i, str(i)) for i in matched_ids)
            else:
                status = "No matching Clio matter under any status — REVIEW"
                suggestion = ""

        mismatches.append({
            "PaperCut Account Name": raw_name,
            "Balance": f"{balance:.2f}",
            "Status": status,
            "Suggested Clio Match": suggestion,
        })

    mismatches.sort(key=lambda r: abs(float(r["Balance"])), reverse=True)
    return mismatches


def write_tsv(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=TSV_COLUMNS, delimiter="\t")
        for row in rows:
            writer.writerow(row)


def write_mismatch_csv(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=["PaperCut Account Name", "Balance", "Status", "Suggested Clio Match"])
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--existing", default="data/shared_account_list.csv",
                         help="PaperCut's current Shared account list export (CSV)")
    parser.add_argument("--output-dir", default="output")
    args = parser.parse_args()

    setup_logging(Path("logs"))

    if not ACCESS_TOKEN:
        logging.error("CLIO_ACCESS_TOKEN not set in .env")
        sys.exit(1)

    existing_path = Path(args.existing)
    try:
        existing = load_existing_balances(existing_path)
    except FileNotFoundError as e:
        logging.error(str(e))
        sys.exit(1)

    session = requests.Session()
    session.headers.update({
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json",
    })
    matters = fetch_open_matters(session)
    new_rows = build_rows(matters)

    # Every matter regardless of status, to tell "this account's matter just
    # closed" (expected, no action) apart from "this account's matter is
    # still open and the name just doesn't line up" (a real problem).
    all_matters = fetch_open_matters(session, status="open,pending,closed")
    open_last_names = index_by_last_name_all(matters)
    all_last_names = index_by_last_name_all(all_matters)
    id_to_display = {int(m["id"]): (m.get("display_number") or "").strip() for m in all_matters}

    migration_rows, matched_keys = build_migration_rows(new_rows, existing)
    mismatches = build_mismatch_report(existing, matched_keys, open_last_names, all_last_names, id_to_display)

    output_dir = Path(args.output_dir)
    migration_path = output_dir / "papercut_accounts_migration.tsv"
    mismatch_path = output_dir / "papercut_balance_mismatches.csv"
    write_tsv(migration_rows, migration_path)
    write_mismatch_csv(mismatches, mismatch_path)

    preserved = sum(1 for r in migration_rows if r["Credit Balance"])
    nonzero_mismatches = [r for r in mismatches if abs(float(r["Balance"])) > 0.001]

    logging.info(
        "Summary: %d Clio accounts total, %d matched to an existing balance and preserved, "
        "%d existing accounts unmatched (%d with a nonzero balance — REVIEW THESE)",
        len(migration_rows), preserved, len(mismatches), len(nonzero_mismatches),
    )
    logging.info("Migration TSV: %s", migration_path)
    logging.info("Mismatch report: %s", mismatch_path)
    if nonzero_mismatches:
        logging.warning(
            "%d existing accounts with real balances have NO matching Clio matter name — "
            "resolve these in %s before running the live sync, or their balance is stranded "
            "under the old account name.", len(nonzero_mismatches), mismatch_path,
        )


if __name__ == "__main__":
    main()
