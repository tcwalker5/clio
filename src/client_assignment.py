"""
client_assignment.py — Find open matters missing Responsible Attorney,
Originating Attorney, and/or Responsible Staff, and assign them from a
fixed roster.

These three fields are nested User relationships on a Matter, not plain
fields (same gotcha court_calendar/matter_fields.py already documented) —
read via `responsible_attorney{id,name}` etc., written via PATCH
`{"data": {"responsible_attorney": {"id": <user_id>}}}`. Confirmed live
2026-09-02 against the designated test matter (DOE, JANE): setting
originating_attorney/responsible_staff works exactly this way, but Clio's
own OpenAPI spec states "The keyword `null` is not valid for this field"
for all three — confirmed live with a 422 trying to clear one. There is no
way to un-assign a field through this API once set; only Clio's own UI can
blank one again. That's fine for this tool's purpose (filling in blanks),
but means `update_matter_field()` below only ever sets a real user, never
clears one.

The attorney/paralegal roster is a fixed, explicit list (Ted, 2026-09-02),
not "everyone with subscription_type Attorney/NonAttorney" — Clio's
NonAttorney bucket also includes non-paralegal staff (confirmed live:
Dalinah Espinoza, Heather Brown, Ted Walker are all NonAttorney but none
belong in the Responsible Staff dropdown). Names are resolved live against
Clio's own staff directory (clio_users.py) rather than hardcoded IDs, same
reasoning clio_users.py itself was built for.

Usage:
  uv run src/client_assignment.py
"""

import argparse
import logging
import os
import sys
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv

import clio_users
from matter_matching import fetch_open_matters

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(ENV_PATH)

BASE_URL = os.getenv("CLIO_BASE_URL", "https://app.clio.com").rstrip("/")
ACCESS_TOKEN = os.getenv("CLIO_ACCESS_TOKEN", "")

RETRY_DELAYS = [5, 15, 30]  # seconds between retries on 429, same backoff as clio_matter_update.py

# Explicit roster (Ted, 2026-09-02) — edit here, not by changing the
# subscription_type filter, if the assignable list ever changes.
ATTORNEY_NAMES = ["Heidi Collier", "Dahann Bowers", "Pamela Bradford"]
PARALEGAL_NAMES = ["Misty Sherman", "Patricia Payne", "Sandy Cressey"]

ASSIGNMENT_FIELDS = ("responsible_attorney", "originating_attorney", "responsible_staff")
FIELD_ROSTER = {
    "responsible_attorney": "attorney",
    "originating_attorney": "attorney",
    "responsible_staff": "paralegal",
}
FIELD_LABELS = {
    "responsible_attorney": "Responsible Attorney",
    "originating_attorney": "Originating Attorney",
    "responsible_staff": "Responsible Staff",
}

MATTER_ASSIGNMENT_FIELDS = (
    "id,display_number,status,client{name},"
    "responsible_attorney{id,name},originating_attorney{id,name},responsible_staff{id,name}"
)


