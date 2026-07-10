"""
store.py — SQLite persistence for parsed court calendar events.

event_uid is keyed on case_number + dept + purpose_raw (not date) so that a
hearing whose date/time changes on re-import updates the same row instead of
creating a duplicate — matching calendar-check's original convention (see
its AGENTS.md "Known issue: stale court_events records when purpose changes"
note; a purpose change is treated as a different hearing, same as before).
"""

import hashlib
from datetime import date, datetime

from court_calendar.normalizer import CourtEvent, normalize_purpose
from web.db import get_connection


def _event_uid(ce: CourtEvent) -> str:
    key = f"{ce.case_number}|{ce.dept}|{ce.purpose_raw}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]


def get_purpose_mappings() -> dict[str, str]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT raw_pattern, canonical_code FROM purpose_mappings WHERE is_active = 1"
        ).fetchall()
        return {r["raw_pattern"]: r["canonical_code"] for r in rows}
    finally:
        conn.close()


def upsert_court_events(events: list[CourtEvent]) -> int:
    mappings = get_purpose_mappings()
    conn = get_connection()
    try:
        for ce in events:
            conn.execute(
                """
                INSERT INTO court_events
                    (event_uid, date, start_time, datetime, dept_raw, dept, judge,
                     case_number, party, party_role, attorney, purpose_raw, purpose, raw_line, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(event_uid) DO UPDATE SET
                    date = excluded.date, start_time = excluded.start_time, datetime = excluded.datetime,
                    dept_raw = excluded.dept_raw, dept = excluded.dept, judge = excluded.judge,
                    party = excluded.party, party_role = excluded.party_role, attorney = excluded.attorney,
                    purpose_raw = excluded.purpose_raw, purpose = excluded.purpose,
                    raw_line = excluded.raw_line, updated_at = CURRENT_TIMESTAMP
                """,
                (
                    _event_uid(ce), ce.date, ce.start_time, ce.dt.isoformat(),
                    ce.dept_raw, ce.dept, ce.judge, ce.case_number, ce.party, ce.party_role,
                    ce.attorney, ce.purpose_raw, normalize_purpose(ce.purpose_raw, mappings), ce.raw_line,
                ),
            )
        conn.commit()
        return len(events)
    finally:
        conn.close()


def fetch_upcoming_events(from_date: str | None = None) -> list[dict]:
    from_date = from_date or date.today().isoformat()
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM court_events WHERE date >= ? ORDER BY datetime ASC",
            (from_date,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def fetch_date_range() -> tuple[str, str] | None:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT MIN(date) AS min_date, MAX(date) AS max_date FROM court_events WHERE date >= ?",
            (date.today().isoformat(),),
        ).fetchone()
        if not row or not row["min_date"]:
            return None
        return row["min_date"], row["max_date"]
    finally:
        conn.close()
