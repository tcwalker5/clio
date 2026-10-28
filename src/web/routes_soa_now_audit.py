"""
routes_soa_now_audit.py — Case Closing page (formerly "SoA/NoW Close
Audit"; URL kept as /soa-now-audit so existing bookmarks still work): every
open/pending matter, checked for a filed Substitution of Attorney, Notice
of Withdrawal, or Request for Dismissal, with a Close Case action and a
"Mark SoA/NoW Filed" checkbox — plus a Trust Refunds section listing closed
matters whose final bill is settled but still hold trust (added 2026-10-14).

See soa_now_audit.py's module docstring for why this is deliberately
separate from Client Assignment (no attorney/staff-assignment concerns
here), why Close Case is never gated on anything this page finds, and why
the scan is a slow, explicitly-triggered action rather than something run
on every page view.

**Results persist in data/clio_dashboard.db since 2026-10-14** (Ted: "If I
run the audit and change web page, so I lose the time it was run?") —
before that they lived in module-level globals, which survived page
changes but not a dashboard restart. The page now renders the latest
case_closing_runs row for each section, and Run Audit only re-scans
matters whose documents changed (see soa_now_audit.run_audit()); "Full
rescan" forces every matter. Every handler that touches the DB opens its
own connection inside the threadpool function — get_connection() doesn't
set check_same_thread=False (see src/web/CLAUDE.md's Concurrency note).
"""

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse

import client_assignment
import soa_now_audit
from web.auth import require_auth
from web.db import get_connection

router = APIRouter(prefix="/soa-now-audit", tags=["soa_now_audit"])


def _format_duration(seconds: float) -> str:
    total = round(seconds)
    minutes, secs = divmod(total, 60)
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _local(iso: str) -> datetime:
    """Stored UTC timestamp -> this machine's local time, for display."""
    return datetime.fromisoformat(iso).astimezone().replace(tzinfo=None)


def _load_page_state() -> dict:
    conn = get_connection()
    try:
        audit = soa_now_audit.latest_run(conn, soa_now_audit.RUN_KIND_AUDIT)
        refunds = soa_now_audit.latest_run(conn, soa_now_audit.RUN_KIND_REFUNDS)
    finally:
        conn.close()

    state: dict = {"matters": None, "stats": None, "last_run_at": None, "duration_text": None,
                   "scanned_count": None, "rescanned_count": None, "full_scan": False,
                   "refunds": None, "refunds_at": None, "refund_total": 0.0}
    if audit is not None:
        matters = sorted((soa_now_audit.AuditMatter.from_dict(d) for d in json.loads(audit["results_json"])), key=lambda m: m.name)
        started, finished = datetime.fromisoformat(audit["started_at"]), datetime.fromisoformat(audit["finished_at"])
        state.update(
            matters=matters,
            stats={
                "filed": sum(1 for m in matters if m.can_close),
                "review": sum(1 for m in matters if m.findings and not m.can_close),
            },
            last_run_at=_local(audit["finished_at"]),
            duration_text=_format_duration((finished - started).total_seconds()),
            scanned_count=audit["scanned_count"],
            rescanned_count=audit["rescanned_count"],
            full_scan=bool(audit["full_scan"]),
        )
    if refunds is not None:
        rows = [soa_now_audit.RefundCandidate(**d) for d in json.loads(refunds["results_json"])]
        state.update(
            refunds=rows,
            refunds_at=_local(refunds["finished_at"]),
            refund_total=sum(r.matter_trust + r.client_trust for r in rows),
        )
    return state


@router.get("", response_class=HTMLResponse)
async def soa_now_audit_home(request: Request, _: None = Depends(require_auth)):
    from web.app import render

    state = await run_in_threadpool(_load_page_state)
    return render(
        request, "soa_now_audit.html", error=None,
        clio_folder_url=soa_now_audit.clio_folder_url,
        **state,
    )


@router.post("/refunds")
async def soa_now_audit_refunds(_: None = Depends(require_auth)):
    def _do() -> int:
        started_at = datetime.now(timezone.utc)
        rows = soa_now_audit.fetch_refund_candidates(client_assignment.build_session())
        conn = get_connection()
        try:
            soa_now_audit.record_run(conn, soa_now_audit.RUN_KIND_REFUNDS, started_at, [r.to_dict() for r in rows])
        finally:
            conn.close()
        return len(rows)

    try:
        count = await run_in_threadpool(_do)
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    return JSONResponse({"success": True, "count": count})


@router.post("/run")
async def soa_now_audit_run(full: bool = Form(False), _: None = Depends(require_auth)):
    def _do() -> tuple[int, int, int]:
        started_at = datetime.now(timezone.utc)
        conn = get_connection()
        try:
            results, rescanned = soa_now_audit.run_audit(client_assignment.build_session(), conn, full=full)
            shown = soa_now_audit.filter_for_display(results)
            soa_now_audit.record_run(
                conn, soa_now_audit.RUN_KIND_AUDIT, started_at, [m.to_dict() for m in shown],
                full=full, scanned_count=len(results), rescanned_count=rescanned,
            )
        finally:
            conn.close()
        return len(shown), len(results), rescanned

    try:
        count, scanned, rescanned = await run_in_threadpool(_do)
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    return JSONResponse({"success": True, "count": count, "scanned": scanned, "rescanned": rescanned})


@router.post("/close")
async def soa_now_audit_close(matter_id: int = Form(...), _: None = Depends(require_auth)):
    def _do() -> str:
        status = client_assignment.close_matter(client_assignment.build_session(), matter_id)
        # Drop it from the saved results so a later reload (without a fresh
        # Run Audit) doesn't show a now-closed matter as still open — the
        # client already removes the card for the current view.
        conn = get_connection()
        try:
            soa_now_audit.update_latest_results(
                conn, soa_now_audit.RUN_KIND_AUDIT,
                lambda rows: [r for r in rows if r["matter_id"] != matter_id],
            )
        finally:
            conn.close()
        return status

    try:
        status = await run_in_threadpool(_do)
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    return JSONResponse({"success": True, "status": status})


@router.post("/mark")
async def soa_now_audit_mark(matter_id: int = Form(...), marked: bool = Form(...), _: None = Depends(require_auth)):
    def _do() -> None:
        soa_now_audit.set_soa_now_filed(client_assignment.build_session(), matter_id, marked)

        def _apply(rows: list[dict]) -> list[dict]:
            for r in rows:
                if r["matter_id"] == matter_id:
                    r["soa_now_marked"] = marked
            return rows

        conn = get_connection()
        try:
            soa_now_audit.update_latest_results(conn, soa_now_audit.RUN_KIND_AUDIT, _apply)
        finally:
            conn.close()

    try:
        await run_in_threadpool(_do)
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    return JSONResponse({"success": True})
