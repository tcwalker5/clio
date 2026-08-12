"""
trust_monitor.py — Two related but independent things:

1. A WIP-vs-trust monitor (build_trust_statuses/TrustStatus) — flags matters
   where unbilled work (WIP) has pushed the trust "cushion" (trust - WIP)
   below TRUST_MINIMUM. This is purely an early-warning signal: "WIP is
   heading toward zeroing out trust before it even gets billed" — shown on
   the Full Monitor table/CSV. It does NOT drive request amounts (see #2) —
   billing (which is how WIP actually gets collected) stays 100% manual,
   done through Clio's own UI on the firm's existing monthly + occasional
   ad hoc cycle. Decided 2026-07-30 (Ted): "I'd rather always bill than add
   to trust and then bill" — this tool doesn't try to pre-fund unbilled work
   via a trust request.

2. A trust replenishment request workflow (evaluate_request_candidates) —
   deliberately WIP-independent. A matter becomes a request "candidate"
   purely on its raw trust balance:
     TRUST_MINIMUM (2500) — the target trust balance a request tops up to
       (and what a matter is considered "resolved" at once trust reaches).
     ACTION_GATE (2000) — a request is only generated once trust itself
       drops below this. Fixed regardless of a per-matter target override
       (an attorney can raise the target above $2,500 for upcoming work,
       but that only changes the requested amount once a matter is already
       past the gate — confirmed with Ted: overrides are entered "before
       the request is made," i.e. the matter is already a candidate).

Request lifecycle, backed by two tables in data/clio_dashboard.db
(trust_matter_settings, trust_requests — see web/db.py): a matter below
ACTION_GATE becomes a "candidate" if it has no pending request yet; if it
has one, an exact match between the recorded trust-at-request and current
trust means the client just hasn't paid yet ("already_requested" — below
the gate, trust can only hold or worsen while unpaid, so any difference
means it moved); a real difference marks the old request "stale" and
generates a fresh candidate. Paused matters (trust_matter_settings.paused)
always show as "paused" and never generate a request until unpaused or the
matter closes (drops out naturally — the matter universe is open matters
only).

Sending is a real, client-facing, hard-to-reverse action (POST
/trust_requests.json, approved: false so it lands as a draft rather than
auto-sending) — Clio's API has no GET/list endpoint for trust requests at
all, so trust_requests is the *only* record of what's already been
requested. The very first live send should be done with a human watching
Clio's UI to confirm `approved: false` really parks it for review and
doesn't notify the client — nothing here has verified that yet.

WIP is NOT read from BillableMatter.unbilled_amount alone — confirmed live
2026-07-29 (real matter WELLS, ANDREW) that field only counts activity never
added to ANY bill, silently excluding activity already sitting on a draft or
awaiting-approval bill (which was $11,125.82 of his real $11,275.82 WIP —
the tool was showing $150). True WIP here = unbilled_amount + the total of
that matter's bills in state draft/awaiting_approval. "Outstanding" (already
invoiced, unpaid) is a separate figure = the balance of bills in state
awaiting_payment — kept only to decide whether a genuinely dormant matter
(zero trust, zero WIP, zero outstanding) can be skipped entirely; it is NOT
displayed on /trust anymore. Split out 2026-08-11 (Ted) into its own page,
/collections (collections_monitor.py) — a retainer shortfall and an overdue
bill are different problems with different remedies (a TrustRequest can't
legally carry the client's card fee; a direct bill payment can), and showing
both on one table made them look related when they never were.

Trust balance comes from Matter.account_balances (type "Trust"), not
BillableMatter.amount_in_trust — confirmed live that BillableMatter only
covers matters with nonzero never-billed activity (60 of 186 real matters
with pending bill activity, 2026-07-29), so it's too narrow to use as the
matter universe. account_balances needs the Accounting permission (granted
2026-07-29 — was returning {"redacted": true} before that).

Usage:
  uv run src/trust_monitor.py
"""

import argparse
import csv
import logging
import math
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(ENV_PATH)

from matter_matching import fetch_open_matters  # noqa: E402

