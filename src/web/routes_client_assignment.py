"""
routes_client_assignment.py — Client Assignment view: open matters missing
Responsible Attorney, Originating Attorney, and/or Responsible Staff, with
an inline dropdown (fixed attorney/paralegal roster) to assign each one.

See client_assignment.py's module docstring for the underlying Clio API
gotchas — notably that a field can be SET through the API but never cleared
back to blank (Clio's own spec: "null is not valid for this field").
"""

import math

from fastapi import APIRouter, Depends, Form, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse

import client_assignment
from web.auth import require_auth

router = APIRouter(prefix="/assignments", tags=["assignments"])

# First three categorical slots of this project's validated default palette
# (see the dataviz skill's references/palette.md) — passes the CVD/contrast
# checks for a 1-3 series chart. "Unassigned" isn't a person, so it gets the
# palette's muted/gap gray instead of continuing the categorical sequence —
# the same "badge the gap, don't blend it in" convention as the rest of
# this app (Collections' trust-request badges, etc.), not a 4th team member.
CATEGORICAL_COLORS = ["#2a78d6", "#eb6834", "#1baf7a"]
UNASSIGNED_COLOR = "#898781"


def build_pie_chart(counts: list[tuple[str, int]], size: int = 200) -> dict:
    """Server-rendered SVG pie geometry — no chart library, matching this
    app's plain FastAPI+Jinja2+vanilla-JS stack. Returns `slices` (one dict
    per non-zero count, with a ready-to-render `<path d=...>`) and `legend`
    (every count including zero, so a roster member with no matters yet
    still gets listed) — same color for a name in both."""
    total = sum(c for _, c in counts)
    cx = cy = size / 2
    r = size / 2 - 4

    legend = []
    slices = []
    angle = -90.0  # 12 o'clock, sweeping clockwise
    color_index = 0
    for name, count in counts:
        if name == "Unassigned":
            color = UNASSIGNED_COLOR
        else:
            color = CATEGORICAL_COLORS[color_index % len(CATEGORICAL_COLORS)]
            color_index += 1
        percent = round(count / total * 100) if total else 0
        legend.append({"name": name, "count": count, "percent": percent, "color": color})

        if count <= 0:
            continue
        fraction = count / total
        sweep = fraction * 360

        if count == total:
            # A full-circle single slice degenerates to a zero-length arc —
            # two semicircle arcs draw a complete circle instead.
            path = f"M {cx - r} {cy} A {r} {r} 0 1 1 {cx + r} {cy} A {r} {r} 0 1 1 {cx - r} {cy} Z"
        else:
            x1 = cx + r * math.cos(math.radians(angle))
            y1 = cy + r * math.sin(math.radians(angle))
            end_angle = angle + sweep
            x2 = cx + r * math.cos(math.radians(end_angle))
            y2 = cy + r * math.sin(math.radians(end_angle))
            large_arc = 1 if sweep > 180 else 0
            path = f"M {cx} {cy} L {x1:.2f} {y1:.2f} A {r} {r} 0 {large_arc} 1 {x2:.2f} {y2:.2f} Z"
            angle = end_angle

        slices.append({"name": name, "count": count, "percent": percent, "color": color, "path": path})

    return {"slices": slices, "legend": legend, "total": total}


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


@router.get("/caseload", response_class=HTMLResponse)
async def assignments_caseload(request: Request, _: None = Depends(require_auth)):
    from web.app import render

    try:
        matters, _, _ = await run_in_threadpool(_load)
    except RuntimeError as e:
        return render(request, "client_assignment_caseload.html", error=str(e), attorney_chart=None, paralegal_chart=None)

    attorney_counts, paralegal_counts = client_assignment.build_caseload(matters)
    return render(
        request, "client_assignment_caseload.html", error=None,
        attorney_chart=build_pie_chart(attorney_counts),
        paralegal_chart=build_pie_chart(paralegal_counts),
    )


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
