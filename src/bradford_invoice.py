"""
bradford_invoice.py — Parse PL Bradford Law LLC invoice PDF and post time entries to Clio.

Two invoice formats handled:
  1. Attorney time (main invoice, pages 1-3)
       Item=Hours, Description=CLIENTNAME- M/D/YY- ACTIVITY
       Price: omitted from payload — Clio uses PAM's matter-defined rate
  2. Paralegal time (Clio export attachments, pages 4-8)
       Price: PARALEGAL_RATE ($150), regardless of what the attachment shows

Skipped:
  - ADMIN entries (not entered in Clio)
  - "PARALEGAL TIME ***SEE ATTACHED" summary lines (detail is on pages 4-8)

All entries posted under USER_ID_PAM (Pamela Bradford).

Usage:
  uv run src/bradford_invoice.py --input "data/Invoice-*.pdf" --dry-run
  uv run src/bradford_invoice.py --input "data/Invoice-*.pdf"
  uv run src/bradford_invoice.py --input "data/Invoice-*.pdf" --matter wells
"""

import argparse
import csv
import difflib
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import pdfplumber
import requests
from dotenv import load_dotenv

from matter_matching import fetch_open_matters, index_by_last_name

load_dotenv()

# Suppress pdfminer font-descriptor warnings — benign, not actionable
logging.getLogger("pdfminer").setLevel(logging.ERROR)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PARALEGAL_RATE = 150.0  # price sent to Clio for paralegal (Taijah Miles) entries
PAM_INITIALS   = "PB"   # Pamela Bradford — every entry posts under her, dashboard display only

BASE_URL       = os.getenv("CLIO_BASE_URL", "https://app.clio.com").rstrip("/")
ACCESS_TOKEN   = os.getenv("CLIO_ACCESS_TOKEN", "")
PAM_USER_ID    = int(os.getenv("USER_ID_PAM", "0"))
ACTIVITIES_URL = f"{BASE_URL}/api/v4/activities.json"
POST_DELAY     = 1.5  # seconds between POSTs — stay under 50 req/min

# Matters fields for the same fetch_open_matters() call bradford_invoice already
# makes — custom_rate{type,rates} requires the Clio app's Billing (Read)
# permission (granted 2026-07-22); the shallow selector is what actually works —
# a deeper `rates{rate,user}` selector is rejected by Clio's API as invalid, but
# `custom_rate{type,rates}` already returns each rate entry fully expanded
# (id, rate, user{id,name}) with no further subfield selection needed or accepted.
MATTERS_FIELDS_WITH_RATES = "id,display_number,custom_number,status,custom_rate{type,rates}"

# Invoice last-name → Clio matter ID.
# Add entries after a dry-run surfaces exceptions or ambiguous matches.
MANUAL_MATTER_MAP: dict[str, int] = {
    "HJERMSTAD":  1786831923,  # Clio: HJERMSTAD, HANS CHRISTIAN
    "LARSON":     1786834653,  # Clio: LARSEN, NOEL (typo on invoice)
    "VISWANATH":  1786847118,  # Clio: VISWANATHAN, VIDYA (name truncated on invoice)
    "BULTMIERE":  1786823793,  # Clio: BULTEMEIER, HANNAH (spelling varies on invoice)
    "WELLS":      1786847613,  # Clio: WELLS, ANDREW (BRITTNEY is closed but not yet in Clio)
}

# Overrides added live from the dashboard (exception's "Use suggested match" button
# or manual matter-ID entry) — a data file rather than editing this source file from
# a web request, same pattern as data/outlook_call_overrides.csv. Merged with
# MANUAL_MATTER_MAP at run time; the code constant above wins on conflict since it's
# the deliberately-reviewed one.
PERSISTED_MATTER_MAP_PATH = Path("data") / "bradford_manual_matter_map.csv"


