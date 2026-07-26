"""
legs_expenses.py — Parse Legs Legal Support, Inc. monthly statement PDF and
post pass-through costs (process serving, filing, delivery, copies) to Clio
as ExpenseEntry activities, billed at cost (no markup).

The PDF has two page types:
  - Statement pages (first few): every invoice number + amount for the
    month, no client names. This is the authoritative dollar-amount source
    (see "Why the statement, not the per-page Total" below).
  - Invoice pages (the rest): one per invoice, one client per page. The
    client identifier is the last non-boilerplate line of the page (a bare
    last name, or a case caption like "ROGERS V KRINSKY" — our client
    listed first).

Classification is content-based, not a hardcoded page-count split or a
"Statement"/"Invoice" heading-word check — tested against the real sample
PDF and found that heading word drops out of OCR entirely on some invoice
pages that otherwise have perfectly good content. A page is a Statement
page if it contains at least one recognizable statement-table row;
otherwise it's an Invoice page if it has any real content at all.

This PDF has ZERO embedded text (confirmed: pdfplumber extracts 0 characters
per page — it's a scanned/faxed image, one image per page), unlike
bradford_invoice.py's PDF which has a real text layer. Everything here goes
through local Tesseract OCR (pytesseract) instead of pdfplumber.extract_text().

Why the statement, not the per-page Total, is the dollar-amount source:
tested against the real sample PDF and found the per-page "Total" field is
NOT reliably OCR'able — its vertical position moves with how many line
items precede it, and it came out garbled or missing entirely on multiple
real pages (e.g. "Total �", or absent altogether). The Statement page's
table, by contrast, OCR'd cleanly and completely on every row across both
pages of the sample. So each invoice page is only used for its client
identifier and a human-readable note; the dollar amount is looked up from
the statement by invoice number.

Why a small header-crop, not full-page OCR, for the invoice number: tested
both. Full-page OCR only recovered the "Invoice #" field on ~60% of sample
pages (page segmentation sometimes drops or garbles it entirely). Cropping
just that fixed-position header box and re-OCRing with `--psm 6` (uniform
block of text) recovered it on 100% of the same sample pages. The header box
sits at a consistent position because these are stable-template scans (the
crop fractions below were measured against the real sample and left with
margin for minor scan jitter).

OCR noise: the "#L" prefix before an invoice number frequently misreads as
"41" or "1" (e.g. "L606098" -> "41606098", "L606191" -> "1606191") — the
actual 6 digits themselves came through clean in every case tested, so
invoice numbers are normalized to "last 6 digits of whatever digit run was
found", not matched as an exact "L######" string.

Skipped (firm overhead, not billed to any client): the monthly retainer line
item ("MONTHLY RETAINER- SAN DIEGO COUNTY COURTS...") — detected by the word
"RETAINER" in the page body, same treatment as bradford_invoice.py's ADMIN-
entry skip (an explicit, named category — not a silent drop, not a generic
exception).

Reconciliation: since the statement independently lists every invoice's
amount, every parsed invoice (matched, exception, or firm-overhead) is
checked against it — a missing/mismatched amount surfaces as a loud warning
rather than silently under- or over-billing a client.

Usage:
  uv run src/legs_expenses.py --input "data/June 2026.pdf" --dry-run
  uv run src/legs_expenses.py --input "data/June 2026.pdf"
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
from pathlib import Path

import pdfplumber
import pytesseract
import requests
from dotenv import load_dotenv

from court_calendar.normalizer import PURPOSE_CODES, party_names_match
from matter_matching import fetch_open_matters, index_by_last_name

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_URL       = os.getenv("CLIO_BASE_URL", "https://app.clio.com").rstrip("/")
ACCESS_TOKEN   = os.getenv("CLIO_ACCESS_TOKEN", "")
ACTIVITIES_URL = f"{BASE_URL}/api/v4/activities.json"
POST_DELAY     = 1.5  # seconds between POSTs — stay under 50 req/min

OCR_RESOLUTION = 200  # dpi for page rendering — matches the resolution these parsing rules were tuned against

# custom_field_values{...} is needed for the Opposing Party fallback below —
# not a plain field on the base Matter resource, same nested-selector gotcha
# as court_calendar/matter_fields.py's Court Case Number.
MATTERS_FIELDS_WITH_OP = "id,display_number,custom_number,status,custom_field_values{field_name,value}"

# Header box holding "Date" + "Invoice #", as fractions of page width/height —
# measured against the real sample PDF, with margin for scan jitter. Only
# meaningful on Invoice pages (Statement pages have a Date but no per-invoice
# number there).
HEADER_CROP_FRACTIONS = (0.65, 0.08, 1.0, 0.16)  # (left, top, right, bottom)

# Invoice last-name/caption → Clio matter ID.
# Add entries after a dry-run surfaces exceptions or ambiguous matches.
MANUAL_MATTER_MAP: dict[str, int] = {}

# Client identifiers that are never a real client — Legs falls back to the
# firm's own attorney name when an invoice has no distinct client attached
# (real example, 2026-06 statement: a "FILE IN RECORDERS OFFICE" invoice
# with no case caption came through as a bare "COLLIER" — overhead/
# non-billable, not a misread client name). Treated the same as the
# MONTHLY RETAINER line: excluded from client billing, shown in the dry-run
# preview's "Firm overhead" table rather than as an exception needing
# resolution every month.
FIRM_OVERHEAD_IDENTIFIERS = {"COLLIER"}

# Overrides added live from the dashboard (exception's "Use suggested match"
# button or manual matter-ID entry) — same pattern as
# data/bradford_manual_matter_map.csv. Merged with MANUAL_MATTER_MAP at run
# time; the code constant wins on conflict since it's the deliberately-
# reviewed one.
PERSISTED_MATTER_MAP_PATH = Path("data") / "legs_manual_matter_map.csv"


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
# Tesseract setup — fail loud with a clear install pointer, not a cryptic
# downstream error, if it's missing.
# ---------------------------------------------------------------------------

_TESSERACT_CANDIDATES = [
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
]


def ensure_tesseract() -> None:
    try:
        pytesseract.get_tesseract_version()
        return
    except Exception:
        pass

    for candidate in _TESSERACT_CANDIDATES:
        if Path(candidate).exists():
            pytesseract.pytesseract.tesseract_cmd = candidate
            try:
                pytesseract.get_tesseract_version()
                return
            except Exception:
                pass

    raise RuntimeError(
        "Tesseract OCR not found. Install it (e.g. `winget install --id "
        "UB-Mannheim.TesseractOCR -e`) or set "
        "pytesseract.pytesseract.tesseract_cmd to its install path."
    )


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class ParsedInvoice:
    invoice_number: str      # normalized 6 digits
    page_number: int         # 1-based PDF page, for tracing back to the source
    date: str | None         # ISO YYYY-MM-DD, None if unreadable
    client_raw: str | None   # last non-boilerplate line, None if not found
    note_text: str           # human-readable service description for the Clio note
    statement_amount: float | None  # looked up from the Statement pages
    is_firm_overhead: bool


# ---------------------------------------------------------------------------
# OCR helpers
# ---------------------------------------------------------------------------

def ocr_page_text(page: pdfplumber.page.Page) -> str:
    img = page.to_image(resolution=OCR_RESOLUTION).original
    return pytesseract.image_to_string(img)


def ocr_header_crop(page: pdfplumber.page.Page) -> str:
    """OCRs just the Date/Invoice# header box in isolation — far more
    reliable than pulling it out of the full-page text (see module
    docstring)."""
    img = page.to_image(resolution=OCR_RESOLUTION).original
    w, h = img.size
    left_f, top_f, right_f, bottom_f = HEADER_CROP_FRACTIONS
    box = (int(w * left_f), int(h * top_f), int(w * right_f), int(h * bottom_f))
    crop = img.crop(box)
    return pytesseract.image_to_string(crop, config="--psm 6")


THUMBNAIL_RESOLUTION = 100  # dpi — plenty for a visual sanity-check thumbnail, renders fast


def save_page_thumbnail(page: pdfplumber.page.Page, dest: Path) -> None:
    """Saves a small JPEG of the page so a human can visually spot-check an
    OCR'd row against the real scanned invoice — this PDF has zero embedded
    text, so OCR errors are a real, expected risk (see module docstring)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    img = page.to_image(resolution=THUMBNAIL_RESOLUTION).original.convert("RGB")
    img.save(dest, "JPEG", quality=70)


