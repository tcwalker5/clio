"""
routes_ringcentral.py — Status page + manual trigger for ringcentral_directory.py.

Read-only status view backed by the ringcentral_sync_runs table (no live Clio call
just to view the page) plus a "Sync now" button that runs the pipeline synchronously.
No confirm step, unlike Bradford/Printer — this never writes to Clio or RingCentral,
so there's nothing destructive to gate behind a second click. No server-side
webbrowser.open() here either: that only makes sense on the physical machine running
the scheduled task, not a dashboard also reachable over Tailscale from other devices —
the page surfaces the RingCentral import-page link instead, for the person at the
keyboard to click.
"""

from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, HTMLResponse

import ringcentral_directory
from web.auth import require_auth
from web.db import get_connection

router = APIRouter(prefix="/ringcentral", tags=["ringcentral"])


def _last_run() -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM ringcentral_sync_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        data = dict(row)
        data["csv_filename"] = Path(data["csv_path"]).name if data.get("csv_path") else None
        return data
    finally:
        conn.close()


def _render(request: Request, **overrides):
    from web.app import render

    context = {
        "last_run": _last_run(),
        "error": None,
        "conflicts": None,
        "import_url": ringcentral_directory.RINGCENTRAL_IMPORT_URL,
    }
    context.update(overrides)
    return render(request, "ringcentral.html", **context)


@router.get("", response_class=HTMLResponse)
async def ringcentral_home(request: Request, _: None = Depends(require_auth)):
    return _render(request)


@router.post("/sync", response_class=HTMLResponse)
async def ringcentral_sync(request: Request, _: None = Depends(require_auth)):
    error = None
    conflicts = None
    try:
        result = ringcentral_directory.run_pipeline()
        conflicts = result.conflicts
    except RuntimeError as e:
        error = str(e)
    return _render(request, error=error, conflicts=conflicts)


@router.get("/download")
async def ringcentral_download(_: None = Depends(require_auth)):
    last = _last_run()
    if not last or not last.get("csv_path"):
        return HTMLResponse("No directory CSV has been generated yet — click Sync now first.", status_code=404)
    path = Path(last["csv_path"])
    if not path.exists():
        return HTMLResponse(f"{path} no longer exists on disk.", status_code=404)
    return FileResponse(path, filename=path.name, media_type="text/csv")
