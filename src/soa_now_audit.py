"""
soa_now_audit.py — SoA/NoW Close Audit: verify a filed Substitution of
Attorney or Notice of Withdrawal exists before closing a specific, hand-
picked list of old matters that the Client Assignment tool surfaced as
missing a Responsible Staff assignment (Heidi Collier's book, flagged
2026-09-22, "all of these should be closed").

Standalone from Client Assignment on purpose (Ted, 2026-09-22) — this page
doesn't concern itself with attorney/staff assignment at all, only with
finding and confirming a SoA/NoW filing before a human decides to close.
Not every matter needs one (a case can close on dismissal or a flat fee,
per Ted) — this is a checklist for staff to work through by hand, not an
automated gate like /assignments' Close button. Close Case here is never
blocked on anything this module finds.

Reuses client_assignment.py's document-matching logic (SOA_NOW_PATTERN,
folder classification, naming-convention token refinement, close_matter())
rather than duplicating it — see that module for how those rules were
derived and live-validated against real matters.

Hardcoded matter list, not a live all-open-matters query (Ted, 2026-09-22:
"for now, just the hard coded list") — this may become a genuine periodic
audit later, at which point MATTER_NAMES should become a live query instead
and this whole approach revisited; not built that way yet since today's
actual goal is closing out this specific list.

"Mark SoA/NoW Filed" writes directly to Clio's own "NoW or SoA filed"
matter custom field (checkbox type) — revised 2026-09-23 (Ted: "I just want
the custom field to be marked") from an earlier version that kept its own
local SQLite table instead, which nothing outside this dashboard could see.
Clio is the source of truth for this checkbox now, same posture as
moore_marsden/clio_matter_dates.py's Date of Marriage/Separation fields —
no local copy, every page load reads the live value. A richer version (a
Clio Note citing the exact file, posted in the same click) is still
deferred, per Ted's original ask on 2026-09-22.
"""

import logging
import os
import re
from dataclasses import dataclass, field
from urllib.parse import quote

import requests

import client_assignment as ca
import matter_matching

BASE_URL = ca.BASE_URL

# The specific matters this one-time audit covers (Ted, 2026-09-22).
# CANNIZARO/COLE/COMER added 2026-09-24 — confirmed live against Clio
# before adding (CANNIZARO, KENNETH id 1795449573; COLE, DYLAN id
# 1800089868; COMER, MEGAN id 1786825578 — Ted named her "Comer" alone,
# resolved to exactly one unambiguous live match).
MATTER_NAMES = [
    "CANNIZARO, KENNETH", "COLE, DYLAN", "COMER, MEGAN",
    "COURTLAND, KELLY", "DAYBERRY, JAMES", "EVANS, KARI", "FRANK, LINDA",
    "GARCIA, LARISSA", "GOTTLIEB, ERIN", "GRANT, CAROL", "HART, CINDY",
    "HOANG, JENNIFER", "HUERTA, BRIDGET", "HUTMACHER, SARAH", "KENDRO, JILL",
    "KENNEDY, KATHERINE", "KERCKHOFF, STEPHEN", "KEYSER, JESSICA",
    "KING, SHELBY", "KOBS, MAUREEN", "KOSBAB, ALAN", "KUMLIN, CHRISTOPHER",
    "LAWLER, MARILYN", "LIEURANCE, JEREMY", "LUMM, JESSICA", "MAYHAR, RONNI",
    "MICHEL, JESSE", "MIKELS, JESSICA", "NEAL, MARIE", "NON-BILLABLE, ADMIN",
    "OBRIEN, ANDREA", "OLSON, SONJA", "PASQUALINI, ANTHONY", "PAUP, LAURA",
    "PEAVEY,", "PEDROZA, NALLELY", "PICQUELLE, KRISTA", "PRECIADO, ERICA",
    "REED, BRITTNEY", "ROBBINS, BRIAN", "RUSSETH,", "SAEZ,",
    "SANCHEZ, FRANCINE", "SATKOWIAK,", "SCIUTTO, BRIAN", "SECKER, LORRAINE",
    "SEWARD, KIMBERLEY", "SMITH, VANESSA ANN", "STEWART, CHRIS",
    "TANNER, ERICA", "TWEED, MICHAEL", "TWIDWELL, RYAN",
    "VENGOECHEA, DANIEL", "VISWANATHAN, VIDYA", "WELLS, BRITTNEY",
]

# Confirmed live 2026-09-22: `net use Y:` on the machine running this
# resolves to \\Heidiofficenas\shared — used to build a UNC-based file://
# link (works regardless of whether a given staff member's own machine
# happens to map the share to a different drive letter than Y:), alongside
# the plain Y:-based path for display/copy-paste since browser support for
# clicking file:// links to a network share is inconsistent across browser
# security policies and can't be assumed to just work.
YDRIVE_LOCAL_BASE = r"Y:\Client Files"
YDRIVE_UNC_BASE = r"\\Heidiofficenas\shared\Client Files"

