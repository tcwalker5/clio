"""
clio_parties.py — Default party names for a new Moore/Marsden worksheet,
pulled from the linked matter: the client contact (assumed to usually be the
owner spouse — the one whose separate property is at issue — editable if
that assumption is wrong for a given matter) and the Opposing Party
relationship contact (the non-owner spouse). Same OC/OP lookup pattern
equalizer/clio_parties.py and outlook_calendar/relationships.py already
established, scoped to one matter's relationships — duplicated here rather
than imported (this project's convention: each subproject owns its own Clio-
facing helpers), so keep the three in sync if any of them changes.

Purely a convenience prefill — never blocks worksheet creation. If the
Opposing Party lookup fails or finds nothing, the field is left blank for a
human to type in rather than raising.
"""

import logging
import os
import re

import requests

BASE_URL = os.getenv("CLIO_BASE_URL", "https://app.clio.com").rstrip("/")
MATTER_FIELDS = "id,display_number,client{id,name,first_name,type}"
RELATIONSHIPS_FIELDS = "id,description,contact{id,name,first_name,type}"

_OP_RE = re.compile(r"\bOP\b|OPPOSING\s+PART(Y|IES)", re.IGNORECASE)


def fetch_matter_summary(session: requests.Session, matter_id: int) -> dict:
    resp = session.get(f"{BASE_URL}/api/v4/matters/{matter_id}.json", params={"fields": MATTER_FIELDS})
    if resp.status_code != 200:
        raise RuntimeError(f"Failed to fetch matter {matter_id}: {resp.status_code} {resp.text[:200]}")
    return resp.json()["data"]


def fetch_default_names(session: requests.Session, matter_id: int) -> tuple[str, str]:
    """(owner spouse first name, non-owner spouse first name) — "" for
    whichever side isn't found or isn't a Person (a Company client has no
    first name to offer), never guessed."""
    owner_name = ""
    try:
        matter = fetch_matter_summary(session, matter_id)
        client = matter.get("client") or {}
        if client.get("type") == "Person":
            owner_name = client.get("first_name") or client.get("name") or ""
        else:
            owner_name = client.get("name") or ""
    except RuntimeError as e:
        logging.warning("Could not fetch matter %s for default party names: %s", matter_id, e)

    non_owner_name = ""
    try:
        resp = session.get(
            f"{BASE_URL}/api/v4/relationships.json",
            params={"fields": RELATIONSHIPS_FIELDS, "matter_id": matter_id, "limit": 200},
        )
        if resp.status_code == 200:
            for r in resp.json().get("data", []):
                if _OP_RE.search(r.get("description") or ""):
                    contact = r.get("contact") or {}
                    non_owner_name = contact.get("first_name") or contact.get("name") or ""
                    break
        else:
            logging.warning("Relationship lookup for matter %s returned %s — leaving non-owner name blank",
                             matter_id, resp.status_code)
    except requests.RequestException as e:
        logging.warning("Could not fetch relationships for matter %s: %s", matter_id, e)

    return owner_name, non_owner_name