# ---------------------------------------------------------------------------
# Page classification and parsing
# ---------------------------------------------------------------------------

_STATEMENT_LINE_RE = re.compile(
    r"INV\s*#?\s*([A-Z0-9]{6,10})\.?\s*(?:Orig\.?\s*)?Amount\D*?([\d,]+\.\d{2})",
    re.IGNORECASE,
)


def classify_page(text: str) -> str:
    """'statement' | 'invoice' | 'unknown'.

    Not based on the "Statement"/"Invoice" heading word — tested against the
    real sample PDF and found that heading OCR drops out entirely on some
    invoice pages that otherwise have perfectly good content (page
    segmentation issue, not a content problem). Instead: a page is a
    Statement page if it contains at least one recognizable
    'INV #...Amount $X.XX' row; otherwise it's an Invoice page if it has any
    real content at all; otherwise unknown (genuinely blank/unreadable)."""
    if _STATEMENT_LINE_RE.search(text):
        return "statement"
    if len(text.strip()) > 20:
        return "invoice"
    return "unknown"


def _normalize_invoice_number(raw: str) -> str:
    """The '#L' prefix before an invoice number frequently OCRs as garbage
    ('L606098' -> '41606098', 'L606191' -> '1606191') but the trailing 6
    digits themselves come through clean — see module docstring."""
    digits = re.sub(r"\D", "", raw)
    return digits[-6:] if len(digits) >= 6 else digits


