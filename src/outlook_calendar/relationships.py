"""
relationships.py — Clio /relationships.json client for Opposing Counsel /
Opposing Party matching, used as a second pass over the Outlook migration's
exceptions file.

A Relationship ties a Contact to a Matter via a free-text `description`
("Opposing Counsel", "Opposing Party", etc.) — this is how Clio already
tracks the other side of a case. matter_matching.py can't see these contacts
at all since it only indexes Matters, so a TCON/OCON call with opposing
counsel (or an unrepresented opposing party) always lands in exceptions even
though Clio already knows who that person is.

Real-data taxonomy check against this firm's ~200 relationships found
"Opposing Party" and "Opposing Counsel" as the dominant values, plus assorted
free-text variants staff actually type ("Atty for Opposing Party", "Paralegal
for OC", "Opposing Counsel - Partner", inconsistent casing) — matched with a
regex here, not an exact-string set.
"""

import logging
import os
import re
from dataclasses import dataclass

import requests

BASE_URL = os.getenv("CLIO_BASE_URL", "https://app.clio.com").rstrip("/")
RELATIONSHIPS_ENDPOINT = f"{BASE_URL}/api/v4/relationships.json"
RELATIONSHIPS_FIELDS = "id,description,matter{id,display_number,status},contact{id,name}"
PAGE_SIZE = 200

_OC_RE = re.compile(r"\bOC\b|OPPOSING\s+COUNSEL", re.IGNORECASE)
_OP_RE = re.compile(r"\bOP\b|OPPOSING\s+PART(Y|IES)", re.IGNORECASE)


@dataclass
class OcOpContact:
    name: str  # contact name, uppercased
    role: str  # "OC" or "OP"
    raw_description: str
    matter_id: int
    matter_display_number: str
    matter_status: str
    contact_id: int | None = None  # Clio contact ID, when the relationship's contact resolved to one


def fetch_oc_op_contacts(session: requests.Session) -> list[OcOpContact]:
    """Every Relationship whose description reads as Opposing Counsel or
    Opposing Party — deliberately across ALL matter statuses, not just open.
    A closed matter can still have old Outlook calendar history worth
    flagging (e.g. a call with opposing counsel on a case that's since
    settled) — the migration's regular client matching already only looks at
    open matters, so this is meant to catch what that path can't."""
    contacts: list[OcOpContact] = []
    next_url: str | None = None
    page = 1
    while True:
        if next_url:
            resp = session.get(next_url)
        else:
            resp = session.get(RELATIONSHIPS_ENDPOINT, params={"fields": RELATIONSHIPS_FIELDS, "limit": PAGE_SIZE})
        if resp.status_code != 200:
            raise RuntimeError(f"Failed to fetch relationships (page {page}): {resp.status_code} {resp.text[:200]}")

        body = resp.json()
        for r in body.get("data", []):
            desc = r.get("description") or ""
            if _OC_RE.search(desc):
                role = "OC"
            elif _OP_RE.search(desc):
                role = "OP"
            else:
                continue

            contact = r.get("contact") or {}
            matter = r.get("matter") or {}
            name = (contact.get("name") or "").strip().upper()
            if not name or not matter.get("id"):
                continue

            contacts.append(OcOpContact(
                name=name,
                role=role,
                raw_description=desc,
                matter_id=int(matter["id"]),
                matter_display_number=matter.get("display_number", ""),
                matter_status=matter.get("status", ""),
                contact_id=int(contact["id"]) if contact.get("id") else None,
            ))

        next_url = body.get("meta", {}).get("paging", {}).get("next")
        logging.info("Fetched relationships page %d", page)
        page += 1
        if not next_url:
            break

    logging.info("Indexed %d OC/OP contacts (out of all relationships fetched)", len(contacts))
    return contacts


def _match_text(text: str, contacts: list[OcOpContact]) -> list[OcOpContact]:
    """Every OC/OP contact whose full name appears in `text` — deliberately
    one-directional (the contact's name must be found IN the text, not the
    reverse). A real-data check found the reverse direction produces false
    positives two different ways: short leftover fragments from a failed
    party-name extraction (e.g. "s", "ta") trivially match as substrings of
    any long contact name, and a bare last name shared between our client and
    the opposing party (e.g. hearing subject "ROJAS FRC" vs. OP contact
    "ANEL ROJAS") falsely matches when checked in that direction. Requiring
    the full (usually two-word) contact name to appear in the text avoids
    both."""
    text = (text or "").strip().upper()
    if len(text) < 4:
        return []
    return [c for c in contacts if c.name in text]


def match_exception_to_oc_op(exception_row: dict, contacts: list[OcOpContact]) -> list[OcOpContact]:
    """Try the already-extracted party first, then the raw subject (which
    can carry more context, e.g. a parenthetical the party regex couldn't
    parse through) — union of both, deduped by (name, matter_id)."""
    found = _match_text(exception_row.get("party", ""), contacts)
    found += _match_text(exception_row.get("subject", ""), contacts)
    uniq = {(c.name, c.matter_id): c for c in found}
    return list(uniq.values())


def build_oc_op_candidates(exceptions: list[dict], contacts: list[OcOpContact]) -> list[dict]:
    """Cross-references the migration's exceptions against OC/OP contacts.
    Rows for review only — nothing here gets auto-imported. When a text
    matches more than one distinct (contact, matter) pair — e.g. the same
    contact is Opposing Counsel on both a closed and an open matter, or the
    text ambiguously matches two different people — every candidate is
    listed in `note` instead of silently picking one; a paralegal resolves it
    by hand."""
    rows: list[dict] = []
    for exc in exceptions:
        matches = match_exception_to_oc_op(exc, contacts)
        if not matches:
            continue

        primary = matches[0]
        note = ""
        if len(matches) > 1:
            alts = "; ".join(f"{c.name} — {c.matter_display_number} ({c.matter_status})" for c in matches[1:])
            note = f"Multiple possible matches: {alts}"

        rows.append({
            "subject": exc["subject"],
            "date": exc["date"],
            "role": primary.role,
            "contact": primary.name,
            "matter": primary.matter_display_number,
            "matter_status": primary.matter_status,
            "note": note,
        })
    return rows
