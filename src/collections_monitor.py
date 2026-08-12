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
BILLS_FIELDS = "id,number,issued_at,due_at,total,balance,client{id,name},matters{id,display_number}"
BILL_STATE = "awaiting_payment"

PAGE_SIZE = 200


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

    @property
    def days_overdue(self) -> int:
        if not self.due_at:
            return 0
        due = datetime.strptime(self.due_at, "%Y-%m-%d").date()
        return max(0, (date.today() - due).days)

    @property
    def overdue(self) -> bool:
        return self.days_overdue > 0


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
        writer.writerow(["Matter", "Client", "Bill #", "Issued", "Due", "Days Overdue", "Balance"])
        for b in sorted(bills, key=lambda b: (-b.days_overdue, -b.balance)):
            writer.writerow([
                b.display_number, b.client_name, b.number, b.issued_at, b.due_at or "",
                b.days_overdue, f"{b.balance:.2f}",
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
