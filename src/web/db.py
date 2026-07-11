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
