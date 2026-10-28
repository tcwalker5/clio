"""
soa_now_audit.py — Case Closing (formerly "SoA/NoW Close Audit"): scans
every currently open (or pending) matter for a filed Substitution of
Attorney, Notice of Withdrawal, or Request for Dismissal, and surfaces the
ones that already have one — a real, direct signal that the matter is
probably done and was never closed. Also lists closed matters whose final
bill is settled but still hold trust funds, so the refund gets processed
(fetch_refund_candidates()).

**Dismissal + trust refunds added 2026-10-14 (Ted: "a case can be closed if
we have the NoW or SoA, or it is dismissed... if there are trust funds
available to refund after a final bill, flag that so I can process the
refund").** See DISMISSAL_PATTERN and fetch_refund_candidates() below.

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
recomputed on every page view. Since 2026-10-14 results are saved in
data/clio_dashboard.db and a normal run only re-scans matters whose
documents changed (see "Incremental scan + persistence" below) — before
that they lived in an in-memory cache lost on every dashboard restart. There is deliberately no fancy
progress UI yet (Ted, 2026-09-24, declined it for now); the trigger just
blocks until the whole scan finishes.

"Mark SoA/NoW Filed" writes directly to Clio's own "NoW or SoA filed"
matter custom field (checkbox type) — Clio is the source of truth (no
local copy), same posture as moore_marsden/clio_matter_dates.py's Date of
Marriage/Separation fields. A richer version (a Clio Note citing the
exact file, posted in the same click) is still deferred, per Ted's
original ask on 2026-09-22.
"""

import json
import logging
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

import requests

import client_assignment as ca
import matter_matching
import trust_monitor
from staff_unbilled_monitor import fetch_client_trust_balances

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

# Request for Dismissal (Judicial Council CIV-110) — the document that
# actually ends a case. Derived from a read-only scratch scan of every
# open/closed matter (2026-10-14, 267 matters, 65 filenames containing
# "dismiss"): the bare word is far too broad — most hits were PARTIAL
# dismissals that don't end the case (a TRO/DVTRO/DVRO or one RFO
# dismissed, a 401K joinder dismissed) or court notices of a 5-/3-year
# dismissal deadline. Real case-ending filenames seen: "26.07.21 Request for
# Dismissal.Conf.pdf", "26.09.04 Req for Dismissal Conf. - Sabbara.pdf",
# "Original - CIV_110_Request_for_Dismissal.pdf".
DISMISSAL_PATTERN = re.compile(
    r"\breq(uest)?\.?[\s_-]+(for[\s_-]+)?dismissal"
    r"|\bciv[\s_-]*110\b",
    re.IGNORECASE,
)
PARTIAL_DISMISSAL_PATTERN = re.compile(
    r"\b(dv)?tro\b|\bdvro\b|\brfo\b|joinder|401k"
    r"|\b\d+[\s-]*year\b|\b(three|five)[\s-]+year\b",
    re.IGNORECASE,
)
# One documents pass per matter covers both kinds — a full scan is already
# minutes long under Clio's rate limit, and fetching every matter's
# documents twice would double it. Each finding is tagged with its kind
# afterward (_finding_kind()).
CLOSING_DOC_PATTERN = re.compile(
    f"{ca.SOA_NOW_PATTERN.pattern}|{DISMISSAL_PATTERN.pattern}", re.IGNORECASE
)


# SoA = "out" only (Ted, 2026-10-14: "SOA is almost always in, so we can
# ignore it. We only need to find SOA.out (case does not matter)"). A
# Substitution of Attorney usually means we came IN on the case — the
# opposite of a closing signal (real false positives from the first live
# run: CASTRUITA, ESTER and FULMER, VERNON, both active). Matched as the
# word "out" anywhere in an SoA filename, not only a literal "SOA.out",
# because real filenames also say e.g. "Sub of Atty Glover out 4.2025.pdf";
# \b keeps the MC-050 form title's "(Without Court Order)" from counting.
# A Notice of Withdrawal is always us going out, so it needs no such check.
NOW_PATTERN = re.compile(r"\bnow\b|notice[\s_-]+of[\s_-]+withdrawal", re.IGNORECASE)
SOA_OUT_PATTERN = re.compile(r"\bout\b", re.IGNORECASE)