def parse_statement_page(text: str) -> dict[str, float]:
    """Invoice number (normalized) -> dollar amount, from a Statement page's
    'INV #L###### . Orig. Amount $X.XX.' rows."""
    amounts: dict[str, float] = {}
    for line in text.splitlines():
        m = _STATEMENT_LINE_RE.search(line)
        if not m:
            continue
        invoice_number = _normalize_invoice_number(m.group(1))
        amount = float(m.group(2).replace(",", ""))
        if invoice_number:
            amounts[invoice_number] = amount
    return amounts


# Token allows an embedded "." (real example: "1.606066" — OCR occasionally
# inserts a stray period mid-number); _normalize_invoice_number strips it
# along with any other non-digit noise.
_HEADER_RE = re.compile(r"(\d{1,2}/\d{1,2}/\d{4})\s*\|?\s*([A-Za-z0-9.]{5,12})")


def parse_header_crop(text: str) -> tuple[str | None, str | None]:
    """(date_iso, invoice_number) from the cropped header box's OCR text."""
    m = _HEADER_RE.search(text)
    if not m:
        return None, None
    try:
        date_iso = datetime.strptime(m.group(1), "%m/%d/%Y").strftime("%Y-%m-%d")
    except ValueError:
        date_iso = None
    invoice_number = _normalize_invoice_number(m.group(2))
    return date_iso, invoice_number or None


# Lines that are always vendor/firm boilerplate, never a client identifier —
# extend this if a future invoice's letterhead or footer text changes.
_BOILERPLATE_RE = re.compile(
    r"LEGS LEGAL SUPPORT|P\.?\s*O\.?\s*BOX|VISTA,?\s*CA|HEIDI COLLIER|"
    r"SECOND AVE|ESCONDIDO|BILL TO|DUE ON RECEIPT|INVOICE\s*#|^INVOICE$|"
    r"^STATEMENT$|QUANTITY|DESCRIPTION|^RATE$|^AMOUNT$|^TOTAL\b|PHONE\s*#|"
    r"^\d{3}-\d{3}-\d{4}$|^PAGE\s*\d+$|P\.O\.\s*NO|^TERMS$|^PROJECT$",
    re.IGNORECASE,
)

