"""
clio_notes.py — Posts a Note on the Clio matter linking back to the live
Moore/Marsden worksheet, so staff browsing the matter in Clio can find (and
reopen) it without hunting through the dashboard's own worksheet list. Runs
once, on the *first* Save to Clio only — a secondary, best-effort step
alongside the PDF upload (clio_documents.py), not a required part of it: if
this fails, the PDF is still saved, just without the matter-side pointer.
Not re-posted on later saves, since the same link stays correct.

Same shape as equalizer/clio_notes.py, own copy per this project's
convention (each subproject owns its own Clio-writing helpers).
"""

import logging
import os

import requests

BASE_URL = os.getenv("CLIO_BASE_URL", "https://app.clio.com").rstrip("/")
NOTES_ENDPOINT = f"{BASE_URL}/api/v4/notes.json"

# Where staff reach the dashboard from elsewhere on the office LAN — see
# CLAUDE.md's "On-LAN access" note. Deliberately not localhost/127.0.0.1:
# this URL goes into a Note any staff member might open, not just whoever's
# running the server.
CAP_BASE_URL = os.getenv("CAP_BASE_URL", "http://cap.lan:8421").rstrip("/")


def create_worksheet_note(session: requests.Session, matter_id: int, worksheet_id: int) -> int:
    """Returns the new Note's Clio id."""
    link = f"{CAP_BASE_URL}/moore-marsden/{worksheet_id}"
    payload = {
        "data": {
            "type": "Matter",
            "matter": {"id": matter_id},
            "subject": "Moore/Marsden worksheet",
            "detail": f'<p>Moore/Marsden calculation saved to Clio — <a href="{link}">open it in CAP</a>.</p>',
            "detail_text_type": "rich_text",
        }
    }
    logging.info("POST %s matter=%s payload=%s", NOTES_ENDPOINT, matter_id, payload)
    resp = session.post(NOTES_ENDPOINT, json=payload)
    logging.info("Response: %s %s", resp.status_code, resp.text[:500])
    if resp.status_code != 201:
        raise RuntimeError(f"Failed to create note for matter {matter_id}: {resp.status_code} {resp.text[:300]}")
    return resp.json()["data"]["id"]
