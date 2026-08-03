"""
routes_bradford.py — Drag-and-drop UI for bradford_invoice.py.

Upload -> dry-run preview (payloads + exceptions) -> confirm -> live post.
Wraps bradford_invoice.run_pipeline(); no matching/posting logic lives here.
"""

import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse

import bradford_invoice
from web.auth import require_auth
from web.preview_store import PREVIEWS

router = APIRouter(prefix="/bradford", tags=["bradford"])

DATA_DIR = Path("data")


def _safe_filename(name: str) -> str:
    return Path(name).name


@router.get("", response_class=HTMLResponse)
async def bradford_home(request: Request, _: None = Depends(require_auth)):
    from web.app import render
    return render(request, "bradford.html", result=None, token=None, error=None, live=False)


@router.post("/preview", response_class=HTMLResponse)
async def bradford_preview(
    request: Request,
    file: UploadFile = File(...),
    _: None = Depends(require_auth),
):
    from web.app import render

    DATA_DIR.mkdir(exist_ok=True)
    dest = DATA_DIR / _safe_filename(file.filename or "invoice.pdf")
    with open(dest, "wb") as out:
        out.write(await file.read())

    result = None
    error = None
    token = None
    try:
        result = await run_in_threadpool(bradford_invoice.run_pipeline, dest, dry_run=True)
        token = uuid.uuid4().hex
        PREVIEWS[token] = {"input_path": dest}
    except (FileNotFoundError, RuntimeError) as e:
        error = str(e)

    return render(request, "bradford.html", result=result, token=token,
                  error=error, live=False, filename=dest.name)


@router.post("/resolve-exception", response_class=HTMLResponse)
async def bradford_resolve_exception(
    request: Request,
    token: str = Form(...),
    name: str = Form(...),
    matter_id: int = Form(...),
    note: str = Form(""),
    _: None = Depends(require_auth),
):
    """Persists one name -> matter_id override (data/bradford_manual_matter_map.csv,
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
        bradford_invoice.save_persisted_override(name, matter_id, note)
        try:
            result = await run_in_threadpool(bradford_invoice.run_pipeline, entry["input_path"], dry_run=True)
        except (FileNotFoundError, RuntimeError) as e:
            error = str(e)

    return render(request, "bradford.html", result=result, token=token if entry else None,
                  error=error, live=False, filename=entry["input_path"].name if entry else "")


@router.post("/confirm", response_class=HTMLResponse)
async def bradford_confirm(
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
            result = await run_in_threadpool(bradford_invoice.run_pipeline, entry["input_path"], dry_run=False)
        except (FileNotFoundError, RuntimeError) as e:
            error = str(e)

    return render(request, "bradford.html", result=result, token=None, error=error, live=True)
