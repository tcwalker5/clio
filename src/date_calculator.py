"""
date_calculator.py — Generic date-duration calculator (years/months/days
between two dates), plus a family-law-specific application: length of
marriage (Date of Marriage -> Date of Separation), including California's
Family Code § 4336 "long-term marriage" (10+ years) presumption.

Reuses moore_marsden/clio_matter_dates.py's existing Date of Marriage/Date
of Separation field names, read, AND write logic rather than re-solving
the same lookup — these are the same two real Clio matter custom fields
Moore/Marsden already owns (ids 18509746/18509761, confirmed live
2026-08-18). **Corrected 2026-09-04** — an earlier version of this tool
was deliberately read-only on those two fields, sending staff to
Moore/Marsden's own Settings to actually set them; Ted reversed that
after using it: "each tool should not depend on another tool or force the
user to navigate to another one." Date Calculator's Save action now
writes Date of Marriage/Separation itself (via
moore_marsden.clio_matter_dates.update_matter_dates(), not a duplicate
implementation) in the same click that writes Length of Marriage —
`routes_date_calculator.py` owns this write path, `date_calculator.py`
itself doesn't call it directly since it's a UI-triggered action, not
part of the background sync.

This tool also writes its own new field, "Length of Marriage" (text, e.g.
"8 years, 4 months — Long-Term Marriage") — a real Clio custom field Ted
created for this (text type, parent type Matter).

Two ways this field gets populated:
1. Interactively, from /date-calculator — attach a matter (auto-fills
   Date of Marriage/Separation if already set), enter or correct dates,
   and click Save to write all three fields (Date of Marriage, Date of
   Separation, Length of Marriage) at once.
2. In the background: `uv run src/date_calculator.py` scans every open/
   pending matter, and for any with BOTH Date of Marriage and Date of
   Separation set, computes and writes the field only if the computed
   value differs from what's already stored — same "only touch what
   changed" discipline as ringcentral_directory.py's own sync, so a run
   against unchanged matters is a silent no-op. Intended to run daily via
   a Windows Scheduled Task (per-feature pattern, see src/web/CLAUDE.md's
   CAP section) — Clio's own webhook API needs a public HTTPS endpoint,
   which this LAN-only dashboard deliberately doesn't have, so polling is
   the fit here, not a push.

Usage:
  uv run src/date_calculator.py
"""

import argparse
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

from matter_matching import fetch_open_matters
from moore_marsden.clio_matter_dates import DATE_OF_MARRIAGE_FIELD_NAME, DATE_OF_SEPARATION_FIELD_NAME

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(ENV_PATH)

BASE_URL = os.getenv("CLIO_BASE_URL", "https://app.clio.com").rstrip("/")
ACCESS_TOKEN = os.getenv("CLIO_ACCESS_TOKEN", "")

LONG_TERM_MARRIAGE_YEARS = 10  # Family Code § 4336 — "10 years or more" presumption threshold
LENGTH_OF_MARRIAGE_FIELD_NAME = "Length of Marriage"
RETRY_DELAYS = [5, 15, 30]  # seconds between retries on 429, same backoff used elsewhere in this project

# Sync job scope: open + pending, same reasoning as Moore/Marsden's own matter
# search — a matter can plausibly have Date of Marriage/Separation entered
# (via Moore/Marsden) before its status flips to Open.
SYNC_STATUS = "open,pending"
MATTER_FIELDS_FOR_SYNC = "id,display_number,custom_field_values{field_name,value}"


@dataclass
class Duration:
    years: int
    months: int
    days: int
    total_days: int

    @property
    def is_long_term(self) -> bool:
        return self.years >= LONG_TERM_MARRIAGE_YEARS

    def format(self) -> str:
        """Human-readable summary for the Length of Marriage field —
        years/months only (day-level precision isn't meaningful for a
        summary field meant to be scanned at a glance), with the
        long-term-marriage callout appended when it applies."""
        parts = []
        if self.years:
            parts.append(f"{self.years} year{'s' if self.years != 1 else ''}")
        if self.months:
            parts.append(f"{self.months} month{'s' if self.months != 1 else ''}")
        if not parts:
            parts.append(f"{self.days} day{'s' if self.days != 1 else ''}")
        text = ", ".join(parts)
        if self.is_long_term:
            text += " — Long-Term Marriage"
        return text


def calculate_duration(start: date, end: date) -> Duration:
    """Calendar-accurate years/months/days between two dates (order-
    independent — always returns a non-negative duration). The standard
    borrow-from-the-previous-month technique (the same one python-
    dateutil's relativedelta uses internally) — not pulled in as a
    dependency since this project has no dateutil anywhere else and the
    algorithm is a dozen lines."""
    if end < start:
        start, end = end, start
    years = end.year - start.year
    months = end.month - start.month
    days = end.day - start.day
    if days < 0:
        months -= 1
        prev_month_end = end.replace(day=1) - timedelta(days=1)
        days += prev_month_end.day
    if months < 0:
        years -= 1
        months += 12
    return Duration(years=years, months=months, days=days, total_days=(end - start).days)


def parse_iso_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def build_session() -> requests.Session:
    if not ACCESS_TOKEN:
        raise RuntimeError("CLIO_ACCESS_TOKEN not set in .env")
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {ACCESS_TOKEN}", "Content-Type": "application/json"})
    return session


_field_id_cache: int | None = None


