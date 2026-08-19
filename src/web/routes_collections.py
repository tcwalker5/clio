"""
routes_collections.py — Collections view: unpaid, already-issued bills.

Read-only, like Court Calendar Sync and the Trust Monitor's WIP table — runs
live on every page view. See collections_monitor.py's module docstring for
why this is split out from Trust Monitor rather than living on /trust.
"""

from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

import collections_flarpl
import collections_monitor
from web.auth import require_auth
from web.db import get_connection

router = APIRouter(prefix="/collections", tags=["collections"])


def _attach_actions(bills: list[collections_monitor.UnpaidBill]) -> None:
    """Sets each bill's `.action` from collections_actions (keyed by matter,
    not bill — see collections_monitor.SCHEMA's docstring note) and
    `.flarpl_recorded` live from Clio for whichever matters currently have
    "FLARPL" selected (collections_flarpl.py — read-only: Clio is the
    source of truth for whether a FLARPL has actually been recorded, this
    dashboard only ever reflects it, never sets it)."""
    conn = get_connection()
    try:
        actions_by_matter = collections_monitor.fetch_actions_by_matter(conn)
    finally:
        conn.close()
    for b in bills:
        b.action = actions_by_matter.get(b.matter_id, "") if b.matter_id else ""

    flarpl_matter_ids = sorted({b.matter_id for b in bills if b.action == "FLARPL" and b.matter_id})
    if flarpl_matter_ids:
        session = collections_monitor.build_session()
        recorded_by_matter = collections_flarpl.fetch_recorded_by_matter(session, flarpl_matter_ids)
        for b in bills:
            if b.action == "FLARPL" and b.matter_id:
                b.flarpl_recorded = recorded_by_matter.get(b.matter_id, False)


@router.get("", response_class=HTMLResponse)
async def collections_home(request: Request, _: None = Depends(require_auth)):
    from web.app import render

    try:
        bills = await run_in_threadpool(collections_monitor.run_pipeline)
    except RuntimeError as e:
        return render(request, "collections.html", error=str(e), bills=None)

    await run_in_threadpool(_attach_actions, bills)

    bills_sorted = sorted(bills, key=lambda b: (-b.days_overdue, -b.balance))
    overdue_count = sum(1 for b in bills_sorted if b.overdue)
    total_balance = sum(b.balance for b in bills_sorted)

    return render(
        request, "collections.html", error=None,
        bills=bills_sorted, overdue_count=overdue_count, total_balance=total_balance,
        actions=collections_monitor.COLLECTIONS_ACTIONS,
    )


@router.post("/set-action")
async def set_action(matter_id: int = Form(...), action: str = Form(""), _: None = Depends(require_auth)):
    # action="" (the "—" clear option) — Form(...) as "required" rejects an
    # empty string as a missing field in this FastAPI version (confirmed:
    # Starlette's own form parser correctly returns action="", but FastAPI's
    # Form(...)-required binding still reports it as absent) — Form("") with
    # an explicit default sidesteps that rather than chasing the framework
    # internals further.
    conn = get_connection()
    try:
        collections_monitor.set_action(conn, matter_id, action)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    finally:
        conn.close()
    return JSONResponse({"success": True})


@router.get("/action-report", response_class=HTMLResponse)
async def action_report(request: Request, _: None = Depends(require_auth)):
    from web.app import render

    try:
        bills = await run_in_threadpool(collections_monitor.run_pipeline)
    except RuntimeError as e:
        return render(request, "collections_action_report.html", error=str(e), bills=None)

    await run_in_threadpool(_attach_actions, bills)

    # Alphabetical by matter display number, which is already "Last, First"
    # by Clio's own convention — no separate last-name parsing needed.
    bills_sorted = sorted(bills, key=lambda b: b.display_number)

    return render(request, "collections_action_report.html", error=None, bills=bills_sorted)


@router.get("/download")
async def collections_download(_: None = Depends(require_auth)):
    path = Path("output") / f"collections_monitor_{datetime.today().strftime('%Y-%m-%d')}.csv"
    if not path.exists():
        return HTMLResponse("No report generated yet — visit /collections first.", status_code=404)
    return FileResponse(path, filename=path.name, media_type="text/csv")
