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
import csv
import logging
import os
import re
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


def _get_with_retry(session: requests.Session, url: str, params: dict | None = None) -> requests.Response:
    """GET with the same 429 retry/backoff this module already used for
    update_matter_field's PATCH (RETRY_DELAYS) — added 2026-09-22 after the
    close-matter folder/document scan (_fetch_matter_folder_tree(),
    find_soa_now_documents(), matter_has_any_documents()) hit real 429s
    live running a ~50-matter bulk report: those GETs had no retry at all,
    which is a gap against this project's own stated safety rule ("Retry on
    rate limit (429)") — every other write path in this module already
    retries, the reads just hadn't needed it until a bulk run made enough
    calls in a row to hit Clio's rate limit. Does NOT raise on a
    non-2xx/429 status — callers still check resp.status_code themselves,
    same as before this helper existed."""
    for attempt, delay in enumerate([0, *RETRY_DELAYS], start=1):
        if delay:
            logging.warning("Rate limited on %s — waiting %ds (attempt %d)", url, delay, attempt)
            time.sleep(delay)
        resp = session.get(url, params=params)
        if resp.status_code != 429:
            return resp
    return resp

# Explicit roster (Ted, 2026-09-02) — edit here, not by changing the
# subscription_type filter, if the assignable list ever changes.
ATTORNEY_NAMES = ["Heidi Collier", "Dahann Bowers", "Pamela Bradford"]
PARALEGAL_NAMES = ["Misty Sherman", "Patricia Payne", "Sandy Cressey"]

ASSIGNMENT_FIELDS = ("originating_attorney", "responsible_attorney", "responsible_staff")
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
    other monitor/report in this repo uses. Sorted alphabetically by matter
    (display_number is "LAST, FIRST", so this sorts by last name) — changed
    2026-09-16 (Ted) from an earlier most-incomplete-first grouping, which
    visually scattered same-client matters (e.g. two "COLTON, ANN" matters
    landing in different missing-count groups) instead of keeping the list
    in a single predictable last-name order."""
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
    result.sort(key=lambda x: x.display_number)
    return result


def write_report_csv(matters: list[MatterAssignment], path: Path) -> None:
    """CSV mirroring client_assignment_report.html's own table exactly — same
    four columns, same order, no client_name (the printed report leaves it
    off too — see that template's own note) and no raw id columns (not shown
    on screen either)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Matter", "Originating Attorney", "Responsible Attorney", "Responsible Staff"])
        for m in matters:
            writer.writerow([
                m.display_number,
                m.originating_attorney_name or "—",
                m.responsible_attorney_name or "—",
                m.responsible_staff_name or "—",
            ])


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


# Close-matter SoA/NoW gate (Ted, 2026-09-21, revised same day to a hard
# gate): a matter may only be closed from this page once a Substitution of
# Attorney or Notice of Withdrawal is found filed (Pleadings/Conformed
# Copies) — that's the filed copy. A match in Correspondence only means a
# draft exists but hasn't been filed yet, which blocks the close with a
# "needs to be filed" message rather than letting it through; no match
# anywhere blocks it too, with a plain "not found" message. This is still
# just a filename search (not proof of anything), but a match specifically
# in a filed-type folder is a hard precondition, not merely advisory text
# before a human decides.
#
# Rewritten 2026-09-22 (Ted asked for a bulk report across ~50 real
# matters, which is what surfaced this): the original version matched
# folder names exactly ("Correspondence", "Pleadings", "Conformed Copies")
# against only each matter's TOP-LEVEL folders. Live-checked against real
# matters and found this was badly wrong on both axes:
#   1. Exact-name matching missed real variants in active use: "OUR
#      PLEADINGS" (not "Pleadings"), "CORRESONDENCE" (typo, missing a 'p'),
#      "OP RFO 2021"/"OP RFO 2023" (Opposing Party — same "OP" as
#      equalizer/clio_parties.py's OC/OP relationship lookup), and
#      critically "THEIR PLEADINGS AND CORRESPONDENCE" — an opposing-party
#      folder that a plain substring match on "pleadings" would have
#      wrongly counted as OUR filed copy.
#   2. Top-level-only missed real nesting: GARCIA, LARISSA has a
#      "CONFORMED COPIES" folder nested *inside* "OUR PLEADINGS" (not a
#      sibling), and a second, entirely separate "PLEADINGS" folder nested
#      under a "DCSS" folder (a sub-matter for a specific agency), several
#      levels deep from the matter root.
# Fixed by fetching the matter's ENTIRE folder tree in one call (Clio
# returns it flat with parent links when only matter_id is given — no
# scope/parent_id needed) and classifying every ancestor of a matching
# document by name pattern, not just its immediate top-level folder. See
# _classify_folder_path() below.
FILED_NAME_PATTERN = re.compile(r"^(our\s+)?pleadings\b|^conformed\s+copies\b", re.IGNORECASE)
CORRESPONDENCE_NAME_PATTERN = re.compile(r"^corr", re.IGNORECASE)  # loose prefix, not gate-determining — also catches the "CORRESONDENCE" typo seen live
OPPOSING_NAME_PATTERN = re.compile(r"^their\b|^op\b|^opposing\b", re.IGNORECASE)

