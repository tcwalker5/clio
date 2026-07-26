"""
routes_legs.py — Drag-and-drop UI for legs_expenses.py.

Upload -> dry-run preview (payloads + exceptions + firm overhead) -> confirm
-> live post. Wraps legs_expenses.run_pipeline(); no matching/posting/OCR
logic lives here — same shape as routes_bradford.py.
"""

import re
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
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
        result = legs_expenses.run_pipeline(dest, dry_run=True)
        token = uuid.uuid4().hex
        PREVIEWS[token] = {"input_path": dest}
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
    survives future imports) and re-runs the dry-run preview in place, so a
    resolved exception disappears from the exceptions table and shows up as a
    matched entry instead — same token, same "Confirm & Post" step at the end."""
    from web.app import render

    entry = PREVIEWS.get(token)
    result = None
    error = None
    if not entry:
        error = "This preview has expired — please upload the file again."
    else:
        legs_expenses.save_persisted_override(name, matter_id, note)
        try:
            result = legs_expenses.run_pipeline(entry["input_path"], dry_run=True)
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
            result = legs_expenses.run_pipeline(entry["input_path"], dry_run=False)
        except (FileNotFoundError, RuntimeError) as e:
            error = str(e)

    return render(request, "legs.html", result=result, token=None, error=error, live=True)