# A plausible client identifier: mostly capital letters/punctuation, no
# digits — rejects stray numbers and OCR'd table-border noise (e.g. "a ee",
# "| mmm | |") while matching bare last names and "X V Y" case captions.
_NAME_SHAPE_RE = re.compile(r"^[A-Z][A-Z&/.,'\-\s]{3,60}$")

# Court hearing purpose codes (RFO, FRC, MSC, ...) sometimes appear as their
# own trailing line AFTER the real client identifier (real example: a
# CROSSON V SAMUELS invoice with "RFO" on the line below it) — reused from
# court_calendar so a purpose code doesn't get mistaken for the name itself.
_PURPOSE_CODE_LINE_RE = re.compile(
    r"^(?:" + "|".join(re.escape(c) for c in PURPOSE_CODES) + r")$", re.IGNORECASE,
)


def extract_client_identifier(text: str) -> str | None:
    """Last non-blank, non-boilerplate, name-shaped line in the page —
    empirically the client identifier's position on every sample page
    checked, even on pages where the numeric table got scrambled elsewhere
    in the OCR output (see module docstring)."""
    for line in reversed(text.splitlines()):
        candidate = line.strip()
        if not candidate:
            continue
        if _BOILERPLATE_RE.search(candidate) or _PURPOSE_CODE_LINE_RE.match(candidate):
            continue
        if _NAME_SHAPE_RE.match(candidate):
            return candidate
    return None


def client_identifier_candidates(identifier: str) -> list[str]:
    """A case caption doesn't reliably say which side is our client — real
    example: "CROSSON V SAMUELS" is filed in Clio as SAMUELS, not the
    first-listed party (a mediation/divorce matter can be opened under
    either name). So a caption yields both names as candidates, in listed
    order, and the caller tries each until one resolves to a matter.
    'ROGERS V KRINSKY' -> ['ROGERS', 'KRINSKY']; 'TANGUAY' -> ['TANGUAY'];
    'COMEAU/GEVRY' -> ['COMEAU', 'GEVRY']."""
    upper = identifier.upper().strip()
    m = re.search(r"\bV\.?\s+", upper)
    if m:
        first = upper[: m.start()].strip()
        second = re.split(r"[,/]", upper[m.end():])[0].strip()
        return [first, second] if second else [first]
    parts = [p.strip() for p in re.split(r"[,/]", upper) if p.strip()]
    return parts or [upper]


def index_opposing_party_by_last_name(matters_raw: list[dict]) -> dict[str, int | None]:
    """Last name of a matter's Opposing Party custom field -> matter ID
    (None if two matters share the same opposing-party last name — treated
    as ambiguous, same as index_by_last_name's own convention).

    Real gap this closes: some Legs invoices (process serving, deposition
    officer fees) only ever name the *opposing* party, never our own
    client — confirmed live: matter VERSTRAETE, MARY PAULA has Opposing
    Party "GARRON, MARK" on file, and a real Legs invoice's only identifier
    was the bare word "GARRON", with no case caption or other text linking
    it back to Verstraete at all."""
    raw: dict[str, list[int]] = {}
    for m in matters_raw:
        opposing_party = None
        for cfv in m.get("custom_field_values") or []:
            if cfv.get("field_name") == "Opposing Party":
                opposing_party = cfv.get("value")
                break
        if not opposing_party:
            continue
        last = opposing_party.split(",")[0].strip().upper()
        if last:
            raw.setdefault(last, []).append(int(m["id"]))
    return {name: (ids[0] if len(ids) == 1 else None) for name, ids in raw.items()}


def build_note_text(text: str, client_identifier: str | None) -> str:
    """Human-readable service description for the Clio note — the body
    lines with vendor/client boilerplate and the identifier line itself
    stripped out, joined for a compact one-line note."""
    lines = []
    for line in text.splitlines():
        candidate = line.strip()
        if not candidate or _BOILERPLATE_RE.search(candidate):
            continue
        if client_identifier and candidate == client_identifier:
            continue
        lines.append(candidate)
    return "; ".join(lines) if lines else "(description unavailable)"


