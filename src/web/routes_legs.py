"""
routes_legs.py — Drag-and-drop UI for legs_expenses.py.

Upload -> dry-run preview (payloads + exceptions + firm overhead) -> confirm
-> live post. No matching/posting/OCR logic lives here.

Unlike routes_bradford.py's shape, this doesn't just wrap one run_pipeline()
call: the OCR pass (parse_statement_pdf) and the live Clio matters fetch
(fetch_matter_data) are each run exactly once, on upload, and their output
is cached in PREVIEWS — every exception resolution after that calls the
cheap build_result() alone. Re-running the whole pipeline (including OCR)
on every single Save was confirmed to take ~45-50s per click on a real
statement — see "Parse-once, match-many architecture" in
reference/invoices.md.
"""

import logging
import re
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse

import legs_expenses
from web.auth import require_auth
from web.preview_store import PREVIEWS

router = APIRouter(prefix="/legs", tags=["legs"])

DATA_DIR = Path("data")
OUTPUT_DIR = Path("output")

# stem is a URL path segment reflected straight into a filesystem lookup —
# only allow the character set legs_expenses.py's own stem sanitizer
# (re.sub(r"[^\w\-]", "_", ...)) can actually produce, so a crafted stem
# can't be used for path traversal.
_SAFE_STEM_RE = re.compile(r"^[\w\-]+$")


def _safe_filename(name: str) -> str:
    return Path(name).name


def _parse_and_build(dest: Path):
    """Runs the two expensive, one-time steps (OCR the whole statement, fetch
    Clio matters) and the cheap matching step, all at once — this is only
    ever called on the initial upload. Returns (parsed, matter_data, result);
    the caller caches the first two in PREVIEWS so every later exception
    resolution can skip straight to the cheap _rebuild() below."""
    if not legs_expenses.ACCESS_TOKEN:
        raise RuntimeError("CLIO_ACCESS_TOKEN not set in .env")
    parsed = legs_expenses.parse_statement_pdf(dest)
    matter_data = legs_expenses.fetch_matter_data(legs_expenses.build_session())
    result = legs_expenses.build_result(
        parsed.stem, parsed.invoices, parsed.statement_amounts, matter_data,
    )
    return parsed, matter_data, result


def _rebuild(entry: dict) -> legs_expenses.RunResult:
    """Re-matches against cached OCR/Clio data — no OCR, no Clio API call.
    Called after every exception resolution (matter override, invoice-number
    correction, or amount override) so "Save" stays fast regardless of how
    many pages the statement has."""
    parsed = entry["parsed"]
    invoices = legs_expenses.apply_manual_overrides(
        parsed.invoices, parsed.statement_amounts,
        entry["invoice_number_overrides"], entry["amount_overrides"],
    )
    return legs_expenses.build_result(
        parsed.stem, invoices, parsed.statement_amounts, entry["matter_data"],
    )


def _confirm_and_post(entry: dict) -> legs_expenses.RunResult:
    result = _rebuild(entry)
    if not result.payloads:
        return result
    session = legs_expenses.build_session()
    for payload in result.payloads:
        if legs_expenses.post_entry(session, payload):
            result.posted += 1
        else:
            result.failed += 1
        time.sleep(legs_expenses.POST_DELAY)
    logging.info("Done: %d posted, %d failed", result.posted, result.failed)
    return result


def _write_upload(dest: Path, content: bytes) -> None:
    """Windows can transiently lock a just-overwritten file (antivirus scan
    of the previous version, a lingering read handle from OCR'ing it a
    moment earlier) — retry briefly instead of failing the whole preview on
    a PermissionError that clears itself within a second."""
    last_error: PermissionError | None = None
    for attempt in range(5):
        try:
            with open(dest, "wb") as out:
                out.write(content)
            return
        except PermissionError as e:
            last_error = e
            time.sleep(0.5)
    raise last_error


@router.get("", response_class=HTMLResponse)
async def legs_home(request: Request, _: None = Depends(require_auth)):
    from web.app import render
    return render(request, "legs.html", result=None, token=None, error=None, live=False)


@router.get("/thumbnail/{stem}/{page_number}")
async def legs_thumbnail(stem: str, page_number: int, _: None = Depends(require_auth)):
    """Small JPEG of one invoice page, so a row in the dry-run/posted tables
    can be visually spot-checked against the real scan — this PDF has zero
    embedded text, so OCR errors are a real, expected risk."""
    if not _SAFE_STEM_RE.match(stem):
        raise HTTPException(status_code=404)
    path = OUTPUT_DIR / f"{stem}_thumbnails" / f"page_{page_number}.jpg"
    if not path.exists():
        raise HTTPException(status_code=404)
    return FileResponse(path, media_type="image/jpeg")


