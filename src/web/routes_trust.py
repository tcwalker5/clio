"""
routes_trust.py — WIP-vs-trust report, self-service for the responsible
attorney.

See trust_monitor.py's module docstring for the full 2026-09-16 redesign
context: live Clio trust-request sending is blocked/on hold (reference/
billing-monitors.md), so this page is now a pure report rather than a
send/review workflow — no Send or Pause routes remain, since there's
nothing left to send or pause. The only mutating route is /set-target,
saved instantly on change (same pattern as Client Assignment's/
Collections' own dropdowns) — a plain JSON endpoint, not a full-page
form post, since nothing else on the page depends on the new value.

The report table (trust_monitor.run_pipeline + build_report_rows) is
read-only and runs live on every page view, like Court Calendar Sync —
the underlying Clio calls are cheap under real data volumes (a few hundred
matters/bills, 1-2 pages each).
"""

from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

import trust_monitor
from web.auth import require_auth
from web.db import get_connection

router = APIRouter(prefix="/trust", tags=["trust"])


@router.get("", response_class=HTMLResponse)
async def trust_home(request: Request, _: None = Depends(require_auth)):
    from web.app import render

    try:
        statuses = await run_in_threadpool(trust_monitor.run_pipeline)
    except RuntimeError as e:
        return render(request, "trust.html", error=str(e), rows=None)

    conn = get_connection()
    try:
        rows = trust_monitor.build_report_rows(conn, statuses)
    finally:
        conn.close()

    rows_sorted = sorted(rows, key=lambda r: r.cushion)
    flagged_count = sum(1 for r in rows_sorted if r.flagged)

    # Attorney filter options: whoever's actually responsible for a matter
    # in this data set, not a hardcoded roster (client_assignment.py's fixed
    # ATTORNEY_NAMES list is for the assign-a-name dropdown; this is just
    # filtering what's already on screen) — plus "Unassigned" if any matter
    # here is missing one (see /assignments to fix that).
    attorneys = sorted({r.attorney_name for r in rows_sorted if r.attorney_name})
    has_unassigned = any(not r.attorney_name for r in rows_sorted)

    return render(
        request, "trust.html", error=None,
        rows=rows_sorted, flagged_count=flagged_count,
        trust_minimum=trust_monitor.TRUST_MINIMUM,
        attorneys=attorneys, has_unassigned=has_unassigned,
    )


@router.get("/download")
async def trust_download(_: None = Depends(require_auth)):
    path = Path("output") / f"trust_monitor_{datetime.today().strftime('%Y-%m-%d')}.csv"
    if not path.exists():
        return HTMLResponse("No report generated yet — visit /trust first.", status_code=404)
    return FileResponse(path, filename=path.name, media_type="text/csv")


@router.post("/set-target")
async def trust_set_target(
    matter_id: int = Form(...), target_amount: str = Form(""), _: None = Depends(require_auth),
):
    """Instant-persist, no confirm button — an empty field clears the
    override back to the $2,500 firm default rather than storing nothing
    meaningful; the response returns that resolved effective value so the
    field can show what actually took effect."""
    raw = target_amount.strip().replace(",", "")
    try:
        amount = float(raw) if raw else None
    except ValueError:
        return JSONResponse({"error": f"'{target_amount}' isn't a valid dollar amount."}, status_code=400)
    if amount is not None and amount < 0:
        return JSONResponse({"error": "Target amount can't be negative."}, status_code=400)

    conn = get_connection()
    try:
        trust_monitor.set_matter_target(conn, matter_id, amount)
    finally:
        conn.close()

    effective = amount if amount is not None else trust_monitor.TRUST_MINIMUM
    return JSONResponse({"success": True, "target_amount": f"{effective:,.2f}"})
