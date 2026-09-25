"""
soa_now_audit.py — SoA/NoW Close Audit: scans every currently open (or
pending) matter for a filed Substitution of Attorney or Notice of
Withdrawal, and surfaces the ones that already have one — a real, direct
signal that the matter is probably done and was never closed.

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

**Rewritten 2026-09-24 from a hardcoded ~55-matter list to a live query
across every open matter** — the original version only checked a list
Client Assignment's "missing Responsible Staff" heuristic had surfaced.
Before replacing it, Ted asked to validate the idea first with a one-off
scratch scan rather than build speculatively (see
reference/client-assignment.md's "Reverse scan" note for the numbers):
scanning all 175 open matters (not the hand-picked list) found 27 already
had a genuinely filed SoA/NoW — and 26 of those 27 were never on the
hand-picked list at all, meaning "missing staff" and "has a filed SoA/NoW"
are largely different matter populations. That result is what justified
this rewrite.

**No Y: drive fallback anymore (Ted, 2026-09-24): "nothing new should
reference the Y drive... a paralegal who has something assigned will find
it in Clio, or we will bring forward legacy data and use Clio from then
on."** The earlier hardcoded-list version fell back to searching the
legacy Y: drive when Clio had nothing — that made sense for a fixed list
of old, possibly-never-migrated matters, but doesn't fit a live, ongoing
audit of the current caseload, which is expected to live in Clio going
forward. A matter with nothing found here just means nothing found,
full stop.

A full scan takes real time — a couple of Clio API calls per open matter,
no artificial delay between them (retries reactively on a 429 instead,
see this project's own `_get_with_retry()` pattern — this is now a
cross-project convention, see the api-rate-limit-handling memory in the
`~/projects/` hub) — so it's triggered explicitly
(`POST /soa-now-audit/run`, see routes_soa_now_audit.py) rather than
recomputed on every page view. Results are cached in-memory between runs
(same "in-memory handoff" pattern this app already uses for
preview_store.py's dry-run previews) — there is deliberately no fancy
progress UI yet (Ted, 2026-09-24, declined it for now); the trigger just
blocks until the whole scan finishes.

"Mark SoA/NoW Filed" writes directly to Clio's own "NoW or SoA filed"
matter custom field (checkbox type) — Clio is the source of truth (no
local copy), same posture as moore_marsden/clio_matter_dates.py's Date of
Marriage/Separation fields. A richer version (a Clio Note citing the
exact file, posted in the same click) is still deferred, per Ted's
original ask on 2026-09-22.
"""

import logging
import re

import requests

import client_assignment as ca
import matter_matching

BASE_URL = ca.BASE_URL

# Display filter, added 2026-09-24 (Ted: "There is no reason to flag the
# case or display it if nothing is found. Also, if you see OP, their
# pleadings, but nothing for us, then there is also no reason to display
# it. The only conditions to display will be ones the reflect a
# sign/conformed/conf in the file or directory"). Deliberately separate
# from client_assignment.py's classification/close-gate logic — a matter
# still gets scanned and classified exactly as before (that's what
# evaluate_close_readiness()/can_close still reflect, unchanged, since
# other code — e.g. /assignments' own Close gate — depends on that being
# consistent), this only controls what's worth putting in front of a human
# on THIS page. Narrower than "filed" on purpose: "filed" also counts a
# plain-English "filed" word match (client_assignment.FILED_WORD_PATTERN),
# which Ted did not name here — only sign(ed)/conformed/conf.
DISPLAY_SIGNAL_PATTERN = re.compile(r"\bsigned?\b|\bconformed\b|\bconf\b", re.IGNORECASE)

# "Mark SoA/NoW Filed" writes straight to Clio's own "NoW or SoA filed"
# matter custom field. Confirmed live 2026-09-23 this field already
# exists on real matters (checkbox type, e.g. id "checkbox-1127268708" on
# KOBS, MAUREEN, value false) — this tool didn't invent it.
SOA_NOW_FIELD_NAME = "NoW or SoA filed"

