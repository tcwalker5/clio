"""
clio_notes.py — Posts a Note on the Clio matter linking back to the live
Equalizer worksheet, so staff browsing the matter in Clio can find (and
reopen) it without hunting through the dashboard's own worksheet list.
Runs once, at Finalize — a secondary, best-effort step alongside the PDF
upload (clio_documents.py), not a required part of it: if this fails, the
worksheet is still finalized and the PDF is still saved, just without the
matter-side pointer.

Shape taken from clio-rate-import's openapi.json (POST /notes.json) as a
first pass only, per this project's standing correction
(reference_clio_api.md, 2026-07-22) — not yet exercised against the real
Clio account. One thing worth confirming live: the spec's top-level
`required` list for the request body includes `contact` even though the
field-level description says it's "required only if the Note type is
Contact" — plausibly a spec artifact rather than real API behavior, but
untested either way.
"""

import logging
import os

import requests

BASE_URL = os.getenv("CLIO_BASE_URL", "https://app.clio.com").rstrip("/")
NOTES_ENDPOINT = f"{BASE_URL}/api/v4/notes.json"

# Where staff reach the dashboard from elsewhere on the office LAN — see
# CLAUDE.md's "On-LAN access" note (CAP Dashboard.url points here too).
# Deliberately not localhost/127.0.0.1: this URL goes into a Note any staff
# member might open, not just whoever's running the server.
CAP_BASE_URL = os.getenv("CAP_BASE_URL", "http://cap.lan:8421").rstrip("/")


def create_worksheet_note(session: requests.Session, matter_id: int, worksheet_id: int) -> int:
    """Returns the new Note's Clio id."""
    link = f"{CAP_BASE_URL}/equalizer/{worksheet_id}"
    payload = {
        "data": {
            "type": "Matter",
            "matter": {"id": matter_id},
            "subject": "Equalizer worksheet",
            "detail": f'<p>Asset/debt division worksheet finalized — <a href="{link}">open it in CAP</a>.</p>',
            "detail_text_type": "rich_text",
        }
    }
    logging.info("POST %s matter=%s payload=%s", NOTES_ENDPOINT, matter_id, payload)
    resp = session.post(NOTES_ENDPOINT, json=payload)
    logging.info("Response: %s %s", resp.status_code, resp.text[:500])
    if resp.status_code != 201:
        raise RuntimeError(f"Failed to create note for matter {matter_id}: {resp.status_code} {resp.text[:300]}")
    return resp.json()["data"]["id"]
