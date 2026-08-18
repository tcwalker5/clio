"""
routes_staff_unbilled.py — Staff Unbilled Report dashboard page.

One summary row per staff member (matter count, total unbilled, total
shortfall, total owed on their own matters), expandable to the underlying
per-matter rows — matter name, client, unbilled activity, matter WIP,
trust balance, shortfall, owed. `run_pipeline()` already returns only the
at-risk rows (trust balance doesn't cover matter WIP) — see
staff_unbilled_monitor.py's module docstring for that filter's exact
definition and CLAUDE.md's "Staff Unbilled Report" section for the
matter-level-vs-per-user caveat on the owed/WIP/trust figures.

Read-only, like Collections and Trust Monitor's WIP table — runs live on
every page view.
"""

from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse

import staff_unbilled_monitor
from web.auth import require_auth

router = APIRouter(prefix="/staff-unbilled", tags=["staff_unbilled"])


@router.get("", response_class=HTMLResponse)
async def staff_unbilled_home(request: Request, _: None = Depends(require_auth)):
    from web.app import render

    try:
        rows = await run_in_threadpool(staff_unbilled_monitor.run_pipeline)
    except RuntimeError as e:
        return render(request, "staff_unbilled.html", error=str(e), summaries=None)

    summaries = staff_unbilled_monitor.build_user_summaries(rows)
    matter_count = len({r.matter_id for r in rows})
    total_unbilled = sum(r.total_unbilled for r in rows)
    total_shortfall = sum(r.shortfall for r in rows)

    return render(
        request, "staff_unbilled.html", error=None,
        summaries=summaries, matter_count=matter_count,
        total_unbilled=total_unbilled, total_shortfall=total_shortfall,
    )


@router.get("/download")
async def staff_unbilled_download(_: None = Depends(require_auth)):
    path = Path("output") / f"staff_unbilled_{datetime.today().strftime('%Y-%m-%d')}.csv"
    if not path.exists():
        return HTMLResponse("No report generated yet — visit /staff-unbilled first.", status_code=404)
    return FileResponse(path, filename=path.name, media_type="text/csv")