def parse_invoice_page(page: pdfplumber.page.Page, page_number: int, body_text: str,
                        statement_amounts: dict[str, float]) -> ParsedInvoice:
    """body_text is the page's full-page OCR text, already computed by the
    caller's classification pass — reused here rather than OCR'd again
    (each page still gets one additional, cheap header-crop OCR, since that
    needs a different crop/PSM than the full-page pass)."""
    header_text = ocr_header_crop(page)
    date_iso, invoice_number = parse_header_crop(header_text)

    is_firm_overhead = "RETAINER" in body_text.upper()
    client_identifier = None if is_firm_overhead else extract_client_identifier(body_text)

    if client_identifier and client_identifier.upper() in FIRM_OVERHEAD_IDENTIFIERS:
        is_firm_overhead = True
        client_identifier = None

    amount = statement_amounts.get(invoice_number) if invoice_number else None

    return ParsedInvoice(
        invoice_number=invoice_number or f"UNKNOWN-p{page_number}",
        page_number=page_number,
        date=date_iso,
        client_raw=client_identifier,
        note_text=build_note_text(body_text, client_identifier),
        statement_amount=amount,
        is_firm_overhead=is_firm_overhead,
    )


# ---------------------------------------------------------------------------
# Fuzzy suggestions for unmatched names — same approach as bradford_invoice.py
# ---------------------------------------------------------------------------

SUGGESTION_CUTOFF = 0.6


def suggest_match(name: str, matters: dict[str, int | None]) -> tuple[str, int | None] | None:
    candidates = difflib.get_close_matches(name, matters.keys(), n=1, cutoff=SUGGESTION_CUTOFF)
    if not candidates:
        return None
    best = candidates[0]
    return best, matters[best]


# ---------------------------------------------------------------------------
# Build Clio API payloads
# ---------------------------------------------------------------------------

