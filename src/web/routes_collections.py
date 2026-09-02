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
import collections_payment_plan
from web.auth import require_auth
from web.db import get_connection

router = APIRouter(prefix="/collections", tags=["collections"])


def _attach_actions(summaries: list[collections_monitor.MatterBillSummary]) -> None:
    """Sets each matter summary's `.action` from collections_actions (keyed
    by matter, which is exactly what a MatterBillSummary already is — see
    collections_monitor.SCHEMA's docstring note), plus two live read-only
    Clio confirmations, each only fetched for matters whose action
    currently matches: `.flarpl_recorded` for "FLARPL" (collections_flarpl.py)
    and `.payment_plan_active` for "Payment plan" (collections_payment_plan.py).
    Clio is the source of truth for both — this dashboard only ever reflects
    them, never sets them."""
    conn = get_connection()
    try:
        actions_by_matter = collections_monitor.fetch_actions_by_matter(conn)
    finally:
        conn.close()
    for s in summaries:
        s.action = actions_by_matter.get(s.matter_id, "") if s.matter_id else ""

    session = None

    flarpl_matter_ids = sorted({s.matter_id for s in summaries if s.action == "FLARPL" and s.matter_id})
    if flarpl_matter_ids:
        session = session or collections_monitor.build_session()
        recorded_by_matter = collections_flarpl.fetch_recorded_by_matter(session, flarpl_matter_ids)
        for s in summaries:
            if s.action == "FLARPL" and s.matter_id:
                s.flarpl_recorded = recorded_by_matter.get(s.matter_id, False)

    payment_plan_matter_ids = sorted({s.matter_id for s in summaries if s.action == "Payment plan" and s.matter_id})
    if payment_plan_matter_ids:
        session = session or collections_monitor.build_session()
        active_by_matter = collections_payment_plan.fetch_active_by_matter(session, payment_plan_matter_ids)
        for s in summaries:
            if s.action == "Payment plan" and s.matter_id:
                s.payment_plan_active = active_by_matter.get(s.matter_id, False)


@router.get("", response_class=HTMLResponse)
async def collections_home(request: Request, _: None = Depends(require_auth)):
    from web.app import render

    try:
        bills = await run_in_threadpool(collections_monitor.run_pipeline)
    except RuntimeError as e:
        return render(request, "collections.html", error=str(e), summaries=None)

    summaries = collections_monitor.build_matter_summaries(bills)
    await run_in_threadpool(_attach_actions, summaries)

    overdue_count = sum(1 for s in summaries if s.overdue)
    # Three categories, most to least urgent (Ted, 2026-09-02) — computed per
    # BILL via UnpaidBill.category, not per matter, since one matter can carry
    # both an earned invoice and a trust top-up bill at once: "earned" (billed
    # work — the actual collections AR, this page's headline $ figure),
    # "replenishment" (a matter's trust top-up — care about it, but it isn't
    # earned yet), "new_trust" (a brand new client's initial retainer — not
    # subject to collections at all). None are dropped from the table, just
    # kept out of each other's totals so the headline number means one thing.
    total_balance = sum(b.balance for b in bills if b.category == "earned")
    replenishment_balance = sum(b.balance for b in bills if b.category == "replenishment")
    new_trust_balance = sum(b.balance for b in bills if b.category == "new_trust")

    return render(
        request, "collections.html", error=None,
        summaries=summaries, overdue_count=overdue_count, total_balance=total_balance,
        replenishment_balance=replenishment_balance, new_trust_balance=new_trust_balance,
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
        return render(request, "collections_action_report.html", error=str(e), summaries=None)

    summaries = collections_monitor.build_matter_summaries(bills)
    await run_in_threadpool(_attach_actions, summaries)

    # Alphabetical by matter display number, which is already "Last, First"
    # by Clio's own convention — no separate last-name parsing needed.
    summaries_sorted = sorted(summaries, key=lambda s: s.display_number)

    return render(request, "collections_action_report.html", error=None, summaries=summaries_sorted)


@router.get("/download")
async def collections_download(_: None = Depends(require_auth)):
    path = Path("output") / f"collections_monitor_{datetime.today().strftime('%Y-%m-%d')}.csv"
    if not path.exists():
        return HTMLResponse("No report generated yet — visit /collections first.", status_code=404)
    return FileResponse(path, filename=path.name, media_type="text/csv")
