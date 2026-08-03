"""
routes_printer.py — Drag-and-drop UI for printer_expenses.py.

Upload -> dry-run preview (payloads + exceptions) -> confirm -> live post.
Wraps printer_expenses.run_pipeline(); no matching/posting logic lives here.
"""

import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse

import printer_expenses
from web.auth import require_auth
from web.preview_store import PREVIEWS

router = APIRouter(prefix="/printer", tags=["printer"])

DATA_DIR = Path("data")


def _safe_filename(name: str) -> str:
    return Path(name).name


@router.get("", response_class=HTMLResponse)
async def printer_home(request: Request, _: None = Depends(require_auth)):
    from web.app import render
    return render(request, "printer.html", result=None, token=None, error=None, live=False)


@router.post("/preview", response_class=HTMLResponse)
async def printer_preview(
    request: Request,
    file: UploadFile = File(...),
    _: None = Depends(require_auth),
):
    from web.app import render

    DATA_DIR.mkdir(exist_ok=True)
    dest = DATA_DIR / _safe_filename(file.filename or "print_copy_summary_by_account.csv")
    with open(dest, "wb") as out:
        out.write(await file.read())

    result = None
    error = None
    token = None
    try:
        result = await run_in_threadpool(printer_expenses.run_pipeline, dest, dry_run=True)
        token = uuid.uuid4().hex
        PREVIEWS[token] = {"input_path": dest}
    except (FileNotFoundError, RuntimeError) as e:
        error = str(e)

    return render(request, "printer.html", result=result, token=token,
                  error=error, live=False, filename=dest.name)


@router.post("/confirm", response_class=HTMLResponse)
async def printer_confirm(
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
            result = await run_in_threadpool(printer_expenses.run_pipeline, entry["input_path"], dry_run=False)
        except (FileNotFoundError, RuntimeError) as e:
            error = str(e)

    return render(request, "printer.html", result=result, token=None, error=error, live=True)
