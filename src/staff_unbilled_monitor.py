"""
staff_unbilled_monitor.py — Read-only view of unbilled activity by staff
member, filtered to the matters actually at risk of not getting collected —
where the client's Clio trust balance no longer covers the real WIP on that
matter. Answers "who is billing time that's at risk of not getting
collected?" — a per-user/per-matter cut Clio's own reporting doesn't offer
directly, and a different question from Trust Monitor's own $2,500-cushion
early warning (see below).

Per-row figures:
  - Unbilled Activity — this staff member's own unbilled work on this
    matter (GET /activities.json?status=unbilled, summed by user+matter).
    Clio's status enum keeps "unbilled" and "non_billable" as distinct
    values, so this is billable-only without any extra filtering.
  - Owed (Outstanding Bills) — the matter's own already-issued, unpaid
    bill balance (state=awaiting_payment), same figure
    collections_monitor.py reports per bill and trust_monitor.py calls
    "outstanding."
  - Matter WIP / Trust Balance — trust_monitor.py's own already-audited
    figures (unbilled_amount + draft/awaiting_approval bill totals; the
    matter's Trust account balance), reused directly rather than
    recomputed here.

The three matter-level figures above (Owed, Matter WIP, Trust Balance) are
NOT per-user splits — a Bill or a trust balance isn't tied to one specific
staff member's work, so if two staff both have unbilled activity on the
same matter, both rows show the same values. Summing those columns across
rows double-counts.

**At-risk filter (2026-08-17, Ted):** only staff/matter rows where the
matter's trust balance is LESS than its real WIP are included — i.e. the
retainer is already fully absorbed by work done, not just running low.
This is deliberately a sharper, buffer-free version of trust_monitor.py's
own "flagged" condition (cushion < $2,500) — that's an early-warning
buffer for matters that still have trust to spare; this is "the money to
cover this work may not be there at all." Rows that pass (trust still
covers WIP) are filtered out entirely rather than shown with a badge — the
stated goal was to remove staff/matters where retainer funds ARE
available, not merely flag them.

Only open matters are included (same convention as every other
subproject) — an activity attached to a non-open matter is skipped and
counted for the log summary rather than silently dropped. Matters
trust_monitor.py itself excludes (firm-overhead client, or genuinely zero
trust/WIP/outstanding footprint) are skipped here too for the same reason
— reusing trust_monitor.build_trust_statuses() directly rather than
reimplementing its exclusions.

Usage:
  uv run src/staff_unbilled_monitor.py
"""

import argparse
import csv
import logging
import os
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(ENV_PATH)

from matter_matching import fetch_open_matters  # noqa: E402
from trust_monitor import (  # noqa: E402
    BILL_OUTSTANDING_STATES,
    BILL_WIP_STATES,
    MATTERS_FIELDS,
    TrustStatus,
    build_trust_statuses,
    fetch_bills_by_matter,
    fetch_billable_matters_unbilled,
)

BASE_URL = os.getenv("CLIO_BASE_URL", "https://app.clio.com").rstrip("/")
ACCESS_TOKEN = os.getenv("CLIO_ACCESS_TOKEN", "")

ACTIVITIES_ENDPOINT = f"{BASE_URL}/api/v4/activities.json"
# user{id,name} and matter{id} are nested relationships — must be requested
# explicitly via subfields or they come back as stubs (same gotcha as
# Bradford's custom_rate, Court Calendar's Responsible Attorney, etc.)
ACTIVITIES_FIELDS = "id,type,total,matter{id},user{id,name}"
ACTIVITIES_STATUS = "unbilled"

PAGE_SIZE = 200