def _find_length_of_marriage_field_id(session: requests.Session) -> int:
    global _field_id_cache
    if _field_id_cache is not None:
        return _field_id_cache
    resp = session.get(
        f"{BASE_URL}/api/v4/custom_fields.json",
        params={"parent_type": "Matter", "query": LENGTH_OF_MARRIAGE_FIELD_NAME, "fields": "id,name"},
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Failed to look up custom fields: {resp.status_code} {resp.text[:200]}")
    for cf in resp.json().get("data", []):
        if cf.get("name") == LENGTH_OF_MARRIAGE_FIELD_NAME:
            _field_id_cache = int(cf["id"])
            return _field_id_cache
    raise RuntimeError(
        f"Could not find a '{LENGTH_OF_MARRIAGE_FIELD_NAME}' custom field on Matter in this Clio account "
        "— create it in Clio's Custom Fields settings first (text type, parent type Matter)."
    )


def _custom_field_value(cfvs: list[dict], field_name: str) -> tuple[str | None, str | None]:
    """(value, CustomFieldValue id) for the named field among an already-
    fetched matter's custom_field_values list, or (None, None)."""
    for cfv in cfvs:
        if cfv.get("field_name") == field_name:
            return cfv.get("value"), cfv.get("id")
    return None, None


def fetch_length_of_marriage(session: requests.Session, matter_id: int) -> tuple[str | None, str | None]:
    """(current value, existing CustomFieldValue id or None) — a single-
    matter live fetch, used by the interactive page (one matter at a time)
    and as update_length_of_marriage()'s fallback when the caller doesn't
    already have this from a bulk fetch (see sync_length_of_marriage)."""
    resp = session.get(
        f"{BASE_URL}/api/v4/matters/{matter_id}.json",
        params={"fields": "custom_field_values{id,field_name,value}"},
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Failed to fetch matter {matter_id}: {resp.status_code} {resp.text[:200]}")
    return _custom_field_value(resp.json()["data"].get("custom_field_values", []), LENGTH_OF_MARRIAGE_FIELD_NAME)


def update_length_of_marriage(
    session: requests.Session, matter_id: int, text_value: str, existing_id: str | None = None,
) -> None:
    """Same existing-CustomFieldValue-id gotcha clio_matter_dates.py already
    documented for Date of Marriage/Separation — some fields get an auto-
    created empty CustomFieldValue record on every matter, and POSTing a
    fresh one via custom_field{id} against a matter that already has one
    422s. Check for an existing id first (or accept one the caller already
    has, e.g. from a bulk fetch); only fall back to the custom_field{id}
    create-shape when genuinely none exists yet."""
    if existing_id is None:
        _, existing_id = fetch_length_of_marriage(session, matter_id)

    if existing_id:
        value_entry = {"id": existing_id, "value": text_value}
    else:
        value_entry = {"custom_field": {"id": _find_length_of_marriage_field_id(session)}, "value": text_value}

    url = f"{BASE_URL}/api/v4/matters/{matter_id}.json"
    body = {"data": {"custom_field_values": [value_entry]}}

    for attempt, delay in enumerate([0, *RETRY_DELAYS], start=1):
        if delay:
            logging.warning("Rate limited updating matter %s Length of Marriage — waiting %ds (attempt %d)", matter_id, delay, attempt)
            time.sleep(delay)

        resp = session.patch(url, json=body)
        if resp.status_code == 200:
            logging.info("Updated matter %s Length of Marriage -> %s", matter_id, text_value)
            return
        if resp.status_code == 429:
            continue
        raise RuntimeError(f"Failed to update matter {matter_id} Length of Marriage: {resp.status_code} {resp.text[:300]}")

    raise RuntimeError(f"Failed to update matter {matter_id} Length of Marriage: gave up after rate limiting")


def setup_logging(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"date_calculator_{datetime.today().strftime('%Y%m%d')}.log"
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


def sync_length_of_marriage(session: requests.Session) -> dict:
    """Scans every open/pending matter in ONE bulk fetch (custom_field_values
    included directly in the matters.json call, same pattern
    court_calendar/matter_fields.py already established) — not a separate
    live fetch per matter, which would be 2x N extra API calls for a
    225-matter firm. Only matters actually needing an update ever get a
    second (write) call."""
    matters = fetch_open_matters(session, fields=MATTER_FIELDS_FOR_SYNC, status=SYNC_STATUS)
    checked = len(matters)
    both_dates = 0
    updated = 0
    for m in matters:
        cfvs = m.get("custom_field_values") or []
        dom, _ = _custom_field_value(cfvs, DATE_OF_MARRIAGE_FIELD_NAME)
        dos, _ = _custom_field_value(cfvs, DATE_OF_SEPARATION_FIELD_NAME)
        if not (dom and dos):
            continue
        both_dates += 1

        text_value = calculate_duration(parse_iso_date(dom), parse_iso_date(dos)).format()
        current_value, existing_id = _custom_field_value(cfvs, LENGTH_OF_MARRIAGE_FIELD_NAME)
        if current_value == text_value:
            continue

        update_length_of_marriage(session, int(m["id"]), text_value, existing_id=existing_id)
        updated += 1

    summary = {"checked": checked, "both_dates_set": both_dates, "updated": updated}
    logging.info(
        "Checked %d open/pending matter(s), %d with both Date of Marriage and Date of Separation set, "
        "%d Length of Marriage value(s) updated",
        checked, both_dates, updated,
    )
    return summary


def run_pipeline() -> dict:
    setup_logging(Path("logs"))
    session = build_session()
    return sync_length_of_marriage(session)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.parse_args()
    run_pipeline()


if __name__ == "__main__":
    main()
