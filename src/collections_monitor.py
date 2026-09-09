"""
collections_monitor.py — Read-only view of unpaid, already-issued bills
(state=awaiting_payment) — the "who owes money for work already earned and
billed" counterpart to trust_monitor.py's retainer-cushion tracker.

Split out from Trust Monitor 2026-08-11 (Ted): a retainer shortfall and an
overdue bill call for genuinely different remedies. A TrustRequest tops up
money held for FUTURE unearned work, and can't legally carry the client's
card processing fee (see trust_monitor.py). Collecting an overdue bill is a
payment for work already done, and the firm CAN pass that surcharge to the
client via a direct bill payment. /trust used to show both on one table (an
"Outstanding" column bolted onto the WIP/cushion math) — that read as if
the two numbers were related, when they never were. This module and
/collections exist purely so "who's low on retainer" and "who owes money"
are two separate questions with two separate answers.

Visibility-only, no send action — same incremental path Trust Monitor
itself started on. An actual "request payment" action (a payable link the
client can pay by card) needs the Clio Payments permission, not currently
granted to this app's Developer Portal registration — see CLAUDE.md's App
Permissions table.

Usage:
  uv run src/collections_monitor.py
"""

import argparse
import csv
import logging
import os
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import requests
from dotenv import load_dotenv

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(ENV_PATH)

BASE_URL = os.getenv("CLIO_BASE_URL", "https://app.clio.com").rstrip("/")
ACCESS_TOKEN = os.getenv("CLIO_ACCESS_TOKEN", "")

BILLS_ENDPOINT = f"{BASE_URL}/api/v4/bills.json"
BILLS_FIELDS = "id,number,issued_at,due_at,total,balance,kind,client{id,name},matters{id,display_number}"
BILL_STATE = "awaiting_payment"

# Used only by fetch_matter_trust_balance below, to catch a CLIENT-level
# trust request (see MatterBillSummary.trust_level_mismatch) whose actual
# deposit got recorded against a matter's own trust ledger instead of
# applied to the request bill. Same account_balances field trust_monitor.py
# already relies on for matter-level trust balances — needs the Accounting
# permission (granted 2026-07-29, see trust_monitor.py's own note).
MATTERS_ENDPOINT = f"{BASE_URL}/api/v4/matters.json"
MATTER_TRUST_FIELDS = "id,account_balances{id,balance,type,name}"
# Any status, not just "open" — a matter could have been closed after the
# money was (mis)recorded against it, and this check should still catch it.
MATTER_STATUSES_FOR_TRUST_CHECK = "open,pending,closed"

PAGE_SIZE = 200

# Fixed dropdown of collections handling decisions (Ted, 2026-08-18) — kept
# to this exact list rather than freeform text so the review report reads
# consistently across every matter, same reasoning as this project's other
# explicit-mapping constants (purpose codes, etc.).
#
# FLARPL and Payment plan record our own INTENTION only (Ted, 2026-08-19) —
# neither dropdown value means the thing has actually happened. FLARPL has
# a real, separate confirmation source: Clio's own "FLARPL Recorded" matter
# custom field, read-only from here (collections_flarpl.py) since recording
# a lien is an external act this dashboard doesn't perform. Payment plan
# didn't have an equivalent field when this was first built (confirmed live
# 2026-08-19 — searched "Payment Plan", "Payment", "Installment", "Plan",
# "Schedule": nothing existed yet), so it stayed a plain dropdown option
# with no second-stage indicator. (An earlier version of this feature gave
# it a locally-writable "Active" checkbox instead — removed: a dashboard
# flag with no external truth behind it is exactly the pattern being
# avoided for FLARPL.) **Corrected 2026-08-25:** Ted added a real "Payment
# Plan" matter custom field (id 19347918, checkbox) — there's still no Clio
# API for payment plans themselves, but this field is now the same kind of
# confirmation source FLARPL already has, read-only from here
# (collections_payment_plan.py), same reasoning throughout.
# "Payment from sale of home" (added 2026-08-25) is the same shape as Payment
# plan — no matching Clio field, so no Confirmed-column indicator — for the
# common family-law case where the fee balance is expected to be paid out of
# escrow once the marital home sells rather than billed/collected in the
# meantime.
# "Claim as uncollectable" -> "Claim as uncollectable and withdraw" ->
# "Uncollectible and Withdraw" (both renamed 2026-08-25, Ted) — the second
# rename was forced by the print report: `nowrap` plus a printed page's
# fixed width means overflow text is silently cut off rather than wrapped,
# and "Claim as uncollectable and withdraw" was long enough to get clipped
# to "Claim as uncollectable and w" on paper. Shorter text sidesteps that;
# see the SCHEMA migration below for existing rows using either old text.
COLLECTIONS_ACTIONS = [
    "Keep billing",
    "Escalate to attorney",
    "Escalate to Heidi",
    "Send to collections agency",
    "Uncollectible and Withdraw",
    "FLARPL",
    "Payment plan",
    "Payment from sale of home",
]

