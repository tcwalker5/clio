"""
db.py — SQLite connection and schema for the Clio dashboard.

Single local file, no external service. Fine for LAN-scale, low-concurrency
use by a handful of staff (see CLAUDE.md philosophy: no external dependencies).
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "clio_dashboard.db"

# Seed data ported from calendar-check's database/schema.sql purpose_mappings table.
# (raw_pattern, canonical_code, description)
DEFAULT_PURPOSE_MAPPINGS: list[tuple[str, str, str]] = [
    ("FAMILY RESOLUTI", "FRC", "Family Resolution Conference"),
    ("FAMILY RESOLUTION", "FRC", "Family Resolution Conference"),
    ("FAMILY RESOLUTION CONFERENCE", "FRC", "Family Resolution Conference"),
    ("REQUEST FOR ORD", "RFO", "Request for Order"),
    ("REQUEST FOR ORDER", "RFO", "Request for Order"),
    ("TRIAL - ONE-DAY", "TRIAL", "Trial"),
    ("TRIAL ONE DAY", "TRIAL", "Trial"),
    ("CASE STATUS CON", "CSC", "Case Status Conference"),
    ("CASE STATUS CONFERENCE", "CSC", "Case Status Conference"),
    ("MANDATORY SETTL", "MSC", "Mandatory Settlement Conference"),
    ("MANDATORY SETTLEMENT", "MSC", "Mandatory Settlement Conference"),
    ("MANDATORY SETTLEMENT CONFERENCE", "MSC", "Mandatory Settlement Conference"),
    ("SELF REPRESENTE", "SRH", "Self-Represented Hearing"),
    ("DCSS HEARING -", "DCSS", "DCSS Hearing"),
    ("DCSS HEARING", "DCSS", "DCSS Hearing"),
    ("EX PARTE HEARIN", "EPH", "Ex Parte Hearing"),
    ("EX PARTE HEARING", "EPH", "Ex Parte Hearing"),
    ("TRIAL SETTING C", "TSC", "Trial Setting Conference"),
    ("TRIAL SETTING", "TSC", "Trial Setting Conference"),
    ("TRIAL SETTING CONFERENCE", "TSC", "Trial Setting Conference"),
    ("TSC", "TSC", "Trial Setting Conference"),
    ("TRIAL READINESS", "TRC", "Trial Readiness Conference"),
    ("ORDER TO SHOW C", "OSC", "Order to Show Cause"),
    ("ORDER TO SHOW CAUSE", "OSC", "Order to Show Cause"),
    ("REVIEW HEARING", "REV", "Review Hearing"),
    ("FSD", "FSD", "Family Support Division"),
    ("CMC", "CMC", "Case Management Conference"),
    ("CASE MANAGEMENT", "CMC", "Case Management Conference"),
    ("HOSC", "HOSC", "Hearing on Order to Show Cause"),
    ("MED", "MED", "Mediation"),
    ("MEDIATION", "MED", "Mediation"),
    ("FCSS MEDIATION", "FCSS", "Family Court Services Mediation"),
    ("FCSS", "FCSS", "Family Court Services Mediation"),
    ("CCRC", "CCRC", "Child Custody Recommending Counseling"),
    ("CCON", "CCON", "Custody/Visitation Conference"),
    ("PTR", "PTR", "Pretrial Review / Pretrial Conference"),
    ("PRETRIAL", "PTR", "Pretrial Review / Pretrial Conference"),
    ("HRG", "HRG", "Hearing"),
    ("HEARING", "HRG", "Hearing"),
    ("S/C", "S/C", "Status Conference"),
    ("STATUS CONFEREN", "S/C", "Status Conference"),
    ("DVRO", "DVRO", "Domestic Violence Restraining Order Hearing"),
    ("DV", "DV", "Domestic Violence Hearing"),
    ("DOMESTIC VIOLEN", "DV", "Domestic Violence Hearing"),
    ("CONT", "CONT", "Continuance"),
    ("CTN", "CTN", "Continuance"),
    ("CONTINUANCE", "CONT", "Continuance"),
    ("MIN", "MIN", "Minute Review"),
    ("MINUTE REVIEW", "MIN", "Minute Review"),
    ("RRC", "RRC", "Resolution Review Conference"),
    ("DR", "DR", "Default Review / Default Hearing"),
    ("DEFAULT", "DR", "Default Review / Default Hearing"),
    ("JUDG", "JUDG", "Judgment Hearing"),
    ("JUDGMENT", "JUDG", "Judgment Hearing"),
    ("MSC-FAM", "MSC-FAM", "Mandatory Settlement Conference (Family)"),
    ("EVIDENTIARY HEARING", "EVID", "Evidentiary Hearing"),
    ("TRO", "TRO", "Temporary Restraining Order Hearing"),
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS court_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_uid TEXT UNIQUE NOT NULL,
    date TEXT NOT NULL,
    start_time TEXT NOT NULL,
    datetime TEXT NOT NULL,
    dept_raw TEXT,
    dept TEXT NOT NULL,
    judge TEXT,
    case_number TEXT NOT NULL,
    party TEXT,
    party_role TEXT,
    attorney TEXT,
    purpose_raw TEXT,
    purpose TEXT,
    matter_id INTEGER,
    raw_line TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_court_events_date ON court_events(date);
CREATE INDEX IF NOT EXISTS idx_court_events_case_number ON court_events(case_number);
CREATE INDEX IF NOT EXISTS idx_court_events_matter_id ON court_events(matter_id);

CREATE TABLE IF NOT EXISTS purpose_mappings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_pattern TEXT NOT NULL UNIQUE,
    canonical_code TEXT NOT NULL,
    description TEXT,
    is_active INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS staff_cache (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    first_name TEXT,
    email TEXT,
    is_attorney INTEGER DEFAULT 0,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ringcentral_sync_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at TEXT DEFAULT CURRENT_TIMESTAMP,
    changed INTEGER NOT NULL,
    included_count INTEGER NOT NULL,
    conflict_count INTEGER NOT NULL,
    csv_path TEXT,
    conflicts_path TEXT,
    snapshot_hash TEXT NOT NULL
);

-- Per-matter persisted overrides for trust replenishment requests: a target
-- amount above the firm's $2,500 default (attorney anticipating upcoming
-- work), and/or a pause (case winding down) that sticks across every future
-- run until explicitly unpaused or the matter closes.
CREATE TABLE IF NOT EXISTS trust_matter_settings (
    matter_id INTEGER PRIMARY KEY,
    target_amount REAL,
    paused INTEGER NOT NULL DEFAULT 0,
    note TEXT,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Lifecycle log of trust replenishment requests. Clio's API has no way to
-- list TrustRequest records back (POST-only), so this is the only record of
-- what's already been requested. Deliberately keyed on raw trust balance,
-- not the WIP-aware "cushion" (trust - WIP) used by the separate monitor
-- report — requests only ever top up trust itself (2026-07-30: billing
-- stays manual/Clio-UI, this tool doesn't pre-fund WIP via trust requests).
-- status: 'pending' (sent, awaiting client payment), 'stale' (superseded by
-- a new request because trust moved since), 'resolved' (trust recovered to
-- target, no longer needed).
CREATE TABLE IF NOT EXISTS trust_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    matter_id INTEGER NOT NULL,
    target_amount REAL NOT NULL,
    trust_at_request REAL NOT NULL,
    requested_amount REAL NOT NULL,
    clio_trust_request_id INTEGER,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    resolved_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_trust_requests_matter_status ON trust_requests(matter_id, status);

-- One worksheet per asset/debt division exercise, tied to a Clio matter.
-- naming_mode 'husband_wife' (default) shows fixed H/W labels; 'first_names'
-- (the same-sex-couple case) shows party_a_label/party_b_label instead,
-- editable and normally pre-filled from the matter's client + Opposing Party
-- contact (see equalizer/clio_parties.py). Tax rates are per-worksheet, one
-- set of four (fed/state/long-term/short-term capital gain) per party — a
-- row picks which one applies via equalizer_items.rate_type, it does not
-- carry its own rate.
--
-- status: 'draft' (never pushed to Clio) or 'saved' (pushed at least once)
-- — purely informational, does NOT lock editing (Ted, 2026-08-14: "nothing
-- is ever final" in this line of work — a worksheet stays editable after
-- being saved to Clio, and Save to Clio can be clicked again any time,
-- pushing a new Document *version* rather than a duplicate file — see
-- equalizer/clio_documents.py). finalized_at is actually "first saved to
-- Clio at" (column name kept as-is, no real data existed to migrate when
-- the terminology changed); last_saved_at updates on every save, including
-- the first.
CREATE TABLE IF NOT EXISTS equalizer_worksheets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    matter_id INTEGER NOT NULL,
    matter_display_number TEXT NOT NULL,
    naming_mode TEXT NOT NULL DEFAULT 'husband_wife',
    party_a_label TEXT NOT NULL DEFAULT 'Husband',
    party_b_label TEXT NOT NULL DEFAULT 'Wife',
    party_a_role TEXT NOT NULL DEFAULT 'Petitioner',
    party_b_role TEXT NOT NULL DEFAULT 'Respondent',
    -- Defaults match the legacy Propertizer tool's own (see
    -- equalizer/store.py's DEFAULT_* constants, which are what actually
    -- apply these — a column DEFAULT here only governs a database created
    -- fresh with this schema, not one where the column already existed).
    fed_rate_a REAL NOT NULL DEFAULT 0.25,
    fed_rate_b REAL NOT NULL DEFAULT 0.25,
    state_rate_a REAL NOT NULL DEFAULT 0.093,
    state_rate_b REAL NOT NULL DEFAULT 0.093,
    lt_rate_a REAL NOT NULL DEFAULT 0.15,
    lt_rate_b REAL NOT NULL DEFAULT 0.15,
    st_rate_a REAL NOT NULL DEFAULT 0.25,
    st_rate_b REAL NOT NULL DEFAULT 0.25,
    status TEXT NOT NULL DEFAULT 'draft',
    clio_document_id INTEGER,
    finalized_at TEXT,
    last_saved_at TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_equalizer_worksheets_matter ON equalizer_worksheets(matter_id);

-- Equity (fmv - debt) is deliberately not stored — computed at read time so
-- it can never drift from its inputs (same reasoning as trust_monitor's
-- cushion/shortfall properties). after_tax_a/b are nullable: NULL means
-- "auto-computed from before_tax +/- the unrealized-gain tax hit" (see
-- equalizer/calc.py); a non-null value is a manual override a paralegal
-- typed in directly, which always wins.
CREATE TABLE IF NOT EXISTS equalizer_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    worksheet_id INTEGER NOT NULL REFERENCES equalizer_worksheets(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    fmv REAL NOT NULL DEFAULT 0,
    debt REAL NOT NULL DEFAULT 0,
    before_tax_a REAL NOT NULL DEFAULT 0,
    before_tax_b REAL NOT NULL DEFAULT 0,
    tax_basis REAL,
    rate_type TEXT NOT NULL DEFAULT 'none',
    gain_loss INTEGER NOT NULL DEFAULT 0,
    after_tax_a REAL,
    after_tax_b REAL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_equalizer_items_worksheet ON equalizer_items(worksheet_id, position);
"""


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    """Adds a column to an already-existing table if it predates that column (schema drift guard)."""
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def get_connection() -> sqlite3.Connection:
    """
    Opens a connection and ensures the schema exists. Deliberately re-checked
    on every call (not just once at app startup) — CREATE TABLE IF NOT EXISTS
    and INSERT OR IGNORE are cheap and idempotent, and this makes the app
    self-healing if data/clio_dashboard.db is ever deleted or replaced while
    the server keeps running (a startup-only check would otherwise 500 on
    every request until the process is restarted).
    """
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    _ensure_column(conn, "staff_cache", "is_attorney", "INTEGER DEFAULT 0")
    _ensure_column(conn, "equalizer_worksheets", "last_saved_at", "TEXT")
    conn.executemany(
        "INSERT OR IGNORE INTO purpose_mappings (raw_pattern, canonical_code, description) VALUES (?, ?, ?)",
        DEFAULT_PURPOSE_MAPPINGS,
    )
    conn.commit()
    return conn


def init_db() -> None:
    get_connection().close()


if __name__ == "__main__":
    init_db()
    print(f"Initialized {DB_PATH}")
