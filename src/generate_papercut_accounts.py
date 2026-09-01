"""
generate_papercut_accounts.py — Clio open matters -> PaperCut Shared Account
Sync TSV.

Reads:
  Clio API — fetches open matters live at run time (matter_matching.fetch_open_matters)

Writes:
  <PAPERCUT_ACCOUNTS_PATH, default output/papercut_accounts.tsv>
    Tab-delimited, no header row, PaperCut's own "Text file source" batch
    import format. Overwritten in place every run (NOT date-stamped like
    this project's other outputs) — PaperCut's Shared Account Sync feature
    is configured to read one fixed path on a schedule (hourly/nightly), so
    the file at that path always needs to reflect the latest state, not a
    dated snapshot staff pick by hand.
  logs/generate_papercut_accounts_YYYYMMDD.log
  data/clio_dashboard.db (papercut_sync_runs) — one row per run, for the
    dashboard's status view

Never writes to Clio. Never talks to PaperCut directly (no PaperCut API is
used here) — PaperCut's own Shared Account Sync feature reads the file this
script produces on its own schedule; see CLAUDE.md's "PaperCut Shared
Account Sync" section for the sync-side setup (Accounts > Shared Account
Sync > Text file source, pointed at PAPERCUT_ACCOUNTS_PATH).

Column D (Account PIN/Code) is the Clio matter's own numeric `id` — not
`custom_number` (Clio's user-defined "Matter Unique ID" field, sometimes
called MUID elsewhere in this codebase — see matter_matching.py). `id` was
chosen because it's guaranteed present and unique on every matter (custom_
number is optional and blank on some), and it's already the exact value
this codebase's other scripts pass as `matter.id` in Clio API payloads
(see printer_expenses.py's ExpenseEntry POST) — so a future switch of
printer_expenses.py to PIN/Code-based matching (the whole point of this
integration, see CLAUDE.md) can use the PIN straight through with no extra
Clio lookup to translate it back to a matter id.

Usage:
  uv run src/generate_papercut_accounts.py
"""

import csv
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(ENV_PATH)

from matter_matching import fetch_open_matters  # noqa: E402

BASE_URL = os.getenv("CLIO_BASE_URL", "https://app.clio.com").rstrip("/")
ACCESS_TOKEN = os.getenv("CLIO_ACCESS_TOKEN", "")

# Where PaperCut's Shared Account Sync (Text file source) reads from. Defaults
# to a plain repo-local file until a real mapped-drive/UNC path reachable from
# the PaperCut server is configured — see CLAUDE.md.
ACCOUNTS_PATH = Path(os.getenv("PAPERCUT_ACCOUNTS_PATH", "output/papercut_accounts.tsv"))

# Groups column (who can print to each client's shared account) — [All Users]
# per Ted, 2026-08-19: matter access already works this way day to day, no
# need to restrict shared-account printing to a narrower staff group.
PAPERCUT_GROUPS = "[All Users]"
INVOICE_OPTION = "ALWAYS_INVOICE"

# PaperCut's own documented column order for a Shared Account batch import —
# tab-delimited, no header row (see CLAUDE.md's "PaperCut Shared Account Sync").
TSV_COLUMNS = [
    "Parent Account Name", "Sub-account Name", "Enabled", "Account PIN/Code",
    "Credit Balance", "Restricted Status", "Users", "Groups",
    "Invoice Option", "Comment Option", "Notes",
]

# Own schema fragment (see web/db.py's _apply_fragment) rather than added to
# its monolithic CORE_SCHEMA — one row per generation run, for the dashboard's
# status view. This never writes to Clio, so unlike trust_requests there's no
# lifecycle to track, just a history of what was last generated.
SCHEMA = """
CREATE TABLE IF NOT EXISTS papercut_sync_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at TEXT DEFAULT CURRENT_TIMESTAMP,
    matter_count INTEGER NOT NULL,
    tsv_path TEXT NOT NULL
);
"""

SCHEMA_COLUMNS = []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def setup_logging(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"generate_papercut_accounts_{datetime.today().strftime('%Y%m%d')}.log"
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


def build_rows(matters: list[dict]) -> list[dict]:
    """One row per open matter. Only open matters are included (same scope
    convention as every other subproject) — every row is therefore Enabled=Y;
    there's no need for a per-row disabled state here."""
    rows: list[dict] = []
    for m in matters:
        display = (m.get("display_number") or "").strip()
        matter_id = m.get("id")
        if not display or not matter_id:
            continue
        rows.append({
            "Parent Account Name": display,
            "Sub-account Name": "",
            "Enabled": "Y",
            "Account PIN/Code": str(matter_id),
            "Credit Balance": "",
            "Restricted Status": "",
            "Users": "",
            "Groups": PAPERCUT_GROUPS,
            "Invoice Option": INVOICE_OPTION,
            "Comment Option": "",
            "Notes": "",
        })
    rows.sort(key=lambda r: r["Parent Account Name"])
    return rows


def write_accounts_tsv(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=TSV_COLUMNS, delimiter="\t")
        for row in rows:
            writer.writerow(row)


# ---------------------------------------------------------------------------
# Pipeline (shared by the CLI and the web dashboard)
# ---------------------------------------------------------------------------

@dataclass
class RunResult:
    matter_count: int
    tsv_path: Path
    run_at: str = ""


def run_pipeline(output_path: Path = ACCOUNTS_PATH) -> RunResult:
    setup_logging(Path("logs"))

    if not ACCESS_TOKEN:
        raise RuntimeError("CLIO_ACCESS_TOKEN not set in .env")

    session = requests.Session()
    session.headers.update({
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json",
    })

    matters = fetch_open_matters(session)
    rows = build_rows(matters)
    write_accounts_tsv(rows, output_path)
    logging.info("Wrote %d account rows to %s", len(rows), output_path)

    from web.db import get_connection  # local import: avoids web deps for CLI-only runs
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO papercut_sync_runs (matter_count, tsv_path) VALUES (?, ?)",
            (len(rows), str(output_path)),
        )
        conn.commit()
        run_at = conn.execute(
            "SELECT run_at FROM papercut_sync_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()["run_at"]
    finally:
        conn.close()

    logging.info("Summary: %d open matters -> %d account rows", len(matters), len(rows))
    return RunResult(matter_count=len(rows), tsv_path=output_path, run_at=run_at)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    try:
        run_pipeline()
    except RuntimeError as e:
        logging.error(str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
