"""
trust_monitor.py — WIP-vs-trust report, shared directly with attorneys.

**Redesigned 2026-09-16 (Ted).** Live Clio trust-request sending has been
blocked/on hold since 2026-08-12 with no resolution from Clio support (see
reference/billing-monitors.md's full diagnostic writeup) — every real send
attempt fails with an empty-body 400. Rather than keep building review UI
around a send path that has never once worked, `/trust` pivoted to a pure
self-service report: every open matter's WIP/trust/balance-due picture,
plus a per-matter trust TARGET that the responsible attorney sets directly
(default $2,500, firm policy — `TRUST_MINIMUM`), saved instantly on change,
same "instant-persist, no confirm button" pattern as Client Assignment's/
Collections' own dropdowns. There is nothing left to Send or Pause, so
those buttons and the whole candidate/already-requested/paused lifecycle
they drove are gone from the dashboard page.

`evaluate_request_candidates`/`create_trust_request`/`record_trust_request`/
`set_matter_paused`/`TrustRequestCandidate` below are UNTOUCHED and still
fully working — they're just no longer wired into `routes_trust.py`. Kept
here (not deleted) for whenever Clio support's reply unblocks live sending;
see billing-monitors.md before reviving them, not this docstring, for the
current state of that investigation.

**The report itself** (`build_trust_statuses`/`TrustStatus`, then
`build_report_rows`/`TrustReportRow` merges in each matter's attorney-set
target from `trust_matter_settings`) shows, per open matter with any real
financial footprint:
  - **WIP** — NOT `BillableMatter.unbilled_amount` alone. Confirmed live
    2026-07-29 (real matter WELLS, ANDREW) that field only counts activity
    never added to ANY bill, silently excluding activity already sitting on
    a draft or awaiting-approval bill ($11,125.82 of his real $11,275.82
    WIP — the tool was showing $150). True WIP = unbilled_amount + the
    total of that matter's bills in state draft/awaiting_approval.
  - **In Trust** — from `Matter.account_balances` (type "Trust"), not
    `BillableMatter.amount_in_trust` — confirmed live that BillableMatter
    only covers matters with nonzero never-billed activity (60 of 186 real
    matters with pending bill activity, 2026-07-29), too narrow to use as
    the matter universe. Needs the Accounting permission (granted
    2026-07-29 — was returning {"redacted": true} before that).
  - **Cushion** — trust minus WIP, the early-warning signal that unbilled
    work is outpacing trust before it's even billed. Compared against each
    matter's own **Target** (not a fixed number) to decide `flagged` —
    changed 2026-09-16 alongside the redesign above, since the target field
    now exists specifically for the attorney to say what cushion they want.
    This does NOT drive billing — billing stays 100% manual, done through
    Clio's own UI on the firm's existing cycle. Decided 2026-07-30 (Ted):
    "I'd rather always bill than add to trust and then bill" — this tool
    never pre-funds unbilled work via a trust request.
  - **Balance Due** — already invoiced, unpaid (sum of bills in state
    awaiting_payment). Re-added to the display 2026-09-16 at Ted's request
    ("full picture") after being deliberately removed 2026-08-11 into its
    own page, /collections — a retainer shortfall and an overdue bill are
    still different problems with different remedies (a TrustRequest can't
    legally carry the client's card fee; a direct bill payment can), so
    this column is kept visually separate from the WIP/Trust/Cushion
    formula rather than folded into it — the exact juxtaposition that made
    the original column read as related when it never was.

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

MATTERS_FIELDS = (
    "id,display_number,status,client{id,name},account_balances{id,balance,type,name},"
    "responsible_attorney{id,name}"
)

BILLABLE_MATTERS_ENDPOINT = f"{BASE_URL}/api/v4/billable_matters.json"
BILLABLE_MATTERS_FIELDS = "id,unbilled_amount"

BILLS_ENDPOINT = f"{BASE_URL}/api/v4/bills.json"
BILLS_FIELDS = "id,state,total,balance,kind,matters{id}"
# Bills that haven't been sent yet still count as WIP (the work is done and
# assigned to a bill, just not finalized) — confirmed no bill firm-wide ever
# has more than one matter attached (2026-07-29: 1 draft / 143
# awaiting_approval / 95 awaiting_payment, all len(matters) in {0, 1}), so
# each bill's full total/balance is attributed to its single matter with no
# need to split via matter_totals.
BILL_WIP_STATES = ("draft", "awaiting_approval")
BILL_OUTSTANDING_STATES = ("awaiting_payment",)
# Only ever sum "revenue_kind" bills (billed WORK) for WIP/outstanding —
# Clio's Bill.kind also has "trust_kind" for a trust deposit/replenishment
# request (same distinction collections_monitor.py's UnpaidBill.category
# was built around). Confirmed live 2026-09-16: 10 real matters have an
# unpaid trust_kind bill sitting in awaiting_payment (a pending trust
# request, e.g. WELLS, ANDREW's $7,560 request from the blocked-sending
# investigation) — without this filter, those got summed into "Balance Due"
# on /trust as if they were money owed for work already done, which they
# are not. No trust_kind bill has been seen live in draft/awaiting_approval
# (WIP) as of this check, but the same filter is applied there too, on the
# same reasoning, rather than assuming that stays true.
BILL_KIND_REVENUE = "revenue_kind"

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
    attorney_name: str  # responsible attorney — "" if unassigned (see Client Assignment for filling those gaps)
    unbilled_amount: float  # WIP: never-billed + on a draft/awaiting-approval bill
    amount_in_trust: float
    outstanding: float  # already invoiced, unpaid, revenue_kind only (awaiting_payment bills, trust_kind excluded — see BILL_KIND_REVENUE) — "Balance Due" on /trust, informational only, kept out of the cushion formula below (see module docstring)

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
    """matter_id -> summed amount_field, across revenue_kind bills (billed
    WORK — see BILL_KIND_REVENUE) in any of `states`. A trust_kind bill
    (deposit/replenishment request) is skipped even if it's in one of these
    states — it isn't money owed for work done, and mixing it in silently
    overstated WIP/Balance Due (confirmed live 2026-09-16, see
    BILL_KIND_REVENUE's comment). Bills with no matter attached (real
    example: two firm-wide bills with matters: []) are also skipped —
    nothing to attribute them to."""
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
                if b.get("kind") != BILL_KIND_REVENUE:
                    continue
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
        attorney = m.get("responsible_attorney") or {}

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
            attorney_name=attorney.get("name") or "",
            unbilled_amount=wip,
            amount_in_trust=float(trust),
            outstanding=outstanding,
        ))
    return statuses


def write_report_csv(statuses: list[TrustStatus], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Matter", "Attorney", "Unbilled (WIP)", "In Trust", "Cushion", "Shortfall", "Balance Due", "Flagged"])
        for s in sorted(statuses, key=lambda s: s.cushion):
            writer.writerow([
                s.display_number, s.attorney_name or "—",
                f"{s.unbilled_amount:.2f}", f"{s.amount_in_trust:.2f}",
                f"{s.cushion:.2f}", f"{s.shortfall:.2f}", f"{s.outstanding:.2f}",
                "yes" if s.flagged else "",
            ])


# --- Everything below this line is the retired live-send candidate workflow ---
# Not called from routes_trust.py as of the 2026-09-16 redesign (see module
# docstring) — Clio's own trust_requests.json POST has never once succeeded
# live (empty-body 400, escalated to Clio support, see
# reference/billing-monitors.md). Left in place, fully working on its own
# terms, for whenever that's unblocked — don't delete without checking that
# doc first.


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


@dataclass
class TrustReportRow:
    """One row of the attorney-facing /trust report — a TrustStatus plus
    that matter's attorney-set trust target (trust_matter_settings,
    defaulting to TRUST_MINIMUM), everything the report needs in one place
    so the template doesn't have to reach into two objects per row."""
    matter_id: int
    display_number: str
    client_name: str
    attorney_name: str
    unbilled_amount: float  # WIP
    amount_in_trust: float
    balance_due: float  # already invoiced, unpaid — informational, see module docstring
    target_amount: float  # attorney-set, defaults to TRUST_MINIMUM

    @property
    def cushion(self) -> float:
        return self.amount_in_trust - self.unbilled_amount

    @property
    def shortfall(self) -> float:
        return max(0.0, self.target_amount - self.cushion)

    @property
    def flagged(self) -> bool:
        """Below the attorney's OWN target, not a fixed number — the whole
        point of letting them set one (changed 2026-09-16)."""
        return self.cushion < self.target_amount


def build_report_rows(conn, statuses: list[TrustStatus]) -> list[TrustReportRow]:
    """Merges each matter's persisted trust_matter_settings.target_amount
    (attorney override, if any) into its live TrustStatus. No gating here —
    unlike the retired candidate workflow's ACTION_GATE, every matter with
    real financial footprint shows up, since an attorney should be able to
    set a target for any of their matters, not just ones already critical."""
    settings = _load_matter_settings(conn)
    rows = []
    for s in statuses:
        setting = settings.get(s.matter_id, {})
        target = setting.get("target_amount") or TRUST_MINIMUM
        rows.append(TrustReportRow(
            matter_id=s.matter_id,
            display_number=s.display_number,
            client_name=s.client_name,
            attorney_name=s.attorney_name,
            unbilled_amount=s.unbilled_amount,
            amount_in_trust=s.amount_in_trust,
            balance_due=s.outstanding,
            target_amount=target,
        ))
    return rows


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
    OpenAPI spec (this project's reference/openapi.json, POST /trust_requests.json)
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