# Fields pulled for every open matter in one batched call — status +
# custom_field_values so the "NoW or SoA filed" checkbox's live value
# comes back for free alongside the matter list itself, no per-matter
# extra request needed just to know if it's already marked.
MATTER_FIELDS = "id,display_number,status,custom_field_values{id,field_name,value}"


class AuditMatter:
    __slots__ = ("name", "matter_id", "status", "findings", "can_close", "soa_now_marked")

    def __init__(self, name: str, matter_id: int, status: str, soa_now_marked: bool):
        self.name = name
        self.matter_id = matter_id
        self.status = status  # "open" | "pending" — live query never returns anything else
        self.findings: list[dict] = []
        self.can_close = False  # informational only here — never gates Close Case
        self.soa_now_marked = soa_now_marked


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


def _has_display_signal(findings: list[dict]) -> bool:
    """True if at least one non-"opposing" finding's name or path shows a
    sign/conformed/conf signal — see DISPLAY_SIGNAL_PATTERN above. An
    "opposing" finding (OP's own pleadings) never counts even if its name
    happens to contain one of these words — Ted: "if you see OP, their
    pleadings, but nothing for us, then there is also no reason to display
    it.\""""
    for f in findings:
        if f.get("classification") == "opposing":
            continue
        haystack = f"{f.get('name', '')} {f.get('path', '')}"
        if DISPLAY_SIGNAL_PATTERN.search(haystack):
            return True
    return False


def filter_for_display(matters: list[AuditMatter]) -> list[AuditMatter]:
    """Narrows a full run_audit() result down to matters actually worth
    showing a human. run_audit() itself stays unfiltered (every open/
    pending matter, whatever it found or didn't) so callers can still
    report a true "N matters scanned" count — this is a separate step so
    that count isn't lost. See DISPLAY_SIGNAL_PATTERN's comment for the
    2026-09-24 rationale."""
    return [m for m in matters if _has_display_signal(m.findings)]


def clio_folder_url(matter_id: int, folder_id: int | None) -> str:
    base = f"{BASE_URL}/nc/#/matters/{matter_id}/document_management"
    return f"{base}?folder_id={folder_id}" if folder_id else base


def fetch_open_matters_for_audit(session: requests.Session) -> list[AuditMatter]:
    """Every open or pending matter, minus the same test/internal
    exclusions Client Assignment applies (client_assignment.EXCLUDED_MATTER_NAMES
    — DOE, JANE and NON-BILLABLE, ADMIN are never real client work)."""
    raw = matter_matching.fetch_open_matters(session, fields=MATTER_FIELDS, status="open,pending")
    result = []
    for m in raw:
        display_number = m.get("display_number") or ""
        if display_number.strip().upper() in ca.EXCLUDED_MATTER_NAMES:
            continue
        soa_now_marked = False
        for cfv in m.get("custom_field_values") or []:
            if cfv.get("field_name") == SOA_NOW_FIELD_NAME:
                soa_now_marked = bool(cfv.get("value"))
                break
        result.append(AuditMatter(
            name=display_number,
            matter_id=int(m["id"]),
            status=(m.get("status") or "").lower(),
            soa_now_marked=soa_now_marked,
        ))
    return result


def run_audit(session: requests.Session) -> list[AuditMatter]:
    """Runs the SoA/NoW document check (Clio only — see module docstring
    on why the Y: drive fallback was dropped) for every currently open or
    pending matter. Slow — a couple of Clio API calls per matter, no
    artificial delay between them (relies entirely on find_soa_now_documents()'s
    own reactive 429 retry, never throttles preemptively) — expect several
    minutes for the full caseload. Meant to be triggered explicitly and
    cached by the caller (see routes_soa_now_audit.py), not re-run on
    every page view."""
    matters = fetch_open_matters_for_audit(session)
    for m in matters:
        findings = ca.find_soa_now_documents(session, m.matter_id)
        readiness = ca.evaluate_close_readiness(findings)
        m.findings = findings
        m.can_close = readiness["can_close"]
    return matters