def setup_logging(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"client_assignment_{datetime.today().strftime('%Y%m%d')}.log"
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


def build_session() -> requests.Session:
    if not ACCESS_TOKEN:
        raise RuntimeError("CLIO_ACCESS_TOKEN not set in .env")
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {ACCESS_TOKEN}", "Content-Type": "application/json"})
    return session


def get_assignable_users() -> tuple[list[dict], list[dict]]:
    """Live directory lookup, narrowed to ATTORNEY_NAMES/PARALEGAL_NAMES —
    fails loud (not a silent skip) if a named person isn't found or an
    "attorney" name isn't actually marked Attorney in Clio, since a
    misspelled name here would otherwise silently vanish from the dropdown."""
    directory = clio_users.get_staff_directory()
    by_name = {info["name"]: {"id": uid, **info} for uid, info in directory.items()}

    attorneys = []
    for name in ATTORNEY_NAMES:
        info = by_name.get(name)
        if not info:
            raise RuntimeError(f"Attorney {name!r} not found in Clio's staff directory")
        if not info["is_attorney"]:
            raise RuntimeError(f"{name!r} is not marked as an Attorney in Clio (subscription_type)")
        attorneys.append(info)

    paralegals = []
    for name in PARALEGAL_NAMES:
        info = by_name.get(name)
        if not info:
            raise RuntimeError(f"Staff member {name!r} not found in Clio's staff directory")
        paralegals.append(info)

    return attorneys, paralegals


@dataclass
class MatterAssignment:
    matter_id: int
    display_number: str
    client_name: str
    responsible_attorney_id: int | None
    responsible_attorney_name: str
    originating_attorney_id: int | None
    originating_attorney_name: str
    responsible_staff_id: int | None
    responsible_staff_name: str

    @property
    def missing_count(self) -> int:
        return sum(1 for v in (self.responsible_attorney_id, self.originating_attorney_id, self.responsible_staff_id) if not v)

    @property
    def missing_any(self) -> bool:
        return self.missing_count > 0


def fetch_matters_for_assignment(session: requests.Session) -> list[MatterAssignment]:
    """Open matters only (Ted, 2026-09-02) — matches the convention every
    other monitor/report in this repo uses. Sorted most-incomplete first,
    then alphabetically, so the matters needing the most attention surface
    at the top of the page."""
    matters = fetch_open_matters(session, fields=MATTER_ASSIGNMENT_FIELDS)
    result = []
    for m in matters:
        ra = m.get("responsible_attorney") or {}
        oa = m.get("originating_attorney") or {}
        rs = m.get("responsible_staff") or {}
        client = m.get("client") or {}
        result.append(MatterAssignment(
            matter_id=int(m["id"]),
            display_number=m.get("display_number") or "",
            client_name=client.get("name") or "",
            responsible_attorney_id=ra.get("id"),
            responsible_attorney_name=ra.get("name") or "",
            originating_attorney_id=oa.get("id"),
            originating_attorney_name=oa.get("name") or "",
            responsible_staff_id=rs.get("id"),
            responsible_staff_name=rs.get("name") or "",
        ))
    result.sort(key=lambda x: (-x.missing_count, x.display_number))
    return result


def _counts_by_name(matters: list[MatterAssignment], name_attr: str, roster_names: list[str]) -> list[tuple[str, int]]:
    """Case count per person for one field, in roster order, with an
    "Unassigned" bucket appended if any open matter lacks it — the same
    kind of gap this whole page exists to surface, not dropped just
    because this is a summary. Fails loud (not a silent drop) if a matter
    is assigned to someone outside the fixed roster — see this module's
    docstring on why the roster is fixed rather than "everyone with
    subscription_type X"; a name showing up here that isn't in the roster
    means Clio's data and this list have drifted apart."""
    seen = Counter(getattr(m, name_attr) for m in matters)
    unassigned = seen.pop("", 0)
    unknown = set(seen) - set(roster_names)
    if unknown:
        raise RuntimeError(f"Matter(s) assigned to unexpected name(s) not in the fixed roster: {sorted(unknown)}")
    result = [(name, seen.get(name, 0)) for name in roster_names]
    if unassigned:
        result.append(("Unassigned", unassigned))
    return result


def build_caseload(matters: list[MatterAssignment]) -> tuple[list[tuple[str, int]], list[tuple[str, int]]]:
    """Case count per person for the two roles Ted asked to chart
    (2026-09-03): Responsible Attorney and Responsible Staff. Originating
    Attorney is deliberately left out of this — confirmed live it's almost
    exclusively Heidi Collier in practice (163 of 226 open matters, vs. 3
    for Dahann Bowers and 59 unset), so a chart of it wouldn't tell staff
    anything they don't already know."""
    attorney_counts = _counts_by_name(matters, "responsible_attorney_name", ATTORNEY_NAMES)
    paralegal_counts = _counts_by_name(matters, "responsible_staff_name", PARALEGAL_NAMES)
    return attorney_counts, paralegal_counts


def update_matter_field(session: requests.Session, matter_id: int, field: str, user_id: int) -> str:
    """PATCH one of the three assignment fields to a real user. Returns the
    assigned name as confirmed by Clio's own response. See this module's
    docstring — there is no way to clear a field back to blank through this
    API, so `user_id` must always be a real, valid id for `field`'s roster;
    callers (the /assignments/set route) validate that before calling this."""
    if field not in ASSIGNMENT_FIELDS:
        raise ValueError(f"Unknown assignment field: {field!r}")

    url = f"{BASE_URL}/api/v4/matters/{matter_id}.json"
    body = {"data": {field: {"id": user_id}}}
    params = {"fields": f"id,{field}{{id,name}}"}

    for attempt, delay in enumerate([0, *RETRY_DELAYS], start=1):
        if delay:
            logging.warning("Rate limited updating matter %s %s — waiting %ds (attempt %d)", matter_id, field, delay, attempt)
            time.sleep(delay)

        logging.info("PATCH matter %s %s -> user %s", matter_id, field, user_id)
        resp = session.patch(url, json=body, params=params)
        if resp.status_code == 200:
            name = ((resp.json().get("data") or {}).get(field) or {}).get("name", "")
            logging.info("Updated matter %s %s -> %s (user %s)", matter_id, field, name, user_id)
            return name
        if resp.status_code == 429:
            continue
        logging.error("Failed to update matter %s %s: %s %s", matter_id, field, resp.status_code, resp.text[:300])
        raise RuntimeError(f"Failed to update matter {matter_id} {field}: {resp.status_code} {resp.text[:300]}")

    raise RuntimeError(f"Failed to update matter {matter_id} {field}: gave up after rate limiting")


def run_pipeline() -> list[MatterAssignment]:
    setup_logging(Path("logs"))
    session = build_session()
    matters = fetch_matters_for_assignment(session)
    missing = [m for m in matters if m.missing_any]
    logging.info("Checked %d open matter(s) — %d missing at least one assignment", len(matters), len(missing))
    return matters


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.parse_args()
    matters = run_pipeline()
    for m in matters:
        if not m.missing_any:
            continue
        gaps = [FIELD_LABELS[f] for f in ASSIGNMENT_FIELDS if getattr(m, f + "_id") is None]
        print(f"{m.display_number:<30} missing: {', '.join(gaps)}")


if __name__ == "__main__":
    main()
