"""
routes_collections.py — Collections view: unpaid, already-issued bills.

Read-only, like Court Calendar Sync and the Trust Monitor's WIP table — runs
live on every page view. See collections_monitor.py's module docstring for
why this is split out from Trust Monitor rather than living on /trust.
"""

import os
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

CLIO_BASE_URL = os.getenv("CLIO_BASE_URL", "https://app.clio.com").rstrip("/")


def _attach_actions(summaries: list[collections_monitor.MatterBillSummary]) -> None:
    """Sets each matter summary's `.action` from collections_actions (keyed
    by matter, which is exactly what a MatterBillSummary already is — see
    collections_monitor.SCHEMA's docstring note), plus three live read-only
    Clio confirmations, each only fetched for the summaries they're
    meaningful for: `.flarpl_recorded` for "FLARPL" (collections_flarpl.py),
    `.payment_plan_active` for "Payment plan" (collections_payment_plan.py),
    and `.matter_trust_balance` for a client-level trust request (see
    collections_monitor.MatterBillSummary.trust_level_mismatch). Clio is the
    source of truth for all three — this dashboard only ever reflects them,
    never sets them."""
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

    # Client-level trust requests only (matter_id is None, all_trust) — a
    # handful of clients at most, one targeted matters.json call each, not a
    # firm-wide sweep. See MatterBillSummary.trust_level_mismatch.
    client_trust_client_ids = sorted({s.client_id for s in summaries if s.matter_id is None and s.all_trust and s.client_id})
    if client_trust_client_ids:
        session = session or collections_monitor.build_session()
        # Cached per client_id — a client with more than one client-level
        # trust bill gets one summary per bill (build_matter_summaries can't
        # group matter-less bills together), which would otherwise mean a
        # duplicate live call for the same client.
        trust_balance_by_client = {cid: collections_monitor.fetch_matter_trust_balance(session, cid) for cid in client_trust_client_ids}
        for s in summaries:
            if s.matter_id is None and s.all_trust and s.client_id:
                s.matter_trust_balance = trust_balance_by_client[s.client_id]


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

    # Split trust requests into their own alphabetical block, separate from
    # actual invoices (Ted, 2026-09-09) — sorting the whole list together by
    # display_number put trust-only rows (no matter, so no display_number)
    # at the top out of alphabetical order relative to real matters, which
    # read as confusing to anyone unfamiliar with the page. Each block is
    # alphabetized by display_name, which is already "Last, First" for a
    # real matter and reformatted to match for a trust-only row (see
    # collections_monitor._last_first) — no separate last-name parsing
    # needed either way.
    invoice_summaries = sorted((s for s in summaries if not s.all_trust), key=lambda s: s.display_name)
    trust_summaries = sorted((s for s in summaries if s.all_trust), key=lambda s: s.display_name)

    # Written on every view so its download link always matches this exact
    # page — deliberately a separate file from /collections/download's own
    # CSV below, which is bill-level and has no Handling/Confirmed columns.
    action_report_csv_path = Path("output") / f"collections_action_report_{datetime.today().strftime('%Y-%m-%d')}.csv"
    await run_in_threadpool(collections_monitor.write_action_report_csv, invoice_summaries, trust_summaries, action_report_csv_path)

    return render(
        request, "collections_action_report.html", error=None, summaries=summaries,
        invoice_summaries=invoice_summaries, trust_summaries=trust_summaries, clio_base_url=CLIO_BASE_URL,
    )


@router.get("/action-report/download")
async def collections_action_report_download(_: None = Depends(require_auth)):
    path = Path("output") / f"collections_action_report_{datetime.today().strftime('%Y-%m-%d')}.csv"
    if not path.exists():
        return HTMLResponse("No report generated yet — visit the print report page first.", status_code=404)
    return FileResponse(path, filename=path.name, media_type="text/csv")


@router.get("/download")
async def collections_download(_: None = Depends(require_auth)):
    path = Path("output") / f"collections_monitor_{datetime.today().strftime('%Y-%m-%d')}.csv"
    if not path.exists():
        return HTMLResponse("No report generated yet — visit /collections first.", status_code=404)
    return FileResponse(path, filename=path.name, media_type="text/csv")