def load_persisted_matter_map(path: Path = PERSISTED_MATTER_MAP_PATH) -> dict[str, int]:
    if not path.exists():
        return {}
    out: dict[str, int] = {}
    with open(path, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            name = (row.get("name") or "").strip().upper()
            matter_id = (row.get("matter_id") or "").strip()
            if name and matter_id.isdigit():
                out[name] = int(matter_id)
    return out


def save_persisted_override(name: str, matter_id: int, note: str = "",
                             path: Path = PERSISTED_MATTER_MAP_PATH) -> None:
    """Appends one override row, writing a header (and BOM, for Excel) only if
    the file doesn't exist yet — utf-8-sig on every open() would otherwise
    write a fresh BOM into the middle of the file on each append."""
    path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not path.exists()
    with open(path, "a", newline="", encoding="utf-8") as f:
        if is_new:
            f.write("﻿")
            csv.writer(f).writerow(["name", "matter_id", "note", "added_at"])
        csv.writer(f).writerow([
            name.strip().upper(), matter_id, note,
            datetime.now().isoformat(timespec="seconds"),
        ])
    logging.info("Persisted override: %s -> matter %s", name.strip().upper(), matter_id)


def effective_manual_matter_map() -> dict[str, int]:
    combined = load_persisted_matter_map()
    combined.update(MANUAL_MATTER_MAP)
    return combined


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class InvoiceEntry:
    client_name: str        # normalized last name for matter matching
    date: str               # ISO YYYY-MM-DD
    description: str        # activity note
    hours: float            # decimal hours
    source: str             # "attorney" or "paralegal"
    paralegal_rate: float | None  # None for attorney (Clio uses matter rate); 150.0 for paralegal


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"bradford_invoice_{datetime.today().strftime('%Y%m%d')}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        force=True,
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(
                open(sys.stdout.fileno(), mode="w", encoding="utf-8",
                     errors="replace", closefd=False)
            ),
        ],
    )
    logging.info("Log: %s", log_file)


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def parse_invoice_date(s: str) -> str | None:
    """Parse M/D/YY or M-D-YY (4-digit year also accepted) -> YYYY-MM-DD."""
    s = s.strip()
    for fmt in ("%m/%d/%y", "%m-%d-%y", "%m/%d/%Y", "%m-%d-%Y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return None


# ---------------------------------------------------------------------------
# Main invoice parsing — pages 1-3
#
# Strategy: try pdfplumber table extraction; fall back to line-by-line regex
# if a page yields no table rows.
# ---------------------------------------------------------------------------

# Description field pattern: NAME[-; ] DATE [-] ACTIVITY
_DESC_RE = re.compile(
    r'^([A-Z][A-Z ]*?)\s*[;,\-]?\s*(\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4})\s*[-]?\s*(.+)$',
    re.DOTALL,
)


def _parse_desc(desc: str, qty_s: str) -> InvoiceEntry | None:
    """
    Parse a main-invoice description + quantity string into an InvoiceEntry.
    Returns None for ADMIN, SEE ATTACHED, or unparseable rows.
    """
    desc = " ".join(desc.split())

    if desc.upper().startswith("ADMIN"):
        logging.debug("Skip ADMIN: %.60s", desc)
        return None
    if "PARALEGAL TIME" in desc.upper():
        logging.debug("Skip SEE ATTACHED: %.60s", desc)
        return None

    try:
        hours = float(qty_s.replace(",", ""))
    except ValueError:
        return None

    m = _DESC_RE.match(desc)
    if not m:
        logging.warning("Cannot parse description: %.80s", desc)
        return None

    client   = m.group(1).strip().rstrip(",;- ").strip().upper()
    date_iso = parse_invoice_date(m.group(2))
    activity = m.group(3).strip()

    if not date_iso:
        logging.warning("Cannot parse date in: %.80s", desc)
        return None

    return InvoiceEntry(
        client_name=client,
        date=date_iso,
        description=activity,
        hours=hours,
        source="attorney",
        paralegal_rate=None,  # Clio uses PAM's matter rate
    )


# Matches a full invoice row: "Hours DESC  PRICE  HRS  TOTAL"
# All three trailing numbers are required; desc may be truncated (multi-line PDFs
# split long descriptions across lines — we use what appears on the Hours line).
_LINE_RE = re.compile(
    r'^Hours\s+(.+?)\s+([\d,]+\.\d{2})\s+([\d.]+)\s+([\d,]+\.\d{2})\s*$'
)


_SECTION_STARTS = ("Hours", "Subtotal", "Total", "Amount", "Balance", "Item", "PL ")


def parse_main_invoice(pdf: pdfplumber.PDF) -> list[InvoiceEntry]:
    """
    Parse attorney time entries from invoice pages 1-3.

    Uses extract_text() line-by-line (not extract_table) because pdfplumber's
    table detection on invoices with header content returns partial rows.
    Continuation lines (description text that wraps in the PDF) are joined onto
    the Hours line so the full activity note is preserved.
    """
    entries: list[InvoiceEntry] = []

    for page_idx in range(min(3, len(pdf.pages))):
        lines = (pdf.pages[page_idx].extract_text() or "").splitlines()
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            m = _LINE_RE.match(line)
            if not m:
                i += 1
                continue

            desc_part = m.group(1)
            qty_s     = m.group(3)

            # Append continuation lines until the next "Hours" row or section header
            j = i + 1
            while j < len(lines):
                nxt = lines[j].strip()
                if not nxt or any(nxt.startswith(s) for s in _SECTION_STARTS):
                    break
                desc_part += " " + nxt
                j += 1

            i = j
            entry = _parse_desc(desc_part, qty_s)
            if entry:
                entries.append(entry)

    logging.info("Parsed %d attorney entries from main invoice", len(entries))
    return entries


# ---------------------------------------------------------------------------
# Paralegal page parsing — pages 4-8
#
# Clio "Time entries" export columns (0-based):
#   0: Date  |  2: Duration  |  3: Description  |  8: Case
# Rate overridden to PARALEGAL_RATE for all entries.
# ---------------------------------------------------------------------------

def _client_from_case(case: str) -> str:
    """
    Extract client last name from a Clio matter title.
      'Ron Joyce v. Ellen Joyce (San Diego)' -> 'JOYCE'
      'Viswanathan, Vidya'                  -> 'VISWANATHAN'
      'Andrew Wells v. Christina Smith'     -> 'WELLS'
    """
    case = case.strip()
    if not case:
        return ""
    if "," in case and " v." not in case.lower():
        return case.split(",")[0].strip().upper()
    m = re.match(r'^\w+\s+(\w+)\s+v\.', case, re.IGNORECASE)
    if m:
        return m.group(1).upper()
    return case.split()[0].upper()


def parse_paralegal_pages(pdf: pdfplumber.PDF) -> list[InvoiceEntry]:
    entries: list[InvoiceEntry] = []

    for page_idx in range(3, min(8, len(pdf.pages))):
        page  = pdf.pages[page_idx]
        table = page.extract_table()
        current_client = ""

        if table:
            for row in table:
                if not row:
                    continue
                date_s = (row[0] or "").strip()
                if date_s.lower() in ("date", "date ▲", ""):
                    continue
                dur_s = (row[2] or "").strip() if len(row) > 2 else ""
                desc  = " ".join((row[3] or "").split()) if len(row) > 3 else ""
                case  = (row[8] or "").strip() if len(row) > 8 else ""

                if case:
                    current_client = _client_from_case(case)
                if not date_s or not dur_s or not current_client:
                    continue

                try:
                    date_iso = datetime.strptime(date_s, "%b %d, %Y").strftime("%Y-%m-%d")
                    hours    = float(dur_s)
                except ValueError:
                    logging.debug("Paralegal page %d: skip non-data row '%s' / '%s'",
                                  page_idx + 1, date_s, dur_s)
                    continue

                entries.append(InvoiceEntry(
                    client_name=current_client,
                    date=date_iso,
                    description=desc,
                    hours=hours,
                    source="paralegal",
                    paralegal_rate=PARALEGAL_RATE,
                ))
        else:
            # Fallback: text-line parsing
            text = page.extract_text() or ""
            for line in text.splitlines():
                # Grab case from "v." lines to set current_client
                case_m = re.search(r'\b(\w+)\s+v\.\s+\w+', line)
                if case_m:
                    current_client = case_m.group(1).upper()
                    continue
                comma_m = re.match(r'^([A-Z][a-z]+),\s+[A-Z]', line)
                if comma_m:
                    current_client = comma_m.group(1).upper()
                    continue

                tm = re.match(
                    r'^([A-Z][a-z]{2}\s+\d{1,2},\s+\d{4})\s+\S.*?\s+([\d.]+)\s+(.+?)\s+\$[\d.]+/hr',
                    line.strip()
                )
                if not tm or not current_client:
                    continue
                try:
                    date_iso = datetime.strptime(tm.group(1), "%b %d, %Y").strftime("%Y-%m-%d")
                    hours    = float(tm.group(2))
                except ValueError:
                    continue
                entries.append(InvoiceEntry(
                    client_name=current_client,
                    date=date_iso,
                    description=tm.group(3).strip(),
                    hours=hours,
                    source="paralegal",
                    paralegal_rate=PARALEGAL_RATE,
                ))

    logging.info("Parsed %d paralegal entries from attached pages", len(entries))
    return entries


# ---------------------------------------------------------------------------
# Fuzzy suggestions for unmatched names
#
# The contractor (Bradford) frequently misspells or truncates client last
# names on the invoice — real examples: "LARSON" for LARSEN, "BULTMIERE" and
# separately "BULTIMIER" for the same BULTEMEIER client on different invoices.
# Per this project's philosophy, an unmatched name still fails loud rather
# than auto-posting to a guess — but attaching the closest real open-matter
# name to the exceptions file turns "search Clio by hand" into a copy-paste
# into MANUAL_MATTER_MAP, which is where the actual decision stays.
# ---------------------------------------------------------------------------

SUGGESTION_CUTOFF = 0.6  # difflib similarity ratio; below this, no suggestion offered


def suggest_match(name: str, matters: dict[str, int | None]) -> tuple[str, int | None] | None:
    """Closest open-matter last name to an unmatched invoice name, plus its
    matter ID (None if that name is itself ambiguous). None if nothing close
    enough to be worth suggesting."""
    candidates = difflib.get_close_matches(name, matters.keys(), n=1, cutoff=SUGGESTION_CUTOFF)
    if not candidates:
        return None
    best = candidates[0]
    return best, matters[best]


# ---------------------------------------------------------------------------
# Pam's actual per-matter rate — display only, never sent to Clio
#
# Attorney entries still omit "price" from the real POST payload (Clio applies
# the matter-defined rate itself) — this is purely so the dashboard can show
# what that rate actually is, e.g. to make it visually obvious when paralegal
# work ($150 flat) is billed lower than Pam's own matter-defined rate.
# ---------------------------------------------------------------------------

def index_pam_rate_by_matter_id(matters_raw: list[dict], pam_user_id: int) -> dict[int, float]:
    rates: dict[int, float] = {}
    for m in matters_raw:
        custom_rate = m.get("custom_rate") or {}
        for entry in custom_rate.get("rates") or []:
            user = entry.get("user") or {}
            if user.get("id") == pam_user_id and entry.get("rate") is not None:
                rates[int(m["id"])] = float(entry["rate"])
                break
    return rates


def fetch_pam_standard_rate(session: requests.Session, pam_user_id: int) -> float | None:
    """Pam's standard/default hourly rate (Clio User.rate) — confirmed live
    2026-07-23 against real data (Ted $150, Dalinah $200, Sandy $300, Pam $450,
    all matching known figures from the firm's rate history). Used as the
    fallback when a matter has no matter-level custom_rate override for her —
    the firm is moving to a standard-rate system rather than maintaining
    custom per-matter rates going forward, so this is the expected default now,
    not just a display curiosity for the "rate unknown" cases."""
    resp = session.get(f"{BASE_URL}/api/v4/users/{pam_user_id}.json", params={"fields": "id,rate"})
    if resp.status_code != 200:
        logging.warning("Could not fetch Pam's standard rate: %s %s", resp.status_code, resp.text[:200])
        return None
    rate = resp.json().get("data", {}).get("rate")
    return float(rate) if rate is not None else None


# ---------------------------------------------------------------------------
# Build Clio API payloads
# ---------------------------------------------------------------------------

def build_payloads(
    entries: list[InvoiceEntry],
    matters: dict[str, int | None],
    pam_user_id: int,
    manual_map: dict[str, int] | None = None,
    pam_rates: dict[int, float] | None = None,
    pam_standard_rate: float | None = None,
) -> tuple[list[dict], list[dict]]:
    manual_map = MANUAL_MATTER_MAP if manual_map is None else manual_map
    pam_rates = pam_rates or {}
    payloads: list[dict] = []
    exceptions: list[dict] = []

    for e in entries:
        name = e.client_name

        if name in manual_map:
            matter_id: int | None = manual_map[name]
            logging.info("[%-10s] %-14s %s  %.2fh  manual override -> %s",
                         e.source, name, e.date, e.hours, matter_id)
        elif name not in matters:
            suggestion = suggest_match(name, matters)
            exceptions.append({
                "name": name, "date": e.date, "hours": e.hours,
                "source": e.source, "reason": "No matching open matter",
                "suggested_match": suggestion[0] if suggestion else "",
                "suggested_matter_id": suggestion[1] if suggestion and suggestion[1] else "",
            })
            if suggestion:
                logging.warning("[%-10s] %-14s %s  %.2fh  NO MATCH — did you mean %s? (matter %s)",
                                 e.source, name, e.date, e.hours, suggestion[0], suggestion[1] or "ambiguous")
            else:
                logging.warning("[%-10s] %-14s %s  %.2fh  NO MATCH", e.source, name, e.date, e.hours)
            continue
        elif matters[name] is None:
            exceptions.append({"name": name, "date": e.date, "hours": e.hours,
                                "source": e.source,
                                "reason": "Ambiguous — multiple open matters; add ID to MANUAL_MATTER_MAP"})
            logging.warning("[%-10s] %-14s %s  %.2fh  AMBIGUOUS", e.source, name, e.date, e.hours)
            continue
        else:
            matter_id = matters[name]
            logging.info("[%-10s] %-14s %s  %.2fh  -> matter %s",
                         e.source, name, e.date, e.hours, matter_id)

        # Round to firm standard of 0.1-hour increments using half-up rounding.
        # Python's built-in round() uses banker's rounding (0.25 → 0.2), which
        # diverges from Clio's own rounding (0.25 → 0.3). Decimal ROUND_HALF_UP
        # matches Clio and standard legal billing practice.
        hours_billed = float(
            Decimal(str(e.hours)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
        )
        if hours_billed != e.hours:
            logging.info("Rounded %s %s: %.4fh -> %.1fh", e.client_name, e.date, e.hours, hours_billed)

        data: dict = {
            "type":     "TimeEntry",
            "date":     e.date,
            "matter":   {"id": matter_id},
            "user":     {"id": pam_user_id},
            "quantity": round(hours_billed * 3600),  # Clio API v4: seconds
            "note":     e.description,
        }
        # Attorney entries: omit price — Clio applies PAM's matter rate
        # Paralegal entries: explicit $150/hr
        if e.paralegal_rate is not None:
            data["price"] = e.paralegal_rate

        # display_rate/posted_by are dashboard-only — never sent to Clio (see
        # post_entry, which POSTs payload["data"] alone, not this whole dict).
        # For attorney entries: matter-level custom_rate if that matter has one
        # on file for Pam, else her standard rate (User.rate) — the firm is
        # moving toward relying on the standard rate rather than maintaining
        # per-matter overrides, so this is the real expected value now, not a
        # fallback for missing data. Explicit "is not None" check because a
        # genuine $0 custom rate (e.g. pro bono) must not be replaced by the
        # standard rate. Paralegal entries just reuse the flat $150 in "price".
        if e.paralegal_rate is not None:
            display_rate = e.paralegal_rate
        else:
            matter_rate = pam_rates.get(matter_id)
            display_rate = matter_rate if matter_rate is not None else pam_standard_rate

        payloads.append({"data": data, "display_rate": display_rate, "posted_by": PAM_INITIALS})

    return payloads, exceptions


# ---------------------------------------------------------------------------
# Post to Clio
# ---------------------------------------------------------------------------

def post_entry(session: requests.Session, payload: dict) -> bool:
    d            = payload["data"]
    matter_id    = d["matter"]["id"]
    note_preview = d["note"][:50]

    for attempt in range(1, 3):
        # Send {"data": ...} only — payload also carries display_rate/posted_by
        # for the dashboard, which must never reach Clio's API.
        resp = session.post(ACTIVITIES_URL, json={"data": d})
        if resp.status_code in (200, 201):
            activity_id = resp.json().get("data", {}).get("id", "?")
            logging.info("POSTED  matter=%s  activity=%s  %s",
                         matter_id, activity_id, note_preview)
            return True
        elif resp.status_code == 429:
            wait = 60
            m = re.search(r"Retry in (\d+) seconds", resp.text)
            if m:
                wait = int(m.group(1)) + 2
            logging.warning("RATE LIMITED  matter=%s  waiting %ds (attempt %d/2)",
                            matter_id, wait, attempt)
            time.sleep(wait)
        else:
            logging.error("FAILED  matter=%s  status=%s  body=%s",
                          matter_id, resp.status_code, resp.text[:300])
            return False

    logging.error("FAILED  matter=%s  gave up after 2 attempts", matter_id)
    return False


# ---------------------------------------------------------------------------
# Pipeline (shared by the CLI and the web dashboard)
# ---------------------------------------------------------------------------

@dataclass
class RunResult:
    stem: str
    payloads: list[dict] = field(default_factory=list)
    exceptions: list[dict] = field(default_factory=list)
    total_entries: int = 0
    posted: int = 0
    failed: int = 0
    payloads_path: Path | None = None
    exceptions_path: Path | None = None
    matter_names: dict[int, str] = field(default_factory=dict)  # matter_id -> last name, for UI


def run_pipeline(
    input_path: Path,
    dry_run: bool,
    matter_filter: str = "",
    output_dir: Path = Path("output"),
) -> RunResult:
    """
    Parse the Bradford invoice PDF, match to Clio matters, write
    payload/exception output files, and (unless dry_run) POST time entries.

    Raises FileNotFoundError / RuntimeError on hard failures instead of
    exiting the process, so it's safe to call from a long-running server.
    """
    if not ACCESS_TOKEN:
        raise RuntimeError("CLIO_ACCESS_TOKEN not set in .env")
    if not PAM_USER_ID:
        raise RuntimeError("USER_ID_PAM not set in .env")
    if not input_path.exists():
        raise FileNotFoundError(f"PDF not found: {input_path}")

    logging.info("Reading %s", input_path)
    with pdfplumber.open(input_path) as pdf:
        attorney_entries = parse_main_invoice(pdf)
        paralegal_entries = parse_paralegal_pages(pdf)

    all_entries = attorney_entries + paralegal_entries
    logging.info("Total entries: %d attorney + %d paralegal = %d",
                 len(attorney_entries), len(paralegal_entries), len(all_entries))

    if matter_filter:
        f = matter_filter.upper()
        all_entries = [e for e in all_entries if f in e.client_name]
        logging.info("--matter '%s' matched %d entries", matter_filter, len(all_entries))
        if not all_entries:
            raise RuntimeError(f"No entries matched --matter '{matter_filter}'")

    session = requests.Session()
    session.headers.update({
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type":  "application/json",
    })

    matters_raw = fetch_open_matters(session, fields=MATTERS_FIELDS_WITH_RATES)
    matters = index_by_last_name(matters_raw)
    pam_rates = index_pam_rate_by_matter_id(matters_raw, PAM_USER_ID)
    pam_standard_rate = fetch_pam_standard_rate(session, PAM_USER_ID)
    payloads, exceptions = build_payloads(
        all_entries, matters, PAM_USER_ID, effective_manual_matter_map(), pam_rates, pam_standard_rate
    )

    output_dir.mkdir(exist_ok=True)
    stem = re.sub(r"[^\w\-]", "_", input_path.stem)
    matter_names = {mid: name for name, mid in matters.items() if mid}
    result = RunResult(stem=stem, payloads=payloads, exceptions=exceptions,
                        total_entries=len(all_entries), matter_names=matter_names)

    result.payloads_path = output_dir / f"{stem}_payloads.json"
    with open(result.payloads_path, "w", encoding="utf-8") as f:
        json.dump(payloads, f, indent=2)
    logging.info("Wrote %d payloads -> %s", len(payloads), result.payloads_path)

    if exceptions:
        result.exceptions_path = output_dir / f"{stem}_exceptions.csv"
        with open(result.exceptions_path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=[
                "name", "date", "hours", "source", "reason",
                "suggested_match", "suggested_matter_id",
            ])
            w.writeheader()
            w.writerows(exceptions)
        logging.warning("Wrote %d exceptions -> %s", len(exceptions), result.exceptions_path)

    total_hrs = sum(p["data"]["quantity"] / 3600 for p in payloads)
    logging.info(
        "Summary: %d entries / %d payloads / %d exceptions  |  %.2f total hours",
        len(all_entries), len(payloads), len(exceptions), total_hrs,
    )

    if not payloads or dry_run:
        if dry_run and payloads:
            logging.info("--- DRY RUN: no entries posted ---")
        return result

    for payload in payloads:
        if post_entry(session, payload):
            result.posted += 1
        else:
            result.failed += 1
        time.sleep(POST_DELAY)

    logging.info("Done: %d posted, %d failed", result.posted, result.failed)
    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--input",   required=True, help="Path to Bradford invoice PDF")
    ap.add_argument("--dry-run", action="store_true",
                    help="Parse, match, and write payloads — do not POST")
    ap.add_argument("--matter",  default="",
                    help="Process only entries whose client name contains this string")
    args = ap.parse_args()

    setup_logging(Path("logs"))

    try:
        result = run_pipeline(Path(args.input), dry_run=args.dry_run, matter_filter=args.matter)
    except (FileNotFoundError, RuntimeError) as e:
        logging.error(str(e))
        sys.exit(1)

    if result.failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