def _finding_kind(name: str) -> str | None:
    """"soa_now" | "dismissal" | None (an SoA without "out", or a partial
    dismissal — dropped)."""
    if NOW_PATTERN.search(name):
        return "soa_now"
    if ca.SOA_NOW_PATTERN.search(name):
        return "soa_now" if SOA_OUT_PATTERN.search(name) else None
    if DISMISSAL_PATTERN.search(name) and not PARTIAL_DISMISSAL_PATTERN.search(name):
        return "dismissal"
    return None

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

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__slots__}

    @classmethod
    def from_dict(cls, d: dict) -> "AuditMatter":
        m = cls(d["name"], d["matter_id"], d["status"], d["soa_now_marked"])
        m.findings = d["findings"]
        m.can_close = d["can_close"]
        return m


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


# ---------------------------------------------------------------------------
# Incremental scan + persistence (added 2026-10-14, Ted: "would it speed up
# the audit by having a cached version on disk?"). A full scan is ~2 Clio
# calls per open matter and takes ~10 minutes under the rate limit. Each
# matter's findings are now saved; a normal run asks Clio which matters had
# a document or folder change since the last run (firm-wide
# documents/folders.json?updated_since= — one paged call each, ~1s,
# confirmed live 2026-10-14 to return only genuinely-changed items, folders
# included since a folder rename changes a document's classification) and
# re-scans only those, plus any matter with no saved result or one saved
# under older matching rules. Everything else reuses its saved findings.
# Results and run history live in data/clio_dashboard.db, so the page
# survives a dashboard restart (the old in-memory cache didn't).
#
# Known gap: a document DELETED from Clio doesn't show up as "updated", so
# its saved finding would linger until that matter changes again — "Full
# rescan" on the page clears that up.
# ---------------------------------------------------------------------------

# Bump whenever matching/classification changes (patterns above, or
# client_assignment's folder/token rules) — any saved result from an older
# version is re-scanned on the next run instead of trusted.
SCAN_RULES_VERSION = 3  # 1 = SoA/NoW only (pre-2026-10-14), 2 = + Request for Dismissal, 3 = SoA counts only if "out"

# Look back a little before the previous run's start, so a document changed
# while that run was in progress (or a small clock skew) isn't missed.
CHANGE_CURSOR_OVERLAP = timedelta(minutes=10)

SCHEMA = """
CREATE TABLE IF NOT EXISTS case_closing_matter_scans (
    matter_id INTEGER PRIMARY KEY,
    findings_json TEXT NOT NULL,
    can_close INTEGER NOT NULL,
    rules_version INTEGER NOT NULL,
    scanned_at TEXT NOT NULL
);

-- One row per Run Audit / Check Refunds. results_json is exactly what the
-- page displays; an audit run's started_at (UTC) is the next run's change
-- cursor.
CREATE TABLE IF NOT EXISTS case_closing_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    full_scan INTEGER NOT NULL DEFAULT 0,
    scanned_count INTEGER,
    rescanned_count INTEGER,
    results_json TEXT NOT NULL
);
"""
SCHEMA_COLUMNS: list[tuple[str, str, str]] = []

RUN_KIND_AUDIT = "audit"
RUN_KIND_REFUNDS = "refunds"


def fetch_changed_matter_ids(session: requests.Session, since: datetime) -> set[int]:
    """Matter ids with any document or folder created/updated since `since`."""
    changed: set[int] = set()
    for resource in ("documents", "folders"):
        url = f"{BASE_URL}/api/v4/{resource}.json"
        params = {"updated_since": since.isoformat(), "fields": "id,matter{id}", "limit": 200}
        while url:
            resp = ca._get_with_retry(session, url, params=params)
            if resp.status_code != 200:
                raise RuntimeError(f"Failed to fetch changed {resource}: {resp.status_code} {resp.text[:200]}")
            body = resp.json()
            for item in body.get("data", []):
                mid = (item.get("matter") or {}).get("id")
                if mid:
                    changed.add(int(mid))
            url, params = body.get("meta", {}).get("paging", {}).get("next"), None
    return changed