BASE_URL = os.getenv("CLIO_BASE_URL", "https://app.clio.com").rstrip("/")
ACCESS_TOKEN = os.getenv("CLIO_ACCESS_TOKEN", "")

MATTERS_FIELDS = "id,display_number,status,client{id,name},account_balances{id,balance,type,name}"

BILLABLE_MATTERS_ENDPOINT = f"{BASE_URL}/api/v4/billable_matters.json"
BILLABLE_MATTERS_FIELDS = "id,unbilled_amount"

BILLS_ENDPOINT = f"{BASE_URL}/api/v4/bills.json"
BILLS_FIELDS = "id,state,total,balance,matters{id}"
# Bills that haven't been sent yet still count as WIP (the work is done and
# assigned to a bill, just not finalized) — confirmed no bill firm-wide ever
# has more than one matter attached (2026-07-29: 1 draft / 143
# awaiting_approval / 95 awaiting_payment, all len(matters) in {0, 1}), so
# each bill's full total/balance is attributed to its single matter with no
# need to split via matter_totals.
BILL_WIP_STATES = ("draft", "awaiting_approval")
BILL_OUTSTANDING_STATES = ("awaiting_payment",)

PAGE_SIZE = 200

# The firm's own internal time-tracking bucket, not a real client — appears
# in the matters list like any other open matter but should never be
# flagged for a trust request. Keyed by Clio client ID rather than name —
# confirmed live 2026-07-29.
FIRM_OVERHEAD_CLIENT_IDS: dict[int, str] = {
    2392038723: "ADMIN NON-BILLABLE",
}

TRUST_MINIMUM = 2500.00  # firm policy: target trust balance a request tops up to
ACTION_GATE = 2000.00  # a request is only generated once trust balance drops below this
TRUST_REQUEST_DUE_DAYS = 14  # no firm convention yet — adjustable, not a hard policy

# Clio Payments' card processing surcharge — informational only, shown next
# to a bulk selection so staff can see the firm's fee exposure before
# sending (trust deposits can't legally pass this fee to the client the way
# a bill payment can, so the firm absorbs it if a client pays a trust
# request by card). Not applied to any request amount or stored anywhere.
CARD_FEE_RATE = 0.0295

TRUST_REQUESTS_ENDPOINT = f"{BASE_URL}/api/v4/trust_requests.json"