# Widened 2026-09-21 (Ted: "additional terms to consider sub of atty or the
# full term for both") — real filenames use the abbreviation ("SOA", "NOW"),
# a shorthand phrase ("Sub of Atty"/"Sub. of Atty."), or the full term
# ("Substitution of Attorney"/"Notice of Withdrawal"), not consistently one
# or the other. \b-wrapped for the two/three-letter abbreviations so they
# don't match inside ordinary words ("know", "renowned"); the phrases don't
# need that guard, they're distinctive enough on their own. The gap between
# words in a phrase is [\s_-]+, not a literal space — filenames commonly
# separate words with underscores or hyphens instead of spaces.
SOA_NOW_PATTERN = re.compile(
    r"\b(soa|now)\b"
    r"|sub\.?[\s_-]+of[\s_-]+atty\.?"
    r"|substitution[\s_-]+of[\s_-]+attorney"
    r"|notice[\s_-]+of[\s_-]+withdrawal",
    re.IGNORECASE,
)

# Firm file-naming convention (Ted, 2026-09-22): "YY.MM.DD Party.DocType.
# Description.Status", e.g. "25.07.15 CL.SOA.Conf.pdf" — a staff-written
# filename, when it follows this convention, states BOTH whose document it
# is (CL/OP/OC/CT/AT/TP) and its filing status (Exec=signed only,
# Conf=confirmed/filed, Rec=received only, Draft) directly in the name,
# independent of which folder it happens to sit in. This matters for the
# close gate because folder placement alone can be wrong: live-checked
# 2026-09-22 on HOANG, JENNIFER, "26.01.06 OP.SOA.Exec.pdf" sits in a plain
# "PLEADINGS" folder (which _classify_folder_path() alone would call
# "filed") but its own filename says it's the OPPOSING party's copy and
# only Exec (signed, not filed) — this matter isn't split into separate
# our-side/their-side pleadings subfolders, so folder alone can't tell them
# apart. Parsed as a refinement on top of the folder classification, never
# used alone (plenty of real filenames, especially older or non-firm-
# generated ones like raw Judicial Council form names, don't follow this
# convention at all and have no tokens to find) — see
# _refine_classification().
_NAME_TOKEN_SPLIT = re.compile(r"[\s._-]+")
PARTY_TOKENS = {"CL", "OP", "OC", "CT", "AT", "TP"}
STATUS_TOKENS = {"EXEC", "CONF", "REC", "DRAFT"}


def _parse_naming_convention_tokens(name: str) -> tuple[str | None, str | None]:
    """Returns (party_token, status_token) found in `name` as whole tokens
    (split on whitespace/period/underscore/hyphen, matched case-
    insensitively against the firm's abbreviation lists) — None for either
    if the filename doesn't contain one. Only the first match of each kind
    is used; a filename with more than one is unusual enough not to worry
    about."""
    tokens = [t for t in _NAME_TOKEN_SPLIT.split(name) if t]
    party = next((t.upper() for t in tokens if t.upper() in PARTY_TOKENS), None)
    status = next((t.upper() for t in tokens if t.upper() in STATUS_TOKENS), None)
    return party, status