# Own schema fragment (see web/db.py's _apply_fragment) rather than added to
# its monolithic CORE_SCHEMA — one row per MATTER, not per bill: a matter
# with more than one unpaid bill still gets ONE handling decision, since
# "how are we collecting this" is a client-level call, not per-invoice
# (per Ted: today every matter has at most one unpaid bill anyway, a 1:1
# match, so this doesn't come up in practice yet).
SCHEMA = """
CREATE TABLE IF NOT EXISTS collections_actions (
    matter_id INTEGER PRIMARY KEY,
    action TEXT NOT NULL,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Rename migrations (2026-08-25): "Claim as uncollectable" -> "Claim as
-- uncollectable and withdraw" -> "Uncollectible and Withdraw" in
-- COLLECTIONS_ACTIONS. Re-run on every get_connection() call like
-- everywhere else in this file — cheap, idempotent no-op once no row
-- still has either old text. Without this, an existing decision would
-- silently stop matching any <option>, which the dropdown renders as if
-- nothing had ever been chosen for that matter.
UPDATE collections_actions SET action = 'Uncollectible and Withdraw'
    WHERE action IN ('Claim as uncollectable', 'Claim as uncollectable and withdraw');
"""

SCHEMA_COLUMNS = []


def fetch_actions_by_matter(conn) -> dict[int, str]:
    rows = conn.execute("SELECT matter_id, action FROM collections_actions").fetchall()
    return {row["matter_id"]: row["action"] for row in rows}


def set_action(conn, matter_id: int, action: str) -> None:
    """action="" clears the decision back to unset (the "—" dropdown option)
    — deletes the row rather than trying to store an empty string, which
    used to fail COLLECTIONS_ACTIONS validation and silently fail to save
    (real bug, 2026-08-19: the dropdown itself still showed "—" selected
    since nothing reverted it on the failed request, so it looked saved)."""
    if not action:
        conn.execute("DELETE FROM collections_actions WHERE matter_id = ?", (matter_id,))
        conn.commit()
        return
    if action not in COLLECTIONS_ACTIONS:
        raise ValueError(f"Unknown collections action: {action!r}")
    conn.execute(
        """INSERT INTO collections_actions (matter_id, action, updated_at)
           VALUES (?, ?, CURRENT_TIMESTAMP)
           ON CONFLICT(matter_id) DO UPDATE SET action = excluded.action, updated_at = CURRENT_TIMESTAMP""",
        (matter_id, action),
    )
    conn.commit()