@router.post("/preview", response_class=HTMLResponse)
async def legs_preview(
    request: Request,
    file: UploadFile = File(...),
    _: None = Depends(require_auth),
):
    from web.app import render

    DATA_DIR.mkdir(exist_ok=True)
    dest = DATA_DIR / _safe_filename(file.filename or "statement.pdf")
    content = await file.read()

    result = None
    error = None
    token = None
    try:
        _write_upload(dest, content)
        parsed, matter_data, result = await run_in_threadpool(_parse_and_build, dest)
        token = uuid.uuid4().hex
        PREVIEWS[token] = {
            "input_path": dest, "parsed": parsed, "matter_data": matter_data,
            "invoice_number_overrides": {},  # page_number -> corrected invoice number, this session only
            "amount_overrides": {},          # page_number -> corrected dollar amount, this session only
        }
    except (FileNotFoundError, RuntimeError) as e:
        error = str(e)
    except PermissionError:
        error = (f"Could not write {dest.name} — it looks like something else on this machine "
                 "has the file open (antivirus scan, a PDF viewer). Try again in a few seconds.")

    return render(request, "legs.html", result=result, token=token,
                  error=error, live=False, filename=dest.name)


@router.post("/resolve-exception", response_class=HTMLResponse)
async def legs_resolve_exception(
    request: Request,
    token: str = Form(...),
    name: str = Form(...),
    matter_id: int = Form(...),
    note: str = Form(""),
    _: None = Depends(require_auth),
):
    """Persists one name -> matter_id override (data/legs_manual_matter_map.csv,
    survives future imports) and re-matches against the cached OCR/Clio data
    (see _rebuild) — no re-OCR, no re-fetch — so a resolved exception
    disappears from the exceptions table and shows up as a matched entry
    instead, same token, same "Confirm & Post" step at the end."""
    from web.app import render

    entry = PREVIEWS.get(token)
    result = None
    error = None
    if not entry:
        error = "This preview has expired — please upload the file again."
    else:
        legs_expenses.save_persisted_override(name, matter_id, note)
        try:
            result = await run_in_threadpool(_rebuild, entry)
        except (FileNotFoundError, RuntimeError) as e:
            error = str(e)

    return render(request, "legs.html", result=result, token=token if entry else None,
                  error=error, live=False, filename=entry["input_path"].name if entry else "")


@router.post("/resolve-amount", response_class=HTMLResponse)
async def legs_resolve_amount(
    request: Request,
    token: str = Form(...),
    page: int = Form(...),
    invoice_number: str = Form(""),
    amount: str = Form(""),
    _: None = Depends(require_auth),
):
    """Corrects one page's OCR'd invoice number and/or its dollar amount —
    see legs_expenses.apply_manual_overrides for the full reasoning. Both
    fields are optional but at least one must be filled in:
      - invoice_number alone re-looks-up the amount from the already-parsed
        Statement data (no new OCR) — the preferred fix when the Statement
        does have the real invoice.
      - amount (with or without invoice_number) asserts the dollar amount
        directly, straight off the invoice page itself — the per-invoice
        page is the actual authority here, the Statement is only a
        cross-reference for the reconciliation warning, not a hard
        requirement for a page to be billable (2026-09-01 correction).
    Scoped to this upload session only (kept in PREVIEWS, not a persistent
    file) since this is scan-specific correction, not a recurring
    client-name issue that would apply to next month's statement too."""
    from web.app import render

    entry = PREVIEWS.get(token)
    result = None
    error = None
    if not entry:
        error = "This preview has expired — please upload the file again."
    elif not invoice_number.strip() and not amount.strip():
        error = "Enter an invoice number and/or an amount before saving."
    else:
        parsed_amount: float | None = None
        if amount.strip():
            try:
                parsed_amount = float(amount.strip().replace("$", "").replace(",", ""))
            except ValueError:
                error = f"'{amount}' isn't a valid dollar amount."
        if not error:
            if invoice_number.strip():
                entry["invoice_number_overrides"][page] = invoice_number.strip()
            if parsed_amount is not None:
                entry["amount_overrides"][page] = parsed_amount
            try:
                result = await run_in_threadpool(_rebuild, entry)
            except (FileNotFoundError, RuntimeError) as e:
                error = str(e)

    return render(request, "legs.html", result=result, token=token if entry else None,
                  error=error, live=False, filename=entry["input_path"].name if entry else "")


@router.post("/confirm", response_class=HTMLResponse)
async def legs_confirm(
    request: Request,
    token: str = Form(...),
    _: None = Depends(require_auth),
):
    from web.app import render

    entry = PREVIEWS.pop(token, None)
    result = None
    error = None
    if not entry:
        error = "This preview has expired — please upload the file again."
    else:
        try:
            result = await run_in_threadpool(_confirm_and_post, entry)
        except (FileNotFoundError, RuntimeError) as e:
            error = str(e)

    return render(request, "legs.html", result=result, token=None, error=error, live=True)
