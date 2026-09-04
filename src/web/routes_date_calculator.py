"""
routes_date_calculator.py — Date Calculator: a generic two-date duration
calculator, plus a matter-aware mode that reads (never writes) a matter's
Date of Marriage/Separation and can save the computed "Length of Marriage"
summary back to Clio as its own field.

See date_calculator.py's module docstring for the Clio field details and
why this stays read-only on Date of Marriage/Separation themselves.
"""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse

import date_calculator
import matter_matching
from moore_marsden.clio_matter_dates import fetch_matter_dates
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
    def _do() -> str:
        start = date_calculator.parse_iso_date(start_date)
        end = date_calculator.parse_iso_date(end_date)
        text_value = date_calculator.calculate_duration(start, end).format()
        session = date_calculator.build_session()
        date_calculator.update_length_of_marriage(session, matter_id, text_value)
        return text_value

    try:
        text_value = await run_in_threadpool(_do)
    except ValueError:
        return JSONResponse({"error": "Invalid date."}, status_code=400)
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=502)

    return JSONResponse({"success": True, "value": text_value})