# "Mark SoA/NoW Filed" writes straight to Clio's own "NoW or SoA filed"
# matter custom field (Ted, 2026-09-23: "I just want the custom field to be
# marked") — not a local table. Confirmed live 2026-09-23 this field
# already exists on real matters (checkbox type, e.g. id "checkbox-
# 1127268708" on KOBS, MAUREEN, value false) — this tool didn't invent it.
# Clio is the source of truth here, same posture as
# moore_marsden/clio_matter_dates.py's Date of Marriage/Separation fields:
# no local copy to go stale, every page load reads the live value. An
# earlier version of this page kept its own soa_now_audit_marks SQLite
# table instead — dropped same day once "mark filed" moved to Clio itself;
# a mark checked in that table was never visible outside this dashboard,
# which defeated the point once staff expected it to show in Clio directly.
SOA_NOW_FIELD_NAME = "NoW or SoA filed"


@dataclass
class AuditMatter:
    name: str
    matter_id: int | None
    status: str  # "open" | "pending" | "closed" | "not_found" | "ambiguous"
    findings: list[dict] = field(default_factory=list)
    can_close: bool = False  # informational only here — never gates Close Case
    no_documents_in_clio: bool = False
    y_drive: dict | None = None
    soa_now_marked: bool = False  # live value of the "NoW or SoA filed" custom field


_soa_now_field_id_cache: int | None = None