def setup_logging(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"staff_unbilled_monitor_{datetime.today().strftime('%Y%m%d')}.log"
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
class StaffUnbilledRow:
    user_id: int
    user_name: str
    matter_id: int
    display_number: str
    client_name: str
    total_unbilled: float
    total_owed: float  # matter-level, NOT per-user — see module docstring
    matter_wip: float  # matter-level — trust_monitor's WIP (unbilled + draft/awaiting-approval bills)
    matter_trust_balance: float  # matter-level — Clio Trust account balance

    @property
    def cushion(self) -> float:
        return self.matter_trust_balance - self.matter_wip

    @property
    def at_risk(self) -> bool:
        """Trust doesn't even cover WIP — see module docstring for why this
        is a stricter, buffer-free condition than trust_monitor.py's own
        $2,500-cushion "flagged" check."""
        return self.cushion < 0

    @property
    def shortfall(self) -> float:
        return max(0.0, -self.cushion)


@dataclass
class UserSummary:
    """One staff member's rollup — total_owed/total_shortfall here are sums
    across THIS user's own distinct matters (a legitimate "how much is
    outstanding/at-risk on what I'm working" figure), not a global per-matter
    split; summing either ACROSS users still double-counts a shared matter."""
    user_name: str
    total_unbilled: float
    total_owed: float
    total_shortfall: float
    matter_count: int
    rows: list[StaffUnbilledRow]


def fetch_unbilled_activities(session: requests.Session) -> list[dict]:
    """Every Activity in status=unbilled, raw dicts. Nothing else in this
    repo GETs Activities today (Legs/Bradford/Printer only ever POST new
    ones), so this is a new query, same pagination pattern as every other
    fetcher here."""
    activities: list[dict] = []
    next_url: str | None = None
    page = 1
    while True:
        if next_url:
            resp = session.get(next_url)
        else:
            resp = session.get(
                ACTIVITIES_ENDPOINT,
                params={"fields": ACTIVITIES_FIELDS, "status": ACTIVITIES_STATUS, "limit": PAGE_SIZE},
            )
        if resp.status_code != 200:
            raise RuntimeError(f"Failed to fetch unbilled activities (page {page}): {resp.status_code} {resp.text[:200]}")
        body = resp.json()
        activities.extend(body.get("data", []))
        next_url = (body.get("meta") or {}).get("paging", {}).get("next")
        logging.info("Fetched unbilled activities page %d (%d so far)", page, len(activities))
        page += 1
        if not next_url:
            break
    return activities


def build_rows(
    matters: list[dict],
    activities: list[dict],
    outstanding_by_matter: dict[int, float],
    trust_statuses: list[TrustStatus],
) -> list[StaffUnbilledRow]:
    """Returns every staff/matter row with unbilled activity — including
    ones where the matter's trust balance already covers the WIP. Callers
    that want the at-risk-only report should filter on `.at_risk` (see
    run_pipeline) rather than expecting this function to filter — keeping
    this the full, auditable dataset."""
    matter_index = {m["id"]: m for m in matters}
    status_by_matter = {s.matter_id: s for s in trust_statuses}

    unbilled_by_key: dict[tuple[int, int], float] = defaultdict(float)
    user_names: dict[int, str] = {}
    skipped_not_open = 0
    skipped_no_user = 0
    skipped_no_trust_status = 0

    for a in activities:
        matter = a.get("matter") or {}
        user = a.get("user") or {}
        mid = matter.get("id")
        uid = user.get("id")

        if uid is None:
            skipped_no_user += 1
            continue
        if mid is None or mid not in matter_index:
            skipped_not_open += 1
            continue

        unbilled_by_key[(uid, mid)] += float(a.get("total") or 0)
        user_names[uid] = user.get("name", "")

    if skipped_not_open:
        logging.info("Skipped %d unbilled activities on non-open (or unmatched) matters", skipped_not_open)
    if skipped_no_user:
        logging.info("Skipped %d unbilled activities with no user attached", skipped_no_user)

    rows: list[StaffUnbilledRow] = []
    for (uid, mid), total in unbilled_by_key.items():
        status = status_by_matter.get(mid)
        if status is None:
            # trust_monitor.build_trust_statuses() itself excludes the
            # firm-overhead client and genuinely zero-footprint matters —
            # neither is a real client to be "at risk of not collecting"
            # from, so skip rather than guess at trust/WIP figures for them.
            skipped_no_trust_status += 1
            continue

        m = matter_index[mid]
        client = m.get("client") or {}
        rows.append(StaffUnbilledRow(
            user_id=uid,
            user_name=user_names.get(uid, ""),
            matter_id=mid,
            display_number=m.get("display_number", ""),
            client_name=client.get("name", ""),
            total_unbilled=total,
            total_owed=outstanding_by_matter.get(mid, 0.0),
            matter_wip=status.unbilled_amount,
            matter_trust_balance=status.amount_in_trust,
        ))

    if skipped_no_trust_status:
        logging.info(
            "Skipped %d staff/matter rows with no trust_monitor status (firm-overhead client or zero financial footprint)",
            skipped_no_trust_status,
        )
    return rows


def build_user_summaries(rows: list[StaffUnbilledRow]) -> list[UserSummary]:
    """Groups rows into one summary per staff member, sorted by descending
    unbilled total — used by both the console log and the /staff-unbilled
    dashboard page's summary-row-expands-to-matters view."""
    by_user: dict[str, list[StaffUnbilledRow]] = defaultdict(list)
    for r in rows:
        by_user[r.user_name].append(r)

    summaries = [
        UserSummary(
            user_name=user_name,
            total_unbilled=sum(r.total_unbilled for r in user_rows),
            total_owed=sum(r.total_owed for r in user_rows),
            total_shortfall=sum(r.shortfall for r in user_rows),
            matter_count=len(user_rows),
            rows=sorted(user_rows, key=lambda r: -r.total_unbilled),
        )
        for user_name, user_rows in by_user.items()
    ]
    summaries.sort(key=lambda s: -s.total_unbilled)
    return summaries


def write_report_csv(rows: list[StaffUnbilledRow], path: Path) -> None:
    """Expects the already at-risk-filtered list — see run_pipeline."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "User", "Matter", "Client", "Unbilled Activity",
            "Matter WIP", "Trust Balance", "Shortfall", "Owed (Outstanding Bills)",
        ])
        for r in sorted(rows, key=lambda r: (r.user_name, -r.shortfall)):
            writer.writerow([
                r.user_name, r.display_number, r.client_name,
                f"{r.total_unbilled:.2f}",
                f"{r.matter_wip:.2f}", f"{r.matter_trust_balance:.2f}", f"{r.shortfall:.2f}",
                f"{r.total_owed:.2f}",
            ])


def build_session() -> requests.Session:
    if not ACCESS_TOKEN:
        raise RuntimeError("CLIO_ACCESS_TOKEN not set in .env")
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {ACCESS_TOKEN}"})
    return session


def run_pipeline(output_dir: Path = Path("output")) -> list[StaffUnbilledRow]:
    setup_logging(Path("logs"))
    session = build_session()

    matters = fetch_open_matters(session, fields=MATTERS_FIELDS)
    logging.info("Fetched %d open matters", len(matters))

    activities = fetch_unbilled_activities(session)
    never_billed = fetch_billable_matters_unbilled(session)
    bill_wip = fetch_bills_by_matter(session, BILL_WIP_STATES, "total")
    outstanding_by_matter = fetch_bills_by_matter(session, BILL_OUTSTANDING_STATES, "balance")
    trust_statuses = build_trust_statuses(matters, never_billed, bill_wip, outstanding_by_matter)

    all_rows = build_rows(matters, activities, outstanding_by_matter, trust_statuses)
    rows = [r for r in all_rows if r.at_risk]

    today = datetime.today().strftime("%Y-%m-%d")
    output_dir.mkdir(exist_ok=True)
    report_path = output_dir / f"staff_unbilled_{today}.csv"
    write_report_csv(rows, report_path)

    logging.info(
        "%d of %d staff/matter rows are at risk (trust doesn't cover WIP) across %d matter(s), "
        "of %d total matter(s) with unbilled activity. Report: %s",
        len(rows), len(all_rows), len({r.matter_id for r in rows}),
        len({r.matter_id for r in all_rows}), report_path,
    )
    for s in build_user_summaries(rows):
        logging.info(
            "  %-25s $%10.2f unbilled, $%10.2f shortfall, across %d at-risk matter(s)",
            s.user_name, s.total_unbilled, s.total_shortfall, s.matter_count,
        )

    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.parse_args()
    run_pipeline()


if __name__ == "__main__":
    main()
