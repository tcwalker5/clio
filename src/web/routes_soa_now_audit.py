"""
routes_soa_now_audit.py — SoA/NoW Close Audit page: a fixed list of old
matters, each checked for a filed Substitution of Attorney or Notice of
Withdrawal (Clio first, then the legacy Y: drive if Clio has nothing), with
a Close Case action and a simple "Mark SoA/NoW Filed" checkbox.

See soa_now_audit.py's module docstring for why this is deliberately
separate from Client Assignment (no attorney/staff-assignment concerns
here) and why Close Case is never gated on anything this page finds.
"""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse

import client_assignment
import soa_now_audit
from web.auth import require_auth

router = APIRouter(prefix="/soa-now-audit", tags=["soa_now_audit"])


def _load():
    session = client_assignment.build_session()
    return soa_now_audit.run_audit(session)


@router.get("", response_class=HTMLResponse)
async def soa_now_audit_home(request: Request, _: None = Depends(require_auth)):
    from web.app import render

    try:
        matters = await run_in_threadpool(_load)
    except RuntimeError as e:
        return render(request, "soa_now_audit.html", error=str(e), matters=None)

    matters_sorted = sorted(matters, key=lambda m: m.name)
    stats = {
        "closed": sum(1 for m in matters if m.status == "closed"),
        "filed": sum(1 for m in matters if m.can_close),
        "review": sum(1 for m in matters if m.findings and not m.can_close),
        "y_filed": sum(1 for m in matters if m.y_drive and m.y_drive.get("has_filed")),
        "unresolved": sum(1 for m in matters if m.status in ("not_found", "ambiguous")),
    }
    return render(
        request, "soa_now_audit.html", error=None,
        matters=matters_sorted, stats=stats,
        clio_folder_url=soa_now_audit.clio_folder_url,
    )


@router.post("/close")
async def soa_now_audit_close(matter_id: int = Form(...), _: None = Depends(require_auth)):
    def _do() -> str:
        session = client_assignment.build_session()
        return client_assignment.close_matter(session, matter_id)

    try:
        status = await run_in_threadpool(_do)
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=502)

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

    return JSONResponse({"success": True})