def _find_soa_now_field_id(session: requests.Session) -> int:
    global _soa_now_field_id_cache
    if _soa_now_field_id_cache is not None:
        return _soa_now_field_id_cache
    resp = session.get(
        f"{BASE_URL}/api/v4/custom_fields.json",
        params={"parent_type": "Matter", "query": SOA_NOW_FIELD_NAME, "fields": "id,name"},
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Failed to look up custom fields: {resp.status_code} {resp.text[:200]}")
    for cf in resp.json().get("data", []):
        if cf.get("name") == SOA_NOW_FIELD_NAME:
            _soa_now_field_id_cache = int(cf["id"])
            return _soa_now_field_id_cache
    raise RuntimeError(f"Could not find a {SOA_NOW_FIELD_NAME!r} custom field on Matter in this Clio account")


def set_soa_now_filed(session: requests.Session, matter_id: int, marked: bool) -> None:
    """PATCHes the matter's "NoW or SoA filed" checkbox custom field.
    Same "check for an existing CustomFieldValue id first" pattern as
    moore_marsden/clio_matter_dates.py's update_matter_dates() — confirmed
    live 2026-09-23 this field already has a pre-existing (value: false)
    CustomFieldValue record on real matters (e.g. "checkbox-1127268708" on
    KOBS, MAUREEN), so PATCHing with a bare custom_field{id} against a
    matter that already has one 422s with "custom field value ... already
    exists" — same gotcha that module's docstring documents in detail."""
    resp = session.get(
        f"{BASE_URL}/api/v4/matters/{matter_id}.json",
        params={"fields": "custom_field_values{id,field_name}"},
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Failed to fetch matter {matter_id}: {resp.status_code} {resp.text[:200]}")

    existing_id = None
    for cfv in resp.json()["data"].get("custom_field_values", []):
        if cfv.get("field_name") == SOA_NOW_FIELD_NAME:
            existing_id = cfv["id"]
            break

    if existing_id:
        value_entry = {"id": existing_id, "value": marked}
    else:
        value_entry = {"custom_field": {"id": _find_soa_now_field_id(session)}, "value": marked}

    patch_resp = session.patch(
        f"{BASE_URL}/api/v4/matters/{matter_id}.json",
        json={"data": {"custom_field_values": [value_entry]}},
    )
    if patch_resp.status_code != 200:
        raise RuntimeError(f"Failed to update matter {matter_id} SoA/NoW filed field: {patch_resp.status_code} {patch_resp.text[:300]}")
    logging.info("Set matter %s %r -> %s", matter_id, SOA_NOW_FIELD_NAME, marked)


def clio_folder_url(matter_id: int, folder_id: int | None) -> str:
    base = f"{BASE_URL}/nc/#/matters/{matter_id}/document_management"
    return f"{base}?folder_id={folder_id}" if folder_id else base


def y_drive_links(folder_path: str) -> dict:
    """folder_path is a local Y:\\... path. Returns both a best-effort
    file:// link (UNC-based, per this module's docstring) and the plain
    Windows path for display/copy-paste — file:// support for clicking
    into a network share varies by browser policy, so don't rely on it
    alone."""
    rel = os.path.relpath(folder_path, YDRIVE_LOCAL_BASE)
    unc_path = os.path.join(YDRIVE_UNC_BASE, rel)
    # unc_path starts with "\\host\share\..." — strip that leading "\\" (UNC
    # marker) before joining onto "file://", or the result doubles up to
    # "file:////host/..." instead of the correct "file://host/...".
    forward = unc_path.replace("\\", "/").lstrip("/")
    file_url = "file://" + quote(forward)
    return {"file_url": file_url, "display_path": folder_path}


def _norm(s: str) -> str:
    return re.sub(r"[^A-Z0-9, ]", "", s.upper().strip())


def match_matters(session: requests.Session) -> dict[str, AuditMatter]:
    """Resolves MATTER_NAMES against Clio's current matter list (any
    status — open, pending, or closed, so a matter closed since this audit
    started shows up as already closed instead of vanishing without
    explanation)."""
    raw = matter_matching.fetch_open_matters(
        session,
        fields="id,display_number,status,custom_field_values{id,field_name,value}",
        status="open,pending,closed",
    )
    by_norm: dict[str, list[dict]] = {}
    for m in raw:
        by_norm.setdefault(_norm(m.get("display_number") or ""), []).append(m)

    result: dict[str, AuditMatter] = {}
    for raw_name in MATTER_NAMES:
        key = _norm(raw_name)
        cands = by_norm.get(key, [])
        if not cands:
            last = key.split(",")[0]
            cands = [m for m in raw if _norm(m.get("display_number") or "").split(",")[0] == last]
        if not cands:
            result[raw_name] = AuditMatter(name=raw_name, matter_id=None, status="not_found")
            continue
        if len(cands) > 1:
            not_closed = [c for c in cands if (c.get("status") or "").lower() != "closed"]
            if len(not_closed) == 1:
                cands = not_closed
            else:
                result[raw_name] = AuditMatter(name=raw_name, matter_id=None, status="ambiguous")
                continue
        m = cands[0]
        soa_now_marked = False
        for cfv in m.get("custom_field_values") or []:
            if cfv.get("field_name") == SOA_NOW_FIELD_NAME:
                soa_now_marked = bool(cfv.get("value"))
                break
        result[raw_name] = AuditMatter(
            name=m.get("display_number") or raw_name,
            matter_id=int(m["id"]),
            status=(m.get("status") or "").lower(),
            soa_now_marked=soa_now_marked,
        )
    return result


_letter_cache: dict[str, list[str]] = {}


def _list_letter_dir(letter: str) -> list[str]:
    if letter not in _letter_cache:
        path = os.path.join(YDRIVE_LOCAL_BASE, letter)
        try:
            _letter_cache[letter] = [d for d in os.listdir(path) if os.path.isdir(os.path.join(path, d))]
        except OSError as e:
            logging.warning("Couldn't list Y: drive folder %s: %s", path, e)
            _letter_cache[letter] = []
    return _letter_cache[letter]


def _first_token_of_suffix(last_u: str, folder_u: str) -> str:
    if folder_u == last_u:
        return ""
    suffix = folder_u[len(last_u):].lstrip(", ")
    return suffix.split()[0] if suffix else ""


def _find_y_drive_candidate(last: str, first: str) -> tuple[str | None, list[str]]:
    """Returns (confident_folder_path_or_None, all_same_last_name_paths).
    Same exact-first-token-match-or-nothing policy live-validated while
    building the earlier one-off report: a same-last-name folder on Y: is
    common (multiple unrelated clients), so this refuses to guess a
    specific client's folder rather than risk pointing staff at the wrong
    person's documents — confirmed live this matters (4 different "OLSON"
    folders exist, none of them a specific "OLSON, SONJA")."""
    last_u = last.upper()
    letter = last_u[0] if last_u else ""
    if not letter.isalpha():
        return None, []
    subfolders = _list_letter_dir(letter)
    cands = [
        d for d in subfolders
        if d.upper() == last_u or d.upper().startswith(last_u + ",") or d.upper().startswith(last_u + " ")
    ]
    all_paths = [os.path.join(YDRIVE_LOCAL_BASE, letter, d) for d in cands]

    if len(cands) == 1:
        return all_paths[0], all_paths

    target = first.split()[0].upper() if first else ""
    if not target:
        return None, all_paths
    exact = [d for d in cands if _first_token_of_suffix(last_u, d.upper()) == target]
    if len(exact) == 1:
        return os.path.join(YDRIVE_LOCAL_BASE, letter, exact[0]), all_paths
    return None, all_paths


def _classify_y_drive_path(rel_path: str) -> str:
    """Same folder-name patterns as Clio's classification (no formal
    naming-convention *token* parsing — CL/OP/Conf/Exec — since Ted was
    explicit these legacy files predate that convention entirely),
    including the same "filed" (Conformed Copies — the court-stamped copy)
    vs. "prepared" (bare Pleadings/Our Pleadings — drafted/lodged, not
    confirmed the court has it) split added 2026-09-24 after Ted called
    out that a bare-Pleadings match alone isn't evidence of actual court
    filing. The plain English word "filed" in the filename itself (Ted,
    same day: "filed forms could also have the word filed in the name" —
    real examples confirmed on this account: "NOW filed 4.18.23.pdf", "NOW
    FILED 12.22.22.pdf") upgrades a "prepared" match to "filed" — this
    isn't the formal convention either, just someone writing what
    happened, but it's real evidence and these legacy files are exactly
    where it shows up."""
    filename = rel_path.replace("/", "\\").split("\\")[-1]
    parts = rel_path.replace("/", "\\").split("\\")[:-1]
    if any(ca.OPPOSING_NAME_PATTERN.match(p) for p in parts):
        return "opposing"
    if any(ca.CONFORMED_NAME_PATTERN.search(p) for p in parts):
        return "filed"
    if any(ca.FILED_NAME_PATTERN.match(p) for p in parts):
        return "filed" if ca.FILED_WORD_PATTERN.search(filename) else "prepared"
    if any(ca.CORRESPONDENCE_NAME_PATTERN.match(p) for p in parts):
        return "correspondence"
    return "other"


def search_y_drive(display_name: str) -> dict:
    """Best-effort — a Y: drive read failure (share offline, permissions)
    logs and reports "no_folder" rather than raising, since this whole
    check is advisory and one matter's filesystem hiccup shouldn't break
    the page for the other 40+ matters."""
    parts = display_name.split(",", 1)
    last = parts[0].strip()
    first = parts[1].strip() if len(parts) > 1 else ""
    try:
        folder, all_same = _find_y_drive_candidate(last, first)
    except OSError as e:
        logging.warning("Y: drive search failed for %r: %s", display_name, e)
        return {"status": "no_folder", "folder": None, "findings": [], "has_filed": False, "other_folders": []}

    if folder is None:
        status = "ambiguous" if len(all_same) > 1 else "no_folder"
        return {
            "status": status, "folder": None, "findings": [], "has_filed": False,
            "other_folders": [os.path.basename(p) for p in all_same],
        }

    findings = []
    try:
        for root, _dirs, files in os.walk(folder):
            for fn in files:
                if ca.SOA_NOW_PATTERN.search(fn):
                    rel = os.path.relpath(os.path.join(root, fn), folder)
                    rel_dir = rel.rsplit(os.sep, 1)[0] if os.sep in rel else "(client folder root)"
                    findings.append({
                        "name": fn,
                        "path": rel_dir,
                        "classification": _classify_y_drive_path(rel),
                        # Enclosing-folder link for this specific file (Ted,
                        # 2026-09-22: "a link to open each enclosing folder
                        # for any document found") — the folder containing
                        # it, not the client's whole root.
                        "links": y_drive_links(os.path.dirname(os.path.join(root, fn))),
                    })
    except OSError as e:
        logging.warning("Error walking Y: drive folder %s: %s", folder, e)

    return {
        "status": "found",
        "folder": folder,
        # Client's own root folder link — shown regardless of whether a
        # specific SoA/NoW file turned up, so staff can browse by hand
        # (Ted: "if no documents found can we open up the root file of the
        # client on the Y drive").
        "folder_links": y_drive_links(folder),
        "findings": findings,
        "has_filed": any(f["classification"] == "filed" for f in findings),
        "other_folders": [],
    }


def run_audit(session: requests.Session) -> list[AuditMatter]:
    """Runs the SoA/NoW document check (Clio first, then Y: drive only if
    Clio has nothing) for every matched matter that isn't already closed.
    Slow — a couple of Clio API calls per matter plus a filesystem walk —
    expect roughly a minute for the full list; there's no caching yet,
    every page load re-runs it from scratch (acceptable for now — Ted's
    priority is working through this specific list, not building out
    caching for a one-time exercise)."""
    matched = match_matters(session)
    for m in matched.values():
        if m.status == "closed" or m.matter_id is None:
            continue
        findings = ca.find_soa_now_documents(session, m.matter_id)
        readiness = ca.evaluate_close_readiness(findings)
        m.findings = findings
        m.can_close = readiness["can_close"]
        if not findings:
            m.no_documents_in_clio = not ca.matter_has_any_documents(session, m.matter_id)
            m.y_drive = search_y_drive(m.name)
    return list(matched.values())
