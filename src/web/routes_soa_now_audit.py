"""
routes_soa_now_audit.py — SoA/NoW Close Audit page: every open/pending
matter, checked for a filed Substitution of Attorney or Notice of
Withdrawal, with a Close Case action and a "Mark SoA/NoW Filed" checkbox.

See soa_now_audit.py's module docstring for why this is deliberately
separate from Client Assignment (no attorney/staff-assignment concerns
here), why Close Case is never gated on anything this page finds, and why
the scan is a slow, explicitly-triggered action rather than something run
on every page view.
"""

from datetime import datetime

from fastapi import APIRouter, Depends, Form, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse

import client_assignment
import soa_now_audit
from web.auth import require_auth

router = APIRouter(prefix="/soa-now-audit", tags=["soa_now_audit"])

# In-memory cache of the last completed run, same "in-memory handoff"
# pattern this app already uses for preview_store.py's dry-run previews —
# a full scan is slow enough (several minutes across the whole open
# caseload) that recomputing it on every page view would make the page
# effectively unusable; staff trigger a run explicitly and it stays put
# until the next one. Lost on a dashboard restart, same tradeoff
# preview_store.py already accepts — acceptable here since re-running is
# just clicking the button again, nothing is lost that Clio doesn't
# already hold (the audit itself is read-only against Clio; only Close
# Case and Mark SoA/NoW Filed write, and both write straight to Clio, not
# this cache).
_last_results: list[soa_now_audit.AuditMatter] | None = None
_last_run_at: datetime | None = None
# Total matters actually scanned by the last run, kept separate from
# len(_last_results) since 2026-09-24 — _last_results now only holds
# matters that passed soa_now_audit.filter_for_display() (a sign/conformed/
# conf signal on a non-opposing finding), so its length alone can no longer
# answer "how many did we scan."
_last_scanned_count: int | None = None
# Wall-clock duration of the last run, in seconds — lets staff see whether
# "several minutes" is drifting longer over time (growing caseload, more
# frequent 429 retries) before it becomes a real problem, and calibrate
# expectations before clicking Run Audit again (Ted, 2026-09-24).
_last_run_duration_seconds: float | None = None


def _run_audit_now() -> list[soa_now_audit.AuditMatter]:
    session = client_assignment.build_session()
    return soa_now_audit.run_audit(session)


def _format_duration(seconds: float) -> str:
    total = round(seconds)
    minutes, secs = divmod(total, 60)
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


@router.get("", response_class=HTMLResponse)
async def soa_now_audit_home(request: Request, _: None = Depends(require_auth)):
    from web.app import render

    matters_sorted = sorted(_last_results, key=lambda m: m.name) if _last_results is not None else None
    stats = None
    if matters_sorted is not None:
        stats = {
            "filed": sum(1 for m in matters_sorted if m.can_close),
            "review": sum(1 for m in matters_sorted if m.findings and not m.can_close),
        }
    duration_text = _format_duration(_last_run_duration_seconds) if _last_run_duration_seconds is not None else None
    return render(
        request, "soa_now_audit.html", error=None,
        matters=matters_sorted, stats=stats, last_run_at=_last_run_at,
        scanned_count=_last_scanned_count, duration_text=duration_text,
        clio_folder_url=soa_now_audit.clio_folder_url,
    )


@router.post("/run")
async def soa_now_audit_run(_: None = Depends(require_auth)):
    global _last_results, _last_run_at, _last_scanned_count, _last_run_duration_seconds
    started_at = datetime.now()
    try:
        results = await run_in_threadpool(_run_audit_now)
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    _last_scanned_count = len(results)
    _last_results = soa_now_audit.filter_for_display(results)
    _last_run_at = datetime.now()
    _last_run_duration_seconds = (_last_run_at - started_at).total_seconds()
    return JSONResponse({"success": True, "count": len(_last_results), "scanned": _last_scanned_count})


@router.post("/close")
async def soa_now_audit_close(matter_id: int = Form(...), _: None = Depends(require_auth)):
    global _last_results

    def _do() -> str:
        session = client_assignment.build_session()
        return client_assignment.close_matter(session, matter_id)

    try:
        status = await run_in_threadpool(_do)
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=502)

    # Drop it from the cached results so a later page reload (without a
    # fresh Run Audit) doesn't show a now-closed matter as still open —
    # the client already updates the row in place for the current view.
    if _last_results is not None:
        _last_results = [m for m in _last_results if m.matter_id != matter_id]

    return JSONResponse({"success": True, "status": status})


@router.post("/mark")
async def soa_now_audit_mark(matter_id: int = Form(...), marked: bool = Form(...), _: None = Depends(require_auth)):
    def _do() -> None:
        session = client_assignment.build_session()
        soa_now_audit.set_soa_now_filed(session, matter_id, marked)

    try:
        await run_in_threadpool(_do)
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=502)

    if _last_results is not None:
        for m in _last_results:
            if m.matter_id == matter_id:
                m.soa_now_marked = marked
                break

    return JSONResponse({"success": True})
