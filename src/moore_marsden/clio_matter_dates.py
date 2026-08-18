"""
clio_matter_dates.py — Reads and writes a matter's Date of Marriage / Date of
Separation custom fields. These are real, existing matter custom fields at
this firm (confirmed live 2026-08-18: "Date of Marriage" id 18509746, "Date
of Separation" id 18509761, both field_type "date") — not something this
tool invents. Clio is the source of truth: the worksheet never stores its
own copy of either date, it fetches live on every page load (same pattern
as clio_documents.py's trash-status check) and writes straight through to
Clio whenever staff enter or correct one, rather than caching a local value
that could drift from the matter itself. Other subprojects (or a human in
Clio's own UI) that touch these same fields are automatically picked up
next load.

**Real gotcha found live 2026-08-18, different from Court Case Number's own
docstring claim:** "passing custom_field{id} without an existing
CustomFieldValue id creates the value if it doesn't exist yet" is NOT
universally true — DOE, JANE already had an empty (value: null)
CustomFieldValue *record* for Date of Separation (id like "date-1372717531"),
and POSTing a fresh one via custom_field{id} against a matter that already
has one, even with a null value, fails with a 422 ArgumentError: "custom
field value for custom field ... already exists". Text fields like Court
Case Number apparently don't get this auto-created empty placeholder record
on every matter, but these date fields do (maybe firm/intake-form
configuration). Fixed by checking for an existing value id first via
fetch_matter_dates()-style read, and updating THAT record's own id
(`{"id": "date-1372717531", "value": ...}`, no custom_field key needed) when
one exists — confirmed live this succeeds where custom_field{id} 422'd.
Falls back to the custom_field{id} create-shape only when no existing
CustomFieldValue is found for that field on that matter, same as Court Case
Number's own path for a genuinely never-touched field.
"""

import logging
import os
import time

import requests

BASE_URL = os.getenv("CLIO_BASE_URL", "https://app.clio.com").rstrip("/")
DATE_OF_MARRIAGE_FIELD_NAME = "Date of Marriage"
DATE_OF_SEPARATION_FIELD_NAME = "Date of Separation"
RETRY_DELAYS = [5, 15, 30]  # seconds between retries on 429, same backoff used elsewhere in this project

_field_id_cache: dict[str, int] = {}


def _find_field_id(session: requests.Session, field_name: str) -> int:
    if field_name in _field_id_cache:
        return _field_id_cache[field_name]

    resp = session.get(
        f"{BASE_URL}/api/v4/custom_fields.json",
        params={"parent_type": "Matter", "query": field_name, "fields": "id,name"},
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Failed to look up custom fields: {resp.status_code} {resp.text[:200]}")

    for cf in resp.json().get("data", []):
        if cf.get("name") == field_name:
            _field_id_cache[field_name] = int(cf["id"])
            return _field_id_cache[field_name]

    raise RuntimeError(f"Could not find a '{field_name}' custom field on Matter in this Clio account")


def _fetch_custom_field_values(session: requests.Session, matter_id: int) -> list[dict]:
    resp = session.get(
        f"{BASE_URL}/api/v4/matters/{matter_id}.json",
        params={"fields": "custom_field_values{id,field_name,value}"},
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Failed to fetch matter {matter_id}: {resp.status_code} {resp.text[:200]}")
    return resp.json()["data"].get("custom_field_values", [])


def fetch_matter_dates(session: requests.Session, matter_id: int) -> tuple[str | None, str | None]:
    """(date_of_marriage, date_of_separation) as ISO date strings ("YYYY-MM-DD",
    matching Clio's own format and this project's <input type="date"> fields
    directly) — either can be None if not yet set on the matter."""
    date_of_marriage = None
    date_of_separation = None
    for cfv in _fetch_custom_field_values(session, matter_id):
        if cfv.get("field_name") == DATE_OF_MARRIAGE_FIELD_NAME:
            date_of_marriage = cfv.get("value")
        elif cfv.get("field_name") == DATE_OF_SEPARATION_FIELD_NAME:
            date_of_separation = cfv.get("value")
    return date_of_marriage, date_of_separation


def update_matter_dates(
    session: requests.Session, matter_id: int,
    date_of_marriage: str | None = None, date_of_separation: str | None = None,
) -> None:
    """Writes whichever of the two dates is provided (non-None) to the
    matter's custom fields. Passing both updates both in one request."""
    wanted = {}
    if date_of_marriage is not None:
        wanted[DATE_OF_MARRIAGE_FIELD_NAME] = date_of_marriage
    if date_of_separation is not None:
        wanted[DATE_OF_SEPARATION_FIELD_NAME] = date_of_separation
    if not wanted:
        return

    existing_ids = {
        cfv["field_name"]: cfv["id"]
        for cfv in _fetch_custom_field_values(session, matter_id)
        if cfv.get("field_name") in wanted
    }

    values = []
    for field_name, value in wanted.items():
        existing_id = existing_ids.get(field_name)
        if existing_id:
            values.append({"id": existing_id, "value": value})
        else:
            values.append({"custom_field": {"id": _find_field_id(session, field_name)}, "value": value})

    url = f"{BASE_URL}/api/v4/matters/{matter_id}.json"
    body = {"data": {"custom_field_values": values}}

    for attempt, delay in enumerate([0, *RETRY_DELAYS], start=1):
        if delay:
            logging.warning("Rate limited updating matter %s dates — waiting %ds (attempt %d)", matter_id, delay, attempt)
            time.sleep(delay)

        resp = session.patch(url, json=body)
        if resp.status_code == 200:
            logging.info("Updated matter %s dates -> marriage=%s separation=%s", matter_id, date_of_marriage, date_of_separation)
            return
        if resp.status_code == 429:
            continue
        raise RuntimeError(f"Failed to update matter {matter_id} dates: {resp.status_code} {resp.text[:300]}")

    raise RuntimeError(f"Failed to update matter {matter_id} dates: gave up after rate limiting")