def build_payloads(
    invoices: list[ParsedInvoice],
    matters: dict[str, int | None],
    manual_map: dict[str, int] | None = None,
    opposing_party_by_last_name: dict[str, int | None] | None = None,
) -> tuple[list[dict], list[dict], list[dict]]:
    """Returns (payloads, exceptions, firm_overhead)."""
    manual_map = MANUAL_MATTER_MAP if manual_map is None else manual_map
    opposing_party_by_last_name = opposing_party_by_last_name or {}
    payloads: list[dict] = []
    exceptions: list[dict] = []
    firm_overhead: list[dict] = []

    for inv in invoices:
        if inv.is_firm_overhead:
            firm_overhead.append({
                "invoice_number": inv.invoice_number, "page": inv.page_number,
                "amount": inv.statement_amount, "note": inv.note_text,
            })
            logging.info("Invoice %s (p%d): firm overhead — excluded from client billing",
                         inv.invoice_number, inv.page_number)
            continue

        if inv.statement_amount is None:
            exceptions.append({
                "name": inv.client_raw or "", "invoice_number": inv.invoice_number,
                "page": inv.page_number, "date": inv.date or "", "amount": "",
                "reason": "Could not read invoice number/amount from statement — OCR mismatch",
                "suggested_match": "", "suggested_matter_id": "",
            })
            logging.warning("Invoice %s (p%d): no statement amount found", inv.invoice_number, inv.page_number)
            continue

        if not inv.client_raw:
            exceptions.append({
                "name": "", "invoice_number": inv.invoice_number, "page": inv.page_number,
                "date": inv.date or "", "amount": inv.statement_amount,
                "reason": "No client identifier found on invoice page",
                "suggested_match": "", "suggested_matter_id": "",
            })
            logging.warning("Invoice %s (p%d): no client identifier found", inv.invoice_number, inv.page_number)
            continue

        # A case caption or joint identifier doesn't reliably say which side
        # is our client — real example: "CROSSON V SAMUELS" is filed in
        # Clio as SAMUELS, not the first-listed party (mediation/divorce
        # matters can be opened under either name). Try every candidate in
        # order, through three tiers per candidate (exact last name ->
        # compound-surname substring -> opposing-party field); only report
        # an exception if nothing resolves anywhere.
        candidates = client_identifier_candidates(inv.client_raw)
        matter_id: int | None = None
        name: str | None = None
        matched_via: str = ""
        ambiguous_name: str | None = None

        for candidate in candidates:
            if candidate in manual_map:
                matter_id, name, matched_via = manual_map[candidate], candidate, "manual override"
                break

            if candidate in matters:
                if matters[candidate] is not None:
                    matter_id, name, matched_via = matters[candidate], candidate, "last name"
                    break
                elif ambiguous_name is None:
                    ambiguous_name = candidate
                continue

            # No exact last-name match — try substring match (handles
            # compound surnames filed in Clio under just one part, e.g.
            # "FOOKS-WEBB" on the invoice vs. "WEBB" in Clio — same
            # approach court_calendar's party_names_match() already uses).
            substring_hits = {mid for key, mid in matters.items()
                               if mid is not None and party_names_match(candidate, key)}
            if len(substring_hits) == 1:
                matter_id, name, matched_via = substring_hits.pop(), candidate, "compound-name match"
                break
            elif len(substring_hits) > 1:
                if ambiguous_name is None:
                    ambiguous_name = candidate
                continue

            # Not a client name at all on this invoice — maybe it's the
            # *opposing* party instead (real gap: process-serving/
            # deposition invoices sometimes only ever name the other side).
            if candidate in opposing_party_by_last_name:
                op_matter_id = opposing_party_by_last_name[candidate]
                if op_matter_id is not None:
                    matter_id, name, matched_via = op_matter_id, candidate, "opposing party"
                    break
                elif ambiguous_name is None:
                    ambiguous_name = candidate

        if matter_id is not None:
            via = "" if name == candidates[0] and matched_via == "last name" else f" ({matched_via}: {name!r} of {candidates})"
            logging.info("Invoice %s: %-14s $%.2f  -> matter %s%s",
                         inv.invoice_number, name, inv.statement_amount, matter_id, via)
        elif ambiguous_name is not None:
            exceptions.append({
                "name": ambiguous_name, "invoice_number": inv.invoice_number, "page": inv.page_number,
                "date": inv.date or "", "amount": inv.statement_amount,
                "reason": "Ambiguous — multiple open matters; add to MANUAL_MATTER_MAP",
                "suggested_match": "", "suggested_matter_id": "",
            })
            logging.warning("Invoice %s: %-14s $%.2f  AMBIGUOUS", inv.invoice_number, ambiguous_name, inv.statement_amount)
            continue
        else:
            primary = candidates[0]
            suggestion = suggest_match(primary, matters)
            exceptions.append({
                "name": primary, "invoice_number": inv.invoice_number, "page": inv.page_number,
                "date": inv.date or "", "amount": inv.statement_amount,
                "reason": "No matching open matter",
                "suggested_match": suggestion[0] if suggestion else "",
                "suggested_matter_id": suggestion[1] if suggestion and suggestion[1] else "",
            })
            logging.warning("Invoice %s: %-14s $%.2f  NO MATCH%s",
                            inv.invoice_number, primary, inv.statement_amount,
                            f" — did you mean {suggestion[0]}?" if suggestion else "")
            continue

        data = {
            "type": "ExpenseEntry",
            "date": inv.date or datetime.today().strftime("%Y-%m-%d"),
            "matter": {"id": matter_id},
            "quantity": 1,
            "price": inv.statement_amount,
            "note": f"Legs Legal Support — Invoice L{inv.invoice_number}: {inv.note_text}",
        }
        # page is a sibling key, never sent to Clio (see post_entry, which
        # POSTs payload["data"] alone) — dashboard-only, so a matched row
        # can still link to its source page's thumbnail.
        payloads.append({"data": data, "page": inv.page_number})

    return payloads, exceptions, firm_overhead


# ---------------------------------------------------------------------------
# Reconciliation — statement totals vs. what we actually parsed off the
# invoice pages, so an OCR miss (missed page, misread amount) is loud
# instead of silent.
# ---------------------------------------------------------------------------