def _scan_matter(session: requests.Session, m: AuditMatter) -> None:
    findings = []
    for f in ca.find_soa_now_documents(session, m.matter_id, pattern=CLOSING_DOC_PATTERN):
        f["kind"] = _finding_kind(f["name"])
        if f["kind"]:
            findings.append(f)
    m.findings = findings
    m.can_close = ca.evaluate_close_readiness(findings)["can_close"]


def latest_run(conn, kind: str):
    return conn.execute(
        "SELECT * FROM case_closing_runs WHERE kind = ? ORDER BY id DESC LIMIT 1", (kind,)
    ).fetchone()


def run_audit(session: requests.Session, conn, full: bool = False) -> tuple[list[AuditMatter], int]:
    """Runs the closing-document check (Clio only — see module docstring
    on why the Y: drive fallback was dropped) for every currently open or
    pending matter, re-scanning only what changed since the last run unless
    `full` (see the section comment above). Returns (every matter, how many
    were actually re-scanned). Each re-scanned matter's result is saved as
    soon as it's done, so a run that dies partway still keeps its progress.
    Never throttles preemptively — relies on find_soa_now_documents()'s own
    reactive 429 retry."""
    matters = fetch_open_matters_for_audit(session)
    open_ids = {m.matter_id for m in matters}

    # A matter that left the open list (closed, or excluded) loses its saved
    # result, so if it's ever reopened it gets a fresh scan rather than
    # trusting findings from before changes we never looked at.
    saved = {
        row["matter_id"]: row
        for row in conn.execute("SELECT * FROM case_closing_matter_scans").fetchall()
    }
    stale = [mid for mid in saved if mid not in open_ids]
    conn.executemany("DELETE FROM case_closing_matter_scans WHERE matter_id = ?", [(mid,) for mid in stale])
    conn.commit()

    previous = latest_run(conn, RUN_KIND_AUDIT)
    changed: set[int] | None = None
    if not full and previous is not None:
        since = datetime.fromisoformat(previous["started_at"]) - CHANGE_CURSOR_OVERLAP
        changed = fetch_changed_matter_ids(session, since)

    rescanned = 0
    for m in matters:
        row = saved.get(m.matter_id)
        if changed is not None and row is not None and row["rules_version"] == SCAN_RULES_VERSION and m.matter_id not in changed:
            m.findings = json.loads(row["findings_json"])
            m.can_close = bool(row["can_close"])
            continue
        _scan_matter(session, m)
        rescanned += 1
        conn.execute(
            "INSERT OR REPLACE INTO case_closing_matter_scans (matter_id, findings_json, can_close, rules_version, scanned_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (m.matter_id, json.dumps(m.findings), int(m.can_close), SCAN_RULES_VERSION, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()

    logging.info(
        "Case Closing audit: %d open/pending matters, %d re-scanned (%s)",
        len(matters), rescanned, "full" if changed is None else f"{len(changed)} matters with changed documents/folders",
    )
    return matters, rescanned


def record_run(conn, kind: str, started_at: datetime, results: list[dict], full: bool = False,
               scanned_count: int | None = None, rescanned_count: int | None = None) -> None:
    conn.execute(
        "INSERT INTO case_closing_runs (kind, started_at, finished_at, full_scan, scanned_count, rescanned_count, results_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (kind, started_at.isoformat(), datetime.now(timezone.utc).isoformat(), int(full),
         scanned_count, rescanned_count, json.dumps(results)),
    )
    conn.commit()