def _refine_classification(folder_classification: str, party_token: str | None, status_token: str | None) -> str:
    """Combines the folder-based classification with the filename's own
    party/status tokens (see _parse_naming_convention_tokens()) — the
    filename can only ever downgrade a "filed" folder classification, never
    upgrade a non-filed one, since an unrecognized or absent token means
    "the filename doesn't say," not "the filename confirms it's ours and
    filed." Precedence: an explicit OP token always wins (opposing,
    regardless of folder or status); then an explicit non-Conf status
    (Exec/Rec/Draft) downgrades an otherwise-"filed" folder classification
    to "unfiled" — the document itself is saying it isn't filed yet, no
    matter which folder it's sitting in."""
    if party_token == "OP":
        return "opposing"
    if folder_classification == "filed" and status_token in {"EXEC", "REC", "DRAFT"}:
        return "unfiled"
    return folder_classification


def _fetch_matter_folder_tree(session: requests.Session, matter_id: int) -> dict[int, dict]:
    """Returns {folder_id: {"name": ..., "parent_id": ...}} for every folder
    in the matter, root through however many levels deep — one call (plus
    pagination) with just matter_id, no parent_id/scope, returns the whole
    tree flat with parent links, confirmed live 2026-09-22 (GARCIA, LARISSA
    has real folders 4 levels deep and this pulled all of them in one
    page)."""
    tree: dict[int, dict] = {}
    next_url: str | None = None
    params = {"matter_id": matter_id, "fields": "id,name,parent{id}", "limit": 200}
    folders_endpoint = f"{BASE_URL}/api/v4/folders.json"
    while True:
        resp = _get_with_retry(session, next_url) if next_url else _get_with_retry(session, folders_endpoint, params=params)
        if resp.status_code != 200:
            raise RuntimeError(f"Failed to fetch folder tree for matter {matter_id}: {resp.status_code} {resp.text[:200]}")
        body = resp.json()
        for f in body.get("data", []):
            parent = f.get("parent") or {}
            tree[f["id"]] = {"name": f.get("name") or "", "parent_id": parent.get("id")}
        next_url = body.get("meta", {}).get("paging", {}).get("next")
        if not next_url:
            break
    return tree


def _classify_folder_path(tree: dict[int, dict], parent_id: int | None) -> tuple[str, str]:
    """Walks a document's containing folder up through the matter's folder
    tree to the matter root, and returns (classification, path) —
    classification is "filed" (counts toward the close gate), "opposing"
    (an OP/THEIR folder — explicitly never filed, checked ahead of
    "filed" in case a filed-looking name shows up nested under an
    opposing-party folder), "correspondence" (drafted, not yet filed), or
    "other" (matched SOA_NOW_PATTERN somewhere not recognized as either —
    still worth showing a human, just doesn't satisfy the gate). `path` is
    the folder breadcrumb from (just below the matter root) down to the
    document's immediate folder, e.g. "OUR PLEADINGS > CONFORMED COPIES" —
    the matter's own root folder (named after the matter itself) is
    dropped from the breadcrumb since it's redundant."""
    names: list[str] = []
    seen: set[int] = set()
    current = parent_id
    while current is not None and current in tree and current not in seen:
        seen.add(current)
        node = tree[current]
        names.append(node["name"])
        current = node["parent_id"]
    # names is nearest-parent-first; matter's own root folder is always the
    # last one walked (its parent isn't in `tree`) — drop it from the path.
    if names:
        names.pop()
    path = " > ".join(reversed(names)) or "(matter root)"

    if any(OPPOSING_NAME_PATTERN.match(n) for n in names):
        return "opposing", path
    if any(FILED_NAME_PATTERN.match(n) for n in names):
        return "filed", path
    if any(CORRESPONDENCE_NAME_PATTERN.match(n) for n in names):
        return "correspondence", path
    return "other", path


