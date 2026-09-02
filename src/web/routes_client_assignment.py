"""
routes_client_assignment.py — Client Assignment view: open matters missing
Responsible Attorney, Originating Attorney, and/or Responsible Staff, with
an inline dropdown (fixed attorney/paralegal roster) to assign each one.

See client_assignment.py's module docstring for the underlying Clio API
gotchas — notably that a field can be SET through the API but never cleared
back to blank (Clio's own spec: "null is not valid for this field").
"""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse

import client_assignment
from web.auth import require_auth

router = APIRouter(prefix="/assignments", tags=["assignments"])


def _load():
    session = client_assignment.build_session()
    attorneys, paralegals = client_assignment.get_assignable_users()
    matters = client_assignment.fetch_matters_for_assignment(session)
    return matters, attorneys, paralegals


@router.get("", response_class=HTMLResponse)
async def assignments_home(request: Request, _: None = Depends(require_auth)):
    from web.app import render

    try:
        matters, attorneys, paralegals = await run_in_threadpool(_load)
    except RuntimeError as e:
        return render(request, "client_assignment.html", error=str(e), matters=None)

    missing_count = sum(1 for m in matters if m.missing_any)
    return render(
        request, "client_assignment.html", error=None,
        matters=matters, missing_count=missing_count,
        attorneys=attorneys, paralegals=paralegals,
    )


@router.post("/set")
async def set_assignment(
    matter_id: int = Form(...), field: str = Form(...), user_id: int = Form(...),
    _: None = Depends(require_auth),
):
    if field not in client_assignment.ASSIGNMENT_FIELDS:
        return JSONResponse({"error": f"Unknown field: {field}"}, status_code=400)

    def _do() -> str:
        attorneys, paralegals = client_assignment.get_assignable_users()
        roster = attorneys if client_assignment.FIELD_ROSTER[field] == "attorney" else paralegals
        if user_id not in {u["id"] for u in roster}:
            raise ValueError(f"That's not a valid choice for {client_assignment.FIELD_LABELS[field]}")
        session = client_assignment.build_session()
        return client_assignment.update_matter_field(session, matter_id, field, user_id)

    try:
        name = await run_in_threadpool(_do)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=502)

    return JSONResponse({"success": True, "name": name})


@router.get("/report", response_class=HTMLResponse)
async def assignments_report(request: Request, view: str = "missing", _: None = Depends(require_auth)):
    from web.app import render

    try:
        matters, _, _ = await run_in_threadpool(_load)
    except RuntimeError as e:
        return render(request, "client_assignment_report.html", error=str(e), matters=None)

    if view == "missing":
        matters = [m for m in matters if m.missing_any]

    matters_sorted = sorted(matters, key=lambda m: m.display_number)
    return render(
        request, "client_assignment_report.html", error=None,
        matters=matters_sorted, view=view,
    )
