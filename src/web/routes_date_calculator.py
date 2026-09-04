"""
routes_date_calculator.py — Date Calculator: a generic two-date duration
calculator, plus a matter-aware mode that reads AND writes a matter's Date
of Marriage/Separation, and can save the computed "Length of Marriage"
summary back to Clio as its own field — all from one Save action, so this
tool is self-sufficient rather than sending staff to Moore/Marsden just to
set those two dates (Ted, 2026-09-04: "each tool should not depend on
another tool or force the user to navigate to another one").

Reuses moore_marsden.clio_matter_dates.update_matter_dates() directly for
the write — same two fields, same existing-CustomFieldValue-id handling,
no reason to duplicate it. See date_calculator.py's module docstring for
the Length of Marriage field details.
"""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse

import date_calculator
import matter_matching
from moore_marsden.clio_matter_dates import fetch_matter_dates, update_matter_dates
from web.auth import require_auth

router = APIRouter(prefix="/date-calculator", tags=["date-calculator"])


def _all_matters() -> list[dict]:
    session = date_calculator.build_session()
    # Open + pending, same reasoning as Moore/Marsden's own matter search —
    # Date of Marriage/Separation can plausibly be entered before a matter
    # formally flips to Open.
    matters_raw = matter_matching.fetch_open_matters(session, fields="id,display_number", status="open,pending")
    return sorted(
        ({"id": int(m["id"]), "name": m["display_number"]} for m in matters_raw if m.get("display_number")),
        key=lambda m: m["name"],
    )


def _load_matter(matter_id: int, all_matters: list[dict]) -> dict | None:
    display_number = next((m["name"] for m in all_matters if m["id"] == matter_id), None)
    if display_number is None:
        return None
    session = date_calculator.build_session()
    dom, dos = fetch_matter_dates(session, matter_id)
    current_value, _ = date_calculator.fetch_length_of_marriage(session, matter_id)
    return {
        "id": matter_id, "name": display_number,
        "date_of_marriage": dom, "date_of_separation": dos,
        "current_length_of_marriage": current_value,
    }


@router.get("", response_class=HTMLResponse)
async def date_calculator_home(
    request: Request, matter_id: int | None = None, error: str | None = None,
    _: None = Depends(require_auth),
):
    from web.app import render

    all_matters: list[dict] = []
    matter = None
    try:
        all_matters = await run_in_threadpool(_all_matters)
        if matter_id:
            matter = await run_in_threadpool(_load_matter, matter_id, all_matters)
            if matter is None:
                error = error or "That matter wasn't found among open/pending matters."
    except RuntimeError as e:
        error = error or str(e)

    return render(
        request, "date_calculator.html", all_matters=all_matters, matter=matter, error=error,
        long_term_years=date_calculator.LONG_TERM_MARRIAGE_YEARS,
    )


@router.post("/save")
async def date_calculator_save(
    matter_id: int = Form(...), start_date: str = Form(...), end_date: str = Form(...),
    _: None = Depends(require_auth),
):
    def _do() -> dict:
        start = date_calculator.parse_iso_date(start_date)
        end = date_calculator.parse_iso_date(end_date)
        text_value = date_calculator.calculate_duration(start, end).format()
        session = date_calculator.build_session()
        # Same single explicit click writes all three fields — Save always
        # means "these are the real dates," not "just remember the summary."
        # Date of Marriage/Separation is the more foundational write (the
        # same two fields Moore/Marsden's math depends on); Length of
        # Marriage is a secondary convenience summary. If that summary
        # field write fails (e.g. it doesn't exist in Clio yet), the dates
        # themselves should still be considered saved — not reported as a
        # flat failure that makes it look like nothing happened.
        update_matter_dates(session, matter_id, date_of_marriage=start_date, date_of_separation=end_date)
        try:
            date_calculator.update_length_of_marriage(session, matter_id, text_value)
            warning = None
        except RuntimeError as e:
            warning = f"Date of Marriage/Separation saved, but Length of Marriage wasn't: {e}"
        return {"value": text_value, "warning": warning}

    try:
        result = await run_in_threadpool(_do)
    except ValueError:
        return JSONResponse({"error": "Invalid date."}, status_code=400)
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=502)

    return JSONResponse({"success": True, "value": result["value"], "warning": result["warning"]})
