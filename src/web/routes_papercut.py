"""
routes_papercut.py — Status page + manual trigger for generate_papercut_accounts.py.

Read-only status view backed by the papercut_sync_runs table (no live Clio call
just to view the page) plus a "Generate now" button that runs the pipeline
synchronously. No confirm step, same reasoning as RingCentral Directory Sync —
this never writes to Clio, and PaperCut itself pulls from the generated file on
its own schedule rather than anything here pushing to PaperCut directly.
"""

from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse

import generate_papercut_accounts
from web.auth import require_auth
from web.db import get_connection

router = APIRouter(prefix="/papercut", tags=["papercut"])


def _last_run() -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM papercut_sync_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        data = dict(row)
        data["tsv_filename"] = Path(data["tsv_path"]).name if data.get("tsv_path") else None
        return data
    finally:
        conn.close()


def _render(request: Request, **overrides):
    from web.app import render

    context = {
        "last_run": _last_run(),
        "error": None,
        "accounts_path": str(generate_papercut_accounts.ACCOUNTS_PATH),
    }
    context.update(overrides)
    return render(request, "papercut.html", **context)


@router.get("", response_class=HTMLResponse)
async def papercut_home(request: Request, _: None = Depends(require_auth)):
    return _render(request)


@router.post("/generate", response_class=HTMLResponse)
async def papercut_generate(request: Request, _: None = Depends(require_auth)):
    error = None
    try:
        await run_in_threadpool(generate_papercut_accounts.run_pipeline)
    except RuntimeError as e:
        error = str(e)
    return _render(request, error=error)
