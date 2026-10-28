"""
clio_matter_update.py — Pushes the court calendar's case number into a Clio
matter's "Court Case Number" custom field. The only write path in the court
calendar sync (everything else in court_calendar/ is read-only) — triggered
one matter at a time by an explicit button click, never automatically.
"""

import logging
import os
import time

import requests
from dotenv import load_dotenv

load_dotenv()

BASE_URL = os.getenv("CLIO_BASE_URL", "https://app.clio.com").rstrip("/")
CASE_NUMBER_FIELD_NAME = "Court Case Number"
RETRY_DELAYS = [5, 15, 30]  # seconds between retries on 429, same backoff used elsewhere in this project

_case_number_field_id_cache: int | None = None


def _find_case_number_field_id(session: requests.Session) -> int:
    global _case_number_field_id_cache
    if _case_number_field_id_cache is not None:
        return _case_number_field_id_cache

    resp = session.get(
        f"{BASE_URL}/api/v4/custom_fields.json",
        params={"parent_type": "Matter", "query": CASE_NUMBER_FIELD_NAME, "fields": "id,name"},
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Failed to look up custom fields: {resp.status_code} {resp.text[:200]}")

    for cf in resp.json().get("data", []):
        if cf.get("name") == CASE_NUMBER_FIELD_NAME:
            _case_number_field_id_cache = int(cf["id"])
            return _case_number_field_id_cache

    raise RuntimeError(f"Could not find a '{CASE_NUMBER_FIELD_NAME}' custom field on Matter in this Clio account")


def _existing_value_id(session: requests.Session, matter_id: int, field_id: int) -> int | None:
    """The matter's CustomFieldValue id for this field, if it has one — even
    an empty one. Clio refuses a create when one exists (422 "custom field
    value ... already exists", found 2026-10-28: 16 open matters had an empty
    Court Case Number value), so an update must name it."""
    url = f"{BASE_URL}/api/v4/matters/{matter_id}.json"
    params = {"fields": "custom_field_values{id,custom_field}"}
    for attempt, delay in enumerate([0, *RETRY_DELAYS], start=1):
        if delay:
            logging.warning("Rate limited reading matter %s — waiting %ds (attempt %d)", matter_id, delay, attempt)
            time.sleep(delay)
        resp = session.get(url, params=params)
        if resp.status_code == 429:
            continue
        if resp.status_code != 200:
            raise RuntimeError(f"Failed to read matter {matter_id}: {resp.status_code} {resp.text[:300]}")
        for v in resp.json()["data"].get("custom_field_values") or []:
            if (v.get("custom_field") or {}).get("id") == field_id:
                return v["id"]
        return None
    raise RuntimeError(f"Failed to read matter {matter_id}: gave up after rate limiting")


def update_matter_case_number(session: requests.Session, matter_id: int, case_number: str) -> None:
    """
    PATCH the matter's Court Case Number custom field — creating the value,
    or updating the existing one by its CustomFieldValue id (see
    _existing_value_id: passing only custom_field{id} fails when one exists).
    """
    field_id = _find_case_number_field_id(session)
    url = f"{BASE_URL}/api/v4/matters/{matter_id}.json"
    value: dict = {"custom_field": {"id": field_id}, "value": case_number}
    value_id = _existing_value_id(session, matter_id, field_id)
    if value_id:
        value["id"] = value_id
    body = {"data": {"custom_field_values": [value]}}

    for attempt, delay in enumerate([0, *RETRY_DELAYS], start=1):
        if delay:
            logging.warning("Rate limited updating matter %s — waiting %ds (attempt %d)", matter_id, delay, attempt)
            time.sleep(delay)

        resp = session.patch(url, json=body)
        if resp.status_code == 200:
            logging.info("Updated matter %s Court Case Number -> %s", matter_id, case_number)
            return
        if resp.status_code == 429:
            continue
        raise RuntimeError(f"Failed to update matter {matter_id}: {resp.status_code} {resp.text[:300]}")

    raise RuntimeError(f"Failed to update matter {matter_id}: gave up after rate limiting")