def reconcile(statement_amounts: dict[str, float], invoices: list[ParsedInvoice]) -> tuple[bool, str]:
    parsed_by_number = {inv.invoice_number: inv for inv in invoices}
    issues: list[str] = []

    for number, amount in statement_amounts.items():
        if number not in parsed_by_number:
            issues.append(f"Statement invoice {number} (${amount:.2f}) has no matching invoice page")

    for inv in invoices:
        if inv.invoice_number not in statement_amounts:
            issues.append(f"Invoice page {inv.page_number} (#{inv.invoice_number}) not found on the statement")

    statement_total = sum(statement_amounts.values())
    parsed_total = sum(inv.statement_amount for inv in invoices if inv.statement_amount is not None)
    if round(statement_total, 2) != round(parsed_total, 2):
        issues.append(f"Statement total ${statement_total:.2f} != parsed total ${parsed_total:.2f}")

    if issues:
        return False, "; ".join(issues)
    return True, f"Statement total ${statement_total:.2f} matches {len(invoices)} parsed invoice(s)"


# ---------------------------------------------------------------------------
# Post to Clio
# ---------------------------------------------------------------------------

def post_entry(session: requests.Session, payload: dict) -> bool:
    d = payload["data"]
    matter_id = d["matter"]["id"]
    note_preview = d["note"][:50]

    for attempt in range(1, 3):
        resp = session.post(ACTIVITIES_URL, json={"data": d})
        if resp.status_code in (200, 201):
            activity_id = resp.json().get("data", {}).get("id", "?")
            logging.info("POSTED  matter=%s  activity=%s  %s", matter_id, activity_id, note_preview)
            return True
        elif resp.status_code == 429:
            wait = 60
            m = re.search(r"Retry in (\d+) seconds", resp.text)
            if m:
                wait = int(m.group(1)) + 2
            logging.warning("RATE LIMITED  matter=%s  waiting %ds (attempt %d/2)", matter_id, wait, attempt)
            time.sleep(wait)
        else:
            logging.error("FAILED  matter=%s  status=%s  body=%s", matter_id, resp.status_code, resp.text[:300])
            return False

    logging.error("FAILED  matter=%s  gave up after 2 attempts", matter_id)
    return False


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"legs_expenses_{datetime.today().strftime('%Y%m%d')}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        force=True,
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(
                open(sys.stdout.fileno(), mode="w", encoding="utf-8", errors="replace", closefd=False)
            ),
        ],
    )
    logging.info("Log: %s", log_file)


# ---------------------------------------------------------------------------
# Pipeline (shared by the CLI and the web dashboard)
# ---------------------------------------------------------------------------

@dataclass
class RunResult:
    stem: str
    payloads: list[dict] = field(default_factory=list)
    exceptions: list[dict] = field(default_factory=list)
    firm_overhead: list[dict] = field(default_factory=list)
    reconciliation_ok: bool = True
    reconciliation_note: str = ""
    total_entries: int = 0
    posted: int = 0
    failed: int = 0
    payloads_path: Path | None = None
    exceptions_path: Path | None = None
    matter_names: dict[int, str] = field(default_factory=dict)
    all_matters: list[dict] = field(default_factory=list)  # [{"id":, "name":}], for the dashboard's matter-name search


