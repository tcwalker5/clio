"""
routes_collections.py — Collections view: unpaid, already-issued bills.

Read-only, like Court Calendar Sync and the Trust Monitor's WIP table — runs
live on every page view. See collections_monitor.py's module docstring for
why this is split out from Trust Monitor rather than living on /trust.
"""

from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse

import collections_monitor
from web.auth import require_auth

router = APIRouter(prefix="/collections", tags=["collections"])


@router.get("", response_class=HTMLResponse)
async def collections_home(request: Request, _: None = Depends(require_auth)):
    from web.app import render

    try:
        bills = await run_in_threadpool(collections_monitor.run_pipeline)
    except RuntimeError as e:
        return render(request, "collections.html", error=str(e), bills=None)

    bills_sorted = sorted(bills, key=lambda b: (-b.days_overdue, -b.balance))
    overdue_count = sum(1 for b in bills_sorted if b.overdue)
    total_balance = sum(b.balance for b in bills_sorted)

    return render(
        request, "collections.html", error=None,
        bills=bills_sorted, overdue_count=overdue_count, total_balance=total_balance,
    )


@router.get("/download")
async def collections_download(_: None = Depends(require_auth)):
    path = Path("output") / f"collections_monitor_{datetime.today().strftime('%Y-%m-%d')}.csv"
    if not path.exists():
        return HTMLResponse("No report generated yet — visit /collections first.", status_code=404)
    return FileResponse(path, filename=path.name, media_type="text/csv")