def find_soa_now_documents(session: requests.Session, matter_id: int) -> list[dict]:
    """Searches every document in the matter (any folder, any depth) for a
    name matching SOA_NOW_PATTERN, classifies each match by walking its
    folder ancestry (_classify_folder_path()), then refines that with
    whatever party/status tokens the filename itself carries per the firm's
    naming convention (_refine_classification()). Returns a list of
    {"name", "path", "folder_id", "classification": "filed"|"opposing"|
    "unfiled"|"correspondence"|"other", "folder_classification",
    "party_token", "status_token"} — `folder_id` is the document's
    immediate containing folder (for building a direct
    `document_management?folder_id=...` link — see soa_now_audit.py); the
    other extra fields are kept alongside the final `classification` so a
    human (or a report) can see *why* it landed where it did, not just the
    end result."""
    tree = _fetch_matter_folder_tree(session, matter_id)

    findings: list[dict] = []
    next_url: str | None = None
    params = {"matter_id": matter_id, "fields": "id,name,parent{id}", "limit": 200}
    documents_endpoint = f"{BASE_URL}/api/v4/documents.json"
    while True:
        resp = _get_with_retry(session, next_url) if next_url else _get_with_retry(session, documents_endpoint, params=params)
        if resp.status_code != 200:
            raise RuntimeError(f"Failed to search documents on matter {matter_id}: {resp.status_code} {resp.text[:200]}")
        body = resp.json()
        for doc in body.get("data", []):
            name = doc.get("name") or ""
            if not SOA_NOW_PATTERN.search(name):
                continue
            parent = doc.get("parent") or {}
            folder_classification, path = _classify_folder_path(tree, parent.get("id"))
            party_token, status_token = _parse_naming_convention_tokens(name)
            classification = _refine_classification(folder_classification, party_token, status_token)
            findings.append({
                "name": name,
                "path": path,
                "folder_id": parent.get("id"),
                "classification": classification,
                "folder_classification": folder_classification,
                "party_token": party_token,
                "status_token": status_token,
            })
        next_url = body.get("meta", {}).get("paging", {}).get("next")
        if not next_url:
            break

    return findings


def matter_has_any_documents(session: requests.Session, matter_id: int) -> bool:
    """True if the matter has ANY document in Clio at all, regardless of
    folder. Used only when find_soa_now_documents() comes back empty, to
    tell apart two very different situations that would otherwise look
    identical to the SoA/NoW check: "this matter's Clio documents just don't
    happen to include a SoA/NoW" vs. "this matter has nothing in Clio's
    Documents at all" — the second case is a real, live pattern at this
    firm for older matters whose files still live on the legacy Y: drive
    and were never migrated in, and staff should be pointed there instead
    of concluding no SoA/NoW exists anywhere."""
    resp = _get_with_retry(session, f"{BASE_URL}/api/v4/documents.json", params={"matter_id": matter_id, "limit": 1, "fields": "id"})
    if resp.status_code != 200:
        raise RuntimeError(f"Failed to check for any documents on matter {matter_id}: {resp.status_code} {resp.text[:200]}")
    return bool(resp.json().get("data"))


def evaluate_close_readiness(findings: list[dict]) -> dict:
    """Turns find_soa_now_documents()'s raw findings into the actual close
    gate: closeable only if at least one match classified "filed" — a
    match classified "correspondence" (drafted, not yet filed), "opposing"
    (the other side's copy, never counts as ours), "unfiled" (sitting in a
    filed-type folder, but the filename's own status token says Exec/Rec/
    Draft, not Conf — see _refine_classification()), "other" (matched
    somewhere not recognized as either), or no match at all all block the
    close, just with different reasons for the UI to explain to the person
    clicking Close."""
    filed = [f for f in findings if f["classification"] == "filed"]
    unfiled = [f for f in findings if f["classification"] != "filed"]
    return {
        "can_close": bool(filed),
        "filed": filed,
        "unfiled": unfiled,
    }


def close_matter(session: requests.Session, matter_id: int) -> str:
    """PATCH the matter's status to closed. Returns the status Clio confirms
    back. No un-close path here (same "never clear/undo through this tool"
    posture as update_matter_field) — reopening a matter is a Clio-UI action."""
    url = f"{BASE_URL}/api/v4/matters/{matter_id}.json"
    body = {"data": {"status": "closed"}}
    params = {"fields": "id,status"}
    logging.info("PATCH matter %s status -> closed", matter_id)
    resp = session.patch(url, json=body, params=params)
    if resp.status_code != 200:
        logging.error("Failed to close matter %s: %s %s", matter_id, resp.status_code, resp.text[:300])
        raise RuntimeError(f"Failed to close matter {matter_id}: {resp.status_code} {resp.text[:300]}")
    status = (resp.json().get("data") or {}).get("status", "")
    logging.info("Closed matter %s -> status %s", matter_id, status)
    return status


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