def setup_logging(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"collections_monitor_{datetime.today().strftime('%Y%m%d')}.log"
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
class UnpaidBill:
    bill_id: int
    number: str
    matter_id: int | None
    display_number: str
    client_id: int
    client_name: str
    issued_at: str
    due_at: str | None
    total: float
    balance: float
    kind: str = ""  # Clio's own Bill.kind — "trust_kind" for a trust deposit/replenishment request, "revenue_kind" for billed work

    @property
    def is_trust_request(self) -> bool:
        """A trust_kind Bill isn't payment for work already done — it's a
        request for money to be held for FUTURE work, the same category
        trust_monitor.py's (currently blocked) TrustRequest covers. Ted,
        2026-09-02: a trust_kind bill with no matter is typically a new
        client's initial deposit and isn't subject to collections at all;
        one tied to a matter is typically a replenishment request. Either
        way it must never be treated as overdue AR — see `overdue` below."""
        return self.kind == "trust_kind"

    @property
    def trust_label(self) -> str | None:
        if not self.is_trust_request:
            return None
        return "New client retainer" if self.matter_id is None else "Trust replenishment request"

    @property
    def category(self) -> str:
        """The three buckets Ted actually cares about (2026-09-02), most to
        least urgent: "earned" (billed work, real AR — the whole point of
        this page), "replenishment" (a matter's trust top-up — money we
        care about getting but haven't earned yet), "new_trust" (a brand
        new client's initial retainer deposit — not subject to collections
        at all). Computed per BILL, not per matter, since one matter can
        carry both an earned invoice and a trust top-up bill at once."""
        if not self.is_trust_request:
            return "earned"
        return "new_trust" if self.matter_id is None else "replenishment"

    @property
    def days_overdue(self) -> int:
        if not self.due_at:
            return 0
        due = datetime.strptime(self.due_at, "%Y-%m-%d").date()
        return max(0, (date.today() - due).days)

    @property
    def overdue(self) -> bool:
        if self.is_trust_request:
            return False
        return self.days_overdue > 0


def _last_first(name: str) -> str:
    """Best-effort "Last, First" reformat of a raw Clio contact name ("LISA
    BRANSON", Clio's own convention for an individual's `name` field) so a
    trust-only summary — no matter, so no built-in "Last, First"
    display_number to show instead — sorts and reads the same way as every
    matter-backed row (Ted, 2026-09-09: seeing these out of alphabetical
    order next to real "Last, First" matter names was confusing). Already-
    comma'd names and single-word/company names pass through unchanged —
    this is a display nicety for the common two/three-word individual case,
    not a full name parser."""
    name = (name or "").strip()
    if not name or "," in name or " " not in name:
        return name
    first, _, last = name.rpartition(" ")
    return f"{last}, {first}"


@dataclass
class MatterBillSummary:
    """One row per matter (or per bill, for the rare bill with no matter
    linked — see build_matter_summaries) — Handling and its Clio
    confirmation are matter-level facts (collections_actions is keyed by
    matter_id, not bill_id), so they live here rather than being repeated
    identically on every one of that matter's individual bill rows."""
    matter_id: int | None
    display_number: str
    client_name: str
    bills: list[UnpaidBill]  # this matter's unpaid bills, oldest issued first
    client_id: int = 0  # 0 = no client on the underlying bill (shouldn't happen in practice) — lets a matter-less summary still link to the Clio contact instead of nothing
    action: str = ""  # persisted collections_actions.action, set by the route layer
    flarpl_recorded: bool = False  # live, read-only from Clio's own FLARPL Recorded custom field, only meaningful when action == "FLARPL"
    payment_plan_active: bool = False  # live, read-only from Clio's own Payment Plan custom field, only meaningful when action == "Payment plan"
    matter_trust_balance: float = 0.0  # live, only fetched/meaningful for a client-level trust request (matter_id is None and all_trust) — see trust_level_mismatch and fetch_matter_trust_balance

    @property
    def display_name(self) -> str:
        """What to show/sort by when there's no matter display_number to use
        — see _last_first. Matter-backed summaries never need this (their
        display_number is already "Last, First")."""
        return self.display_number if self.display_number else _last_first(self.client_name)

    @property
    def total_balance(self) -> float:
        return sum(b.balance for b in self.bills)

    @property
    def earned_balance(self) -> float:
        """The only figure that's actually collections AR — see UnpaidBill.category.
        A matter with both an earned invoice and a trust top-up bill splits
        across this and replenishment_balance/new_trust_balance below rather
        than being reported as one blended total_balance."""
        return sum(b.balance for b in self.bills if b.category == "earned")

    @property
    def replenishment_balance(self) -> float:
        return sum(b.balance for b in self.bills if b.category == "replenishment")

    @property
    def new_trust_balance(self) -> float:
        return sum(b.balance for b in self.bills if b.category == "new_trust")

    @property
    def oldest_issued_at(self) -> str:
        return min((b.issued_at for b in self.bills if b.issued_at), default="")

    @property
    def max_days_overdue(self) -> int:
        return max((b.days_overdue for b in self.bills if b.overdue), default=0)

    @property
    def all_trust(self) -> bool:
        """True if every one of this matter's/client's unpaid bills is a
        trust request rather than billed work — see UnpaidBill.is_trust_request.
        Used to badge the whole summary row as a trust request instead of
        past-due/current, and to keep it out of the collections $ totals."""
        return all(b.is_trust_request for b in self.bills)

    @property
    def overdue(self) -> bool:
        return self.max_days_overdue > 0

    @property
    def trust_level_mismatch(self) -> bool:
        """True when this is an unpaid trust request issued at the CLIENT
        level (no matter — see UnpaidBill.trust_label) but the client's own
        matter(s) already hold trust funds. Real incident, 2026-09-09
        (Ted): a Branson trust request was issued at the client level, but
        staff recorded the actual deposit against the matter's own trust
        ledger instead of applying it to this request bill — leaving the
        request stuck "awaiting_payment" forever even though the retainer
        had genuinely been funded (confirmed live: the matter's Trust
        balance was $7,935.00, matching the "unpaid" request exactly). Not
        proof it's the same dollars — a live signal worth a human glance,
        not an automatic conclusion — see
        reference/billing-monitors.md for the full writeup."""
        return self.matter_id is None and self.all_trust and self.matter_trust_balance > 0


def fetch_matter_trust_balance(session: requests.Session, client_id: int) -> float:
    """Sums the Trust account_balances across every matter (any status) tied
    to one client — see MatterBillSummary.trust_level_mismatch for why.
    Only called per client with an open client-level trust request, not a
    firm-wide sweep — a handful of targeted calls, not a new page-load-wide
    matters fetch."""
    resp = session.get(MATTERS_ENDPOINT, params={
        "client_id": client_id, "status": MATTER_STATUSES_FOR_TRUST_CHECK, "fields": MATTER_TRUST_FIELDS,
    })
    if resp.status_code != 200:
        raise RuntimeError(f"Failed to fetch matters for client {client_id}: {resp.status_code} {resp.text[:200]}")
    matters = resp.json().get("data", [])
    return sum(
        (b.get("balance") or 0.0)
        for m in matters
        for b in (m.get("account_balances") or [])
        if b.get("type") == "Trust"
    )


def build_matter_summaries(bills: list[UnpaidBill]) -> list[MatterBillSummary]:
    """Groups unpaid bills into one summary per matter, sorted the same way
    the flat bill list used to be (most overdue first, then largest total)
    — used by both /collections and its print report. A bill with no matter
    linked can't be grouped with anything, so it becomes its own
    single-bill summary rather than being dropped or merged incorrectly."""
    by_matter: dict[int | None, list[UnpaidBill]] = defaultdict(list)
    next_unlinked_key = -1
    for b in bills:
        key = b.matter_id
        if key is None:
            key = next_unlinked_key
            next_unlinked_key -= 1
        by_matter[key].append(b)

    summaries = [
        MatterBillSummary(
            matter_id=matter_bills[0].matter_id,
            display_number=matter_bills[0].display_number,
            client_name=matter_bills[0].client_name,
            client_id=matter_bills[0].client_id,
            bills=sorted(matter_bills, key=lambda b: b.issued_at),
        )
        for matter_bills in by_matter.values()
    ]
    summaries.sort(key=lambda s: (-s.max_days_overdue, -s.total_balance))
    return summaries


def fetch_unpaid_bills(session: requests.Session) -> list[UnpaidBill]:
    """Every bill in state=awaiting_payment, one row per bill (not
    aggregated per matter) — staff following up on collections need to see
    which specific invoice is overdue, not just a matter-level total."""
    bills: list[UnpaidBill] = []
    next_url: str | None = None
    page = 1
    while True:
        if next_url:
            resp = session.get(next_url)
        else:
            resp = session.get(BILLS_ENDPOINT, params={"fields": BILLS_FIELDS, "state": BILL_STATE, "limit": PAGE_SIZE})
        if resp.status_code != 200:
            raise RuntimeError(f"Failed to fetch unpaid bills (page {page}): {resp.status_code} {resp.text[:200]}")
        body = resp.json()
        for b in body.get("data", []):
            client = b.get("client") or {}
            matters = b.get("matters") or []
            if len(matters) > 1:
                logging.warning("Bill %s has %d matters attached — using the first, expected 0 or 1", b.get("id"), len(matters))
            matter = matters[0] if matters else {}
            bills.append(UnpaidBill(
                bill_id=b["id"],
                number=b.get("number", ""),
                matter_id=matter.get("id"),
                display_number=matter.get("display_number", ""),
                client_id=client.get("id", 0),
                client_name=client.get("name", ""),
                issued_at=b.get("issued_at", ""),
                due_at=b.get("due_at"),
                total=float(b.get("total") or 0),
                balance=float(b.get("balance") or 0),
                kind=b.get("kind", ""),
            ))
        next_url = (body.get("meta") or {}).get("paging", {}).get("next")
        logging.info("Fetched unpaid bills page %d (%d so far)", page, len(bills))
        page += 1
        if not next_url:
            break
    return bills


def write_report_csv(bills: list[UnpaidBill], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Matter", "Client", "Bill #", "Type", "Issued", "Due", "Days Overdue", "Balance"])
        for b in sorted(bills, key=lambda b: (-b.days_overdue, -b.balance)):
            writer.writerow([
                b.display_number, b.client_name, b.number, b.trust_label or "Bill", b.issued_at, b.due_at or "",
                b.days_overdue if not b.is_trust_request else "", f"{b.balance:.2f}",
            ])


def write_action_report_csv(
    invoice_summaries: list[MatterBillSummary], trust_summaries: list[MatterBillSummary], path: Path,
) -> None:
    """CSV mirroring collections_action_report.html's own table exactly —
    same two sections in the same order (Invoices, then Trust Requests, each
    already alphabetized by the caller), same Handling/Confirmed columns.
    The page's one "Total Balance" column stacks up to three $ figures
    (earned/replenishment/new-trust) — split into three real columns here
    rather than one combined string, since a CSV's whole point is one value
    per cell."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "Section", "Matter", "Oldest Issued", "Earned Balance", "Replenishment Balance",
            "New Client Retainer Balance", "Handling", "Confirmed",
        ])
        for section, summaries in (("Invoice", invoice_summaries), ("Trust Request", trust_summaries)):
            for s in summaries:
                if s.action == "FLARPL":
                    confirmed = "Recorded" if s.flarpl_recorded else "Not recorded yet"
                elif s.action == "Payment plan":
                    confirmed = "In place" if s.payment_plan_active else "Not set up yet"
                else:
                    confirmed = ""
                matter = s.display_name
                if s.trust_level_mismatch:
                    matter += f" — CHECK TRUST LEVEL (matter holds ${s.matter_trust_balance:.2f})"
                writer.writerow([
                    section, matter, s.oldest_issued_at,
                    f"{s.earned_balance:.2f}" if s.earned_balance else "",
                    f"{s.replenishment_balance:.2f}" if s.replenishment_balance else "",
                    f"{s.new_trust_balance:.2f}" if s.new_trust_balance else "",
                    s.action, confirmed,
                ])


def build_session() -> requests.Session:
    if not ACCESS_TOKEN:
        raise RuntimeError("CLIO_ACCESS_TOKEN not set in .env")
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {ACCESS_TOKEN}"})
    return session


def run_pipeline(output_dir: Path = Path("output")) -> list[UnpaidBill]:
    setup_logging(Path("logs"))
    session = build_session()

    bills = fetch_unpaid_bills(session)
    overdue = [b for b in bills if b.overdue]
    total_balance = sum(b.balance for b in bills)

    today = datetime.today().strftime("%Y-%m-%d")
    output_dir.mkdir(exist_ok=True)
    report_path = output_dir / f"collections_monitor_{today}.csv"
    write_report_csv(bills, report_path)

    logging.info(
        "Checked %d unpaid bill(s), $%.2f total balance — %d past due. Report: %s",
        len(bills), total_balance, len(overdue), report_path,
    )
    return bills


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.parse_args()
    run_pipeline()


if __name__ == "__main__":
    main()