def setup_logging(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"trust_monitor_{datetime.today().strftime('%Y%m%d')}.log"
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


@dataclass
class TrustStatus:
    matter_id: int
    display_number: str
    client_id: int
    client_name: str
    unbilled_amount: float  # WIP: never-billed + on a draft/awaiting-approval bill
    amount_in_trust: float
    outstanding: float  # already invoiced, unpaid (awaiting_payment bills) — used only for the dormant-matter skip check below, not displayed (see /collections)

    @property
    def cushion(self) -> float:
        """What's left in trust after covering current unbilled work."""
        return self.amount_in_trust - self.unbilled_amount

    @property
    def shortfall(self) -> float:
        return max(0.0, TRUST_MINIMUM - self.cushion)

    @property
    def flagged(self) -> bool:
        return self.cushion < TRUST_MINIMUM


def fetch_billable_matters_unbilled(session: requests.Session) -> dict[int, float]:
    """matter_id -> never-billed unbilled_amount. Clio only returns a record
    here for matters with nonzero never-billed activity — matters absent
    from this dict have $0 in this specific slice (confirmed live: a matter
    with real pending-bill WIP but zero never-billed activity returns zero
    records here), not missing data."""
    result: dict[int, float] = {}
    next_url: str | None = None
    page = 1
    while True:
        if next_url:
            resp = session.get(next_url)
        else:
            resp = session.get(BILLABLE_MATTERS_ENDPOINT, params={"fields": BILLABLE_MATTERS_FIELDS, "limit": PAGE_SIZE})
        if resp.status_code != 200:
            raise RuntimeError(f"Failed to fetch billable matters (page {page}): {resp.status_code} {resp.text[:200]}")
        body = resp.json()
        for m in body.get("data", []):
            result[m["id"]] = float(m.get("unbilled_amount") or 0)
        next_url = (body.get("meta") or {}).get("paging", {}).get("next")
        logging.info("Fetched billable matters page %d (%d matters so far)", page, len(result))
        page += 1
        if not next_url:
            break
    return result


def fetch_bills_by_matter(session: requests.Session, states: tuple[str, ...], amount_field: str) -> dict[int, float]:
    """matter_id -> summed amount_field, across bills in any of `states`.
    Bills with no matter attached (real example: two firm-wide bills with
    matters: []) are skipped — nothing to attribute them to."""
    totals: dict[int, float] = {}
    for state in states:
        next_url: str | None = None
        page = 1
        while True:
            if next_url:
                resp = session.get(next_url)
            else:
                resp = session.get(BILLS_ENDPOINT, params={"fields": BILLS_FIELDS, "state": state, "limit": PAGE_SIZE})
            if resp.status_code != 200:
                raise RuntimeError(f"Failed to fetch bills (state={state}, page {page}): {resp.status_code} {resp.text[:200]}")
            body = resp.json()
            for b in body.get("data", []):
                matters = b.get("matters") or []
                if len(matters) > 1:
                    logging.warning("Bill %s has %d matters attached — skipping, expected 0 or 1", b.get("id"), len(matters))
                    continue
                if not matters:
                    continue
                mid = matters[0]["id"]
                totals[mid] = totals.get(mid, 0.0) + float(b.get(amount_field) or 0)
            next_url = (body.get("meta") or {}).get("paging", {}).get("next")
            logging.info("Fetched bills page %d for state=%s", page, state)
            page += 1
            if not next_url:
                break
    return totals


def build_trust_statuses(
    matters: list[dict],
    never_billed: dict[int, float],
    bill_wip: dict[int, float],
    bill_outstanding: dict[int, float],
) -> list[TrustStatus]:
    statuses: list[TrustStatus] = []
    for m in matters:
        client = m.get("client") or {}
        client_id = client.get("id")
        if client_id and int(client_id) in FIRM_OVERHEAD_CLIENT_IDS:
            continue

        mid = m["id"]
        trust = next((b.get("balance") or 0.0 for b in (m.get("account_balances") or []) if b.get("type") == "Trust"), 0.0)
        wip = never_billed.get(mid, 0.0) + bill_wip.get(mid, 0.0)
        outstanding = bill_outstanding.get(mid, 0.0)

        # Skip matters with zero financial footprint entirely (e.g. a fresh
        # intake with no trust deposited and no work done yet) — nothing to
        # monitor, and including them would flag every dormant matter as
        # "below cushion" just because 0 < TRUST_MINIMUM.
        if trust == 0 and wip == 0 and outstanding == 0:
            continue

        statuses.append(TrustStatus(
            matter_id=mid,
            display_number=m.get("display_number", ""),
            client_id=int(client_id) if client_id else 0,
            client_name=client.get("name", ""),
            unbilled_amount=wip,
            amount_in_trust=float(trust),
            outstanding=outstanding,
        ))
    return statuses


def write_report_csv(statuses: list[TrustStatus], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Matter", "Unbilled (WIP)", "In Trust", "Cushion", "Shortfall", "Outstanding", "Flagged"])
        for s in sorted(statuses, key=lambda s: s.cushion):
            writer.writerow([
                s.display_number,
                f"{s.unbilled_amount:.2f}", f"{s.amount_in_trust:.2f}",
                f"{s.cushion:.2f}", f"{s.shortfall:.2f}", f"{s.outstanding:.2f}",
                "yes" if s.flagged else "",
            ])


@dataclass
class TrustRequestCandidate:
    matter_id: int
    display_number: str
    client_id: int
    client_name: str
    trust_balance: float  # raw trust balance — NOT the cushion (trust - WIP)
    target_amount: float
    requested_amount: float
    state: str  # "candidate", "already_requested", "paused"
    requested_at: str | None = None  # set for "already_requested"


def _load_matter_settings(conn) -> dict[int, dict]:
    rows = conn.execute("SELECT matter_id, target_amount, paused, note FROM trust_matter_settings").fetchall()
    return {r["matter_id"]: dict(r) for r in rows}


def _load_pending_requests(conn) -> dict[int, dict]:
    """matter_id -> its one 'pending' trust_requests row, if any. There
    should only ever be one at a time per matter by construction (a new
    request always marks the old one stale/resolved first)."""
    rows = conn.execute("SELECT * FROM trust_requests WHERE status = 'pending'").fetchall()
    return {r["matter_id"]: dict(r) for r in rows}


def set_matter_target(conn, matter_id: int, target_amount: float | None, note: str = "") -> None:
    conn.execute(
        """INSERT INTO trust_matter_settings (matter_id, target_amount, paused, note, updated_at)
           VALUES (?, ?, COALESCE((SELECT paused FROM trust_matter_settings WHERE matter_id = ?), 0), ?, CURRENT_TIMESTAMP)
           ON CONFLICT(matter_id) DO UPDATE SET target_amount = excluded.target_amount, note = excluded.note, updated_at = CURRENT_TIMESTAMP""",
        (matter_id, target_amount, matter_id, note),
    )
    conn.commit()


def set_matter_paused(conn, matter_id: int, paused: bool) -> None:
    conn.execute(
        """INSERT INTO trust_matter_settings (matter_id, paused, updated_at)
           VALUES (?, ?, CURRENT_TIMESTAMP)
           ON CONFLICT(matter_id) DO UPDATE SET paused = excluded.paused, updated_at = CURRENT_TIMESTAMP""",
        (matter_id, int(paused)),
    )
    conn.commit()


def record_trust_request(
    conn, matter_id: int, target_amount: float, trust_balance: float, requested_amount: float,
    clio_trust_request_id: int | None = None,
) -> int:
    cur = conn.execute(
        """INSERT INTO trust_requests (matter_id, target_amount, trust_at_request, requested_amount, clio_trust_request_id, status)
           VALUES (?, ?, ?, ?, ?, 'pending')""",
        (matter_id, target_amount, trust_balance, requested_amount, clio_trust_request_id),
    )
    conn.commit()
    return cur.lastrowid


def _round_up_to_10(amount: float) -> float:
    """Requested amounts are rounded up to the next $10 — a clean number is
    easier for a client to write a check for than $2,537.26, and rounding up
    (never down) never leaves the matter still under target."""
    return math.ceil(round(amount, 2) / 10.0) * 10.0


def evaluate_request_candidates(conn, statuses: list[TrustStatus]) -> list[TrustRequestCandidate]:
    """Reconciles current trust balance (NOT cushion — deliberately
    WIP-independent, see module docstring) against persisted settings/
    history and returns the actionable list — also mutates trust_requests as
    a side effect (marking rows resolved once trust recovers, or stale once
    a pending request's trust balance no longer matches what's live)."""
    settings = _load_matter_settings(conn)
    pending = _load_pending_requests(conn)
    candidates: list[TrustRequestCandidate] = []

    for s in statuses:
        setting = settings.get(s.matter_id, {})
        target = setting.get("target_amount") or TRUST_MINIMUM
        paused = bool(setting.get("paused"))
        trust = round(s.amount_in_trust, 2)
        existing = pending.get(s.matter_id)

        if paused:
            candidates.append(TrustRequestCandidate(
                matter_id=s.matter_id, display_number=s.display_number,
                client_id=s.client_id, client_name=s.client_name,
                trust_balance=trust, target_amount=target, requested_amount=0.0,
                state="paused",
            ))
            continue

        if trust >= target:
            if existing:
                conn.execute("UPDATE trust_requests SET status = 'resolved', resolved_at = CURRENT_TIMESTAMP WHERE id = ?", (existing["id"],))
            continue

        if trust >= ACTION_GATE:
            continue  # below target but not urgent enough to bother the client yet

        if existing and round(existing["trust_at_request"], 2) == trust:
            candidates.append(TrustRequestCandidate(
                matter_id=s.matter_id, display_number=s.display_number,
                client_id=s.client_id, client_name=s.client_name,
                trust_balance=trust, target_amount=target,
                requested_amount=existing["requested_amount"],
                state="already_requested", requested_at=existing["created_at"],
            ))
            continue

        if existing:
            conn.execute("UPDATE trust_requests SET status = 'stale' WHERE id = ?", (existing["id"],))

        candidates.append(TrustRequestCandidate(
            matter_id=s.matter_id, display_number=s.display_number,
            client_id=s.client_id, client_name=s.client_name,
            trust_balance=trust, target_amount=target,
            requested_amount=_round_up_to_10(target - trust),
            state="candidate",
        ))

    conn.commit()
    return candidates


def create_trust_request(session: requests.Session, client_id: int, matter_id: int, amount: float) -> int:
    """POSTs a real TrustRequest to Clio with approved=False so it lands as
    a draft for internal review rather than auto-sending to the client —
    per Clio's own field description ("Whether or not the TrustRequest
    should be automatically approved"). NOT YET LIVE-TESTED as of writing —
    the first real call should happen with a human watching Clio's UI to
    confirm that assumption before this is used for anything but a single
    supervised test.

    Per-matter trust_amount is sent as an int, not a float — Clio's own
    OpenAPI spec (clio-rate-import/data/openapi.json, POST /trust_requests.json)
    types the top-level data.trust_amount as number/double but the nested
    data.matter[].trust_amount as integer/int32. Amounts here are always
    whole tens already (_round_up_to_10), so this loses no precision;
    it just avoids handing Clio "2600.0" where it declared an integer field."""
    today = datetime.today().date()
    due = today + timedelta(days=TRUST_REQUEST_DUE_DAYS)
    amount = round(amount, 2)
    payload = {
        "data": {
            "trust_type": "matter",
            "client_id": client_id,
            "issue_date": today.isoformat(),
            "due_date": due.isoformat(),
            "approved": False,
            "trust_amount": amount,
            "matter": [{"id": matter_id, "trust_amount": int(amount)}],
        }
    }
    logging.info("POST %s matter=%s payload=%s", TRUST_REQUESTS_ENDPOINT, matter_id, payload)
    resp = session.post(TRUST_REQUESTS_ENDPOINT, json=payload)
    logging.info("Response for matter %s: %s %s", matter_id, resp.status_code, resp.text[:500])
    if resp.status_code != 201:
        raise RuntimeError(f"Failed to create trust request for matter {matter_id}: {resp.status_code} {resp.text[:300]}")
    return resp.json()["data"]["id"]


def build_session() -> requests.Session:
    if not ACCESS_TOKEN:
        raise RuntimeError("CLIO_ACCESS_TOKEN not set in .env")
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {ACCESS_TOKEN}"})
    return session


def run_pipeline(output_dir: Path = Path("output")) -> list[TrustStatus]:
    setup_logging(Path("logs"))

    session = build_session()

    matters = fetch_open_matters(session, fields=MATTERS_FIELDS)
    logging.info("Fetched %d open matters", len(matters))

    never_billed = fetch_billable_matters_unbilled(session)
    bill_wip = fetch_bills_by_matter(session, BILL_WIP_STATES, "total")
    bill_outstanding = fetch_bills_by_matter(session, BILL_OUTSTANDING_STATES, "balance")

    statuses = build_trust_statuses(matters, never_billed, bill_wip, bill_outstanding)
    flagged = [s for s in statuses if s.flagged]

    today = datetime.today().strftime("%Y-%m-%d")
    output_dir.mkdir(exist_ok=True)
    report_path = output_dir / f"trust_monitor_{today}.csv"
    write_report_csv(statuses, report_path)

    logging.info(
        "Checked %d matters with real financial activity (of %d open) — %d below the $%.0f cushion minimum. Report: %s",
        len(statuses), len(matters), len(flagged), TRUST_MINIMUM, report_path,
    )
    for s in flagged:
        logging.info(
            "  FLAGGED %s (%s) - WIP $%.2f, trust $%.2f, cushion $%.2f, short $%.2f, outstanding $%.2f",
            s.display_number, s.client_name, s.unbilled_amount, s.amount_in_trust, s.cushion, s.shortfall, s.outstanding,
        )

    return statuses


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.parse_args()
    run_pipeline()


if __name__ == "__main__":
    main()