def run_pipeline(
    input_path: Path,
    dry_run: bool,
    matter_filter: str = "",
    output_dir: Path = Path("output"),
) -> RunResult:
    """
    OCR the Legs statement PDF, match invoices to Clio matters, write
    payload/exception output files, and (unless dry_run) POST expense
    entries.

    Raises FileNotFoundError / RuntimeError on hard failures instead of
    exiting the process, so it's safe to call from a long-running server.
    """
    if not ACCESS_TOKEN:
        raise RuntimeError("CLIO_ACCESS_TOKEN not set in .env")
    if not input_path.exists():
        raise FileNotFoundError(f"PDF not found: {input_path}")
    ensure_tesseract()

    output_dir.mkdir(exist_ok=True)
    stem = re.sub(r"[^\w\-]", "_", input_path.stem)
    thumbnails_dir = output_dir / f"{stem}_thumbnails"

    logging.info("Reading %s", input_path)
    statement_amounts: dict[str, float] = {}
    invoices: list[ParsedInvoice] = []

    with pdfplumber.open(input_path) as pdf:
        # Pass 1: find every statement page and build the authoritative
        # invoice-number -> amount map (needed before parsing invoice pages).
        page_texts = [ocr_page_text(page) for page in pdf.pages]
        page_kinds = [classify_page(t) for t in page_texts]

        for text, kind in zip(page_texts, page_kinds):
            if kind == "statement":
                statement_amounts.update(parse_statement_page(text))

        logging.info("Found %d invoice amounts across %d statement page(s)",
                     len(statement_amounts), page_kinds.count("statement"))

        # Pass 2: parse each invoice page, reusing pass 1's full-page OCR
        # text (each page still gets one more OCR call for the header crop,
        # which needs a different crop/PSM — but this avoids OCR'ing every
        # invoice page's full body a second time, which was roughly doubling
        # total processing time for no benefit).
        for i, (page, kind, text) in enumerate(zip(pdf.pages, page_kinds, page_texts)):
            if kind != "invoice":
                if kind == "unknown":
                    logging.warning("Page %d: could not classify as Statement or Invoice — skipped", i + 1)
                continue
            invoices.append(parse_invoice_page(page, i + 1, text, statement_amounts))
            save_page_thumbnail(page, thumbnails_dir / f"page_{i + 1}.jpg")

    logging.info("Parsed %d invoice page(s)", len(invoices))

    if matter_filter:
        f = matter_filter.upper()
        invoices = [inv for inv in invoices if inv.client_raw and f in inv.client_raw.upper()]
        logging.info("--matter '%s' matched %d invoice(s)", matter_filter, len(invoices))
        if not invoices:
            raise RuntimeError(f"No invoices matched --matter '{matter_filter}'")

    reconciliation_ok, reconciliation_note = reconcile(statement_amounts, invoices)
    if reconciliation_ok:
        logging.info("Reconciliation OK: %s", reconciliation_note)
    else:
        logging.warning("Reconciliation MISMATCH: %s", reconciliation_note)

    session = requests.Session()
    session.headers.update({
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json",
    })

    matters_raw = fetch_open_matters(session, fields=MATTERS_FIELDS_WITH_OP)
    matters = index_by_last_name(matters_raw)
    opposing_party_by_last_name = index_opposing_party_by_last_name(matters_raw)
    payloads, exceptions, firm_overhead = build_payloads(
        invoices, matters, effective_manual_matter_map(), opposing_party_by_last_name,
    )

    matter_names = {mid: name for name, mid in matters.items() if mid}
    all_matters = sorted(
        (
            {"id": int(m["id"]), "name": m["display_number"]}
            for m in matters_raw if m.get("display_number")
        ),
        key=lambda m: m["name"],
    )
    result = RunResult(
        stem=stem, payloads=payloads, exceptions=exceptions, firm_overhead=firm_overhead,
        reconciliation_ok=reconciliation_ok, reconciliation_note=reconciliation_note,
        total_entries=len(invoices), matter_names=matter_names, all_matters=all_matters,
    )

    result.payloads_path = output_dir / f"{stem}_payloads.json"
    with open(result.payloads_path, "w", encoding="utf-8") as f:
        json.dump(payloads, f, indent=2)
    logging.info("Wrote %d payloads -> %s", len(payloads), result.payloads_path)

    if exceptions:
        result.exceptions_path = output_dir / f"{stem}_exceptions.csv"
        with open(result.exceptions_path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=[
                "name", "invoice_number", "page", "date", "amount", "reason",
                "suggested_match", "suggested_matter_id",
            ])
            w.writeheader()
            w.writerows(exceptions)
        logging.warning("Wrote %d exceptions -> %s", len(exceptions), result.exceptions_path)

    total_amount = sum(p["data"]["price"] for p in payloads)
    logging.info(
        "Summary: %d invoices / %d payloads / %d exceptions / %d firm overhead  |  $%.2f total",
        len(invoices), len(payloads), len(exceptions), len(firm_overhead), total_amount,
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
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", required=True, help="Path to Legs statement PDF")
    ap.add_argument("--dry-run", action="store_true", help="Parse, match, and write payloads — do not POST")
    ap.add_argument("--matter", default="", help="Process only invoices whose client identifier contains this string")
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