def update_latest_results(conn, kind: str, fn) -> None:
    """Applies fn(list[dict]) -> list[dict] to the latest run's displayed
    results in place — used after Close Case / Mark Filed so a reload shows
    the change without a fresh run."""
    row = latest_run(conn, kind)
    if row is None:
        return
    conn.execute(
        "UPDATE case_closing_runs SET results_json = ? WHERE id = ?",
        (json.dumps(fn(json.loads(row["results_json"]))), row["id"]),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Trust refunds — closed matters with a settled final bill but money left
# in trust (added 2026-10-14). Ted's rule: closed, no unbilled work (WIP =
# 0, which includes draft/awaiting-approval bills), no unpaid revenue bill,
# and trust > 0 — that leftover is the refund. Flag only; the refund itself
# is processed by hand in Clio. Reuses trust_monitor.py's own already-
# audited WIP/trust/outstanding math rather than re-deriving it.
# ---------------------------------------------------------------------------

REFUND_EPSILON = 0.005  # ignore sub-cent float residue


@dataclass
class RefundCandidate:
    matter_id: int
    display_number: str
    client_id: int
    client_name: str
    attorney_name: str
    matter_trust: float
    # held on the contact, not any matter — shown in its own column, since
    # it's one balance shared by every matter that client has
    client_trust: float

    def to_dict(self) -> dict:
        return asdict(self)


def fetch_refund_candidates(session: requests.Session) -> list[RefundCandidate]:
    """Closed matters meeting the refund rule above. Client-level trust
    (Contact.account_balances, via staff_unbilled_monitor) is shown
    alongside and can qualify a matter on its own — money stuck on the
    contact is just as refundable once the work is done — except when that
    client still has an open/pending matter, where it may still be working."""
    closed = matter_matching.fetch_open_matters(session, fields=trust_monitor.MATTERS_FIELDS, status="closed")
    never_billed = trust_monitor.fetch_billable_matters_unbilled(session)
    bill_wip = trust_monitor.fetch_bills_by_matter(session, trust_monitor.BILL_WIP_STATES, "total")
    outstanding = trust_monitor.fetch_bills_by_matter(session, trust_monitor.BILL_OUTSTANDING_STATES, "balance")

    client_ids = {int(m["client"]["id"]) for m in closed if (m.get("client") or {}).get("id")}
    client_trust = fetch_client_trust_balances(session, client_ids)
    active = matter_matching.fetch_open_matters(session, fields="id,client{id}", status="open,pending")
    clients_with_active = {int(m["client"]["id"]) for m in active if (m.get("client") or {}).get("id")}

    results: list[RefundCandidate] = []
    for m in closed:
        if (m.get("display_number") or "").strip().upper() in ca.EXCLUDED_MATTER_NAMES:
            continue
        client = m.get("client") or {}
        cid = int(client["id"]) if client.get("id") else 0
        if cid in trust_monitor.FIRM_OVERHEAD_CLIENT_IDS:
            continue
        mid = m["id"]
        wip = never_billed.get(mid, 0.0) + bill_wip.get(mid, 0.0)
        if wip > REFUND_EPSILON or outstanding.get(mid, 0.0) > REFUND_EPSILON:
            continue  # final bill not settled yet
        m_trust = float(next(
            (b.get("balance") or 0.0 for b in (m.get("account_balances") or []) if b.get("type") == "Trust"), 0.0
        ))
        c_trust = client_trust.get(cid, 0.0)
        if m_trust <= REFUND_EPSILON and (c_trust <= REFUND_EPSILON or cid in clients_with_active):
            continue
        results.append(RefundCandidate(
            matter_id=mid,
            display_number=m.get("display_number") or "",
            client_id=cid,
            client_name=client.get("name") or "",
            attorney_name=(m.get("responsible_attorney") or {}).get("name") or "",
            matter_trust=m_trust,
            client_trust=c_trust,
        ))
    logging.info("Trust refund check: %d closed matters, %d with trust to refund", len(closed), len(results))
    return sorted(results, key=lambda r: -(r.matter_trust + r.client_trust))
