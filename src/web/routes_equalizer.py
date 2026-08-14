"""
routes_equalizer.py — Asset/debt division worksheets (see CLAUDE.md's
"Equalizer" section).

Unlike the rest of the dashboard (full-page HTML re-renders after every
mutating POST), the worksheet editor is a live spreadsheet-style grid — item
CRUD and the H/W/= assignment buttons are small JSON endpoints the page's own
JS calls directly, autosaving as staff edit, rather than a submit-and-reload
form. The list/new-worksheet page and the settings/finalize actions still
follow the usual full-page-render pattern used everywhere else.

Preview vs. Finalize mirrors Bradford/Legs/Printer's dry-run/confirm split:
/preview.pdf regenerates the PDF live from current draft data and never
touches Clio; /finalize is the one action that uploads it, and is the only
route in this file that writes anything outside data/clio_dashboard.db.
"""

from datetime import datetime

from fastapi import APIRouter, Body, Depends, Form, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse, Response

import equalizer.calc as calc
import equalizer.clio_documents as clio_documents
import equalizer.clio_notes as clio_notes
import equalizer.clio_parties as clio_parties
import equalizer.pdf as pdf
import equalizer.store as store
import matter_matching
from web.auth import require_auth
from web.db import get_connection

router = APIRouter(prefix="/equalizer", tags=["equalizer"])


def _worksheet_or_404(conn, worksheet_id: int) -> dict:
    worksheet = store.get_worksheet(conn, worksheet_id)
    if worksheet is None:
        raise HTTPException(status_code=404, detail=f"No such worksheet: {worksheet_id}")
    return worksheet


async def _create_worksheet_for_matter(conn, matter_id: int) -> int:
    """Always resolves the matter's display_number live from Clio rather
    than trusting any client-supplied name — the one place a new worksheet
    row actually gets created, used both by the plain "start a worksheet"
    form and the matter-lookup route's zero-worksheets-found path."""
    session = clio_documents.build_session()
    try:
        matter = await run_in_threadpool(clio_parties.fetch_matter_summary, session, matter_id)
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return store.create_worksheet(conn, matter_id, matter.get("display_number", str(matter_id)))


@router.get("", response_class=HTMLResponse)
async def equalizer_home(request: Request, _: None = Depends(require_auth)):
    from web.app import render

    conn = get_connection()
    try:
        worksheets = store.list_worksheets(conn, status="draft")
    finally:
        conn.close()

    error = None
    all_matters: list[dict] = []
    try:
        session = clio_documents.build_session()
        matters_raw = await run_in_threadpool(matter_matching.fetch_open_matters, session, fields="id,display_number")
        all_matters = sorted(
            ({"id": int(m["id"]), "name": m["display_number"]} for m in matters_raw if m.get("display_number")),
            key=lambda m: m["name"],
        )
    except RuntimeError as e:
        error = str(e)

    return render(request, "equalizer.html", worksheets=worksheets, all_matters=all_matters, error=error)


@router.get("/lookup", response_class=HTMLResponse)
async def equalizer_lookup(request: Request, matter_id: int, matter_name: str = "", _: None = Depends(require_auth)):
    """Where the landing page's matter search actually lands: a matter with
    no worksheets yet goes straight to a fresh one (the common case — no
    extra click); a matter with existing worksheets (draft or finalized)
    shows the list to recall from instead of silently creating a duplicate."""
    from web.app import render

    conn = get_connection()
    try:
        existing = store.list_worksheets_by_matter(conn, matter_id)
        if not existing:
            worksheet_id = await _create_worksheet_for_matter(conn, matter_id)
            return RedirectResponse(url=f"/equalizer/{worksheet_id}", status_code=303)
        display_name = matter_name or existing[0]["matter_display_number"]
    finally:
        conn.close()

    return render(request, "equalizer_matter.html", matter_id=matter_id, matter_display_number=display_name,
                  worksheets=existing)


@router.post("/new")
async def equalizer_new(request: Request, matter_id: int = Form(...), _: None = Depends(require_auth)):
    conn = get_connection()
    try:
        worksheet_id = await _create_worksheet_for_matter(conn, matter_id)
    finally:
        conn.close()

    return RedirectResponse(url=f"/equalizer/{worksheet_id}", status_code=303)


@router.post("/{worksheet_id}/delete")
async def equalizer_delete_worksheet(
    worksheet_id: int, return_to: str = Form("/equalizer"), _: None = Depends(require_auth),
):
    """Draft-only — a finalized worksheet already has a PDF and a matter
    Note pointing at it in Clio (see clio_documents.py/clio_notes.py), so
    deleting the local record here would orphan both rather than clean
    anything up. return_to lets the three call sites (landing page,
    matter-lookup page, editor toolbar) each bounce back to where they were
    instead of always landing on the generic list; restricted to this
    router's own paths, same as the /login?next= pattern elsewhere in this
    dashboard, so it can't be used as an open redirect."""
    conn = get_connection()
    try:
        worksheet = _worksheet_or_404(conn, worksheet_id)
        if worksheet["status"] != "draft":
            raise HTTPException(status_code=400, detail="Only draft worksheets can be deleted.")
        store.delete_worksheet(conn, worksheet_id)
    finally:
        conn.close()

    target = return_to if return_to.startswith("/equalizer") else "/equalizer"
    return RedirectResponse(url=target, status_code=303)


@router.get("/{worksheet_id}", response_class=HTMLResponse)
async def equalizer_editor(worksheet_id: int, request: Request, _: None = Depends(require_auth)):
    from web.app import render

    conn = get_connection()
    try:
        worksheet = _worksheet_or_404(conn, worksheet_id)
        items = store.list_items(conn, worksheet_id)
    finally:
        conn.close()

    totals = calc.compute_totals(items, worksheet)
    enriched_items = [calc.enrich_item(i, worksheet) for i in items]
    return render(request, "equalizer_worksheet.html", worksheet=worksheet, items=enriched_items, totals=totals)


@router.patch("/{worksheet_id}/settings")
async def equalizer_update_settings(worksheet_id: int, payload: dict = Body(...), _: None = Depends(require_auth)):
    conn = get_connection()
    try:
        _worksheet_or_404(conn, worksheet_id)
        try:
            store.update_worksheet_settings(conn, worksheet_id, **payload)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        worksheet = store.get_worksheet(conn, worksheet_id)
        items = store.list_items(conn, worksheet_id)
    finally:
        conn.close()

    totals = calc.compute_totals(items, worksheet)
    return {"worksheet": worksheet, "totals": totals.__dict__}


@router.post("/{worksheet_id}/settings/autofill-names")
async def equalizer_autofill_names(worksheet_id: int, _: None = Depends(require_auth)):
    """Best-effort prefill for the Settings panel's same-sex first-name
    fields — pulled from the matter's client + Opposing Party contact.
    Never fails the request; a blank name just means "type it in yourself"."""
    conn = get_connection()
    try:
        worksheet = _worksheet_or_404(conn, worksheet_id)
    finally:
        conn.close()

    session = clio_documents.build_session()
    client_name, opposing_name = await run_in_threadpool(
        clio_parties.fetch_default_names, session, worksheet["matter_id"]
    )
    return {"party_a_label": client_name, "party_b_label": opposing_name}


@router.post("/{worksheet_id}/items")
async def equalizer_add_item(worksheet_id: int, _: None = Depends(require_auth)):
    conn = get_connection()
    try:
        worksheet = _worksheet_or_404(conn, worksheet_id)
        item_id = store.add_item(conn, worksheet_id)
        items = store.list_items(conn, worksheet_id)
    finally:
        conn.close()

    item = next(i for i in items if i["id"] == item_id)
    totals = calc.compute_totals(items, worksheet)
    return {"item": calc.enrich_item(item, worksheet), "totals": totals.__dict__}


@router.patch("/{worksheet_id}/items/{item_id}")
async def equalizer_update_item(worksheet_id: int, item_id: int, payload: dict = Body(...), _: None = Depends(require_auth)):
    conn = get_connection()
    try:
        worksheet = _worksheet_or_404(conn, worksheet_id)
        try:
            store.update_item(conn, item_id, **payload)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        items = store.list_items(conn, worksheet_id)
    finally:
        conn.close()

    item = next((i for i in items if i["id"] == item_id), None)
    if item is None:
        raise HTTPException(status_code=404, detail=f"No such item: {item_id}")
    totals = calc.compute_totals(items, worksheet)
    return {"item": calc.enrich_item(item, worksheet), "totals": totals.__dict__}


@router.post("/{worksheet_id}/items/{item_id}/assign")
async def equalizer_assign_item(worksheet_id: int, item_id: int, payload: dict = Body(...), _: None = Depends(require_auth)):
    side = payload.get("side")
    if side not in ("a", "b", "split"):
        raise HTTPException(status_code=400, detail=f"side must be 'a', 'b', or 'split', got {side!r}")

    conn = get_connection()
    try:
        worksheet = _worksheet_or_404(conn, worksheet_id)
        items = store.list_items(conn, worksheet_id)
        item = next((i for i in items if i["id"] == item_id), None)
        if item is None:
            raise HTTPException(status_code=404, detail=f"No such item: {item_id}")

        before_a, before_b = calc.assign_before_tax(item["fmv"], item["debt"], side)
        store.update_item(conn, item_id, before_tax_a=before_a, before_tax_b=before_b)
        items = store.list_items(conn, worksheet_id)
    finally:
        conn.close()

    item = next(i for i in items if i["id"] == item_id)
    totals = calc.compute_totals(items, worksheet)
    return {"item": calc.enrich_item(item, worksheet), "totals": totals.__dict__}


@router.delete("/{worksheet_id}/items/{item_id}")
async def equalizer_delete_item(worksheet_id: int, item_id: int, _: None = Depends(require_auth)):
    conn = get_connection()
    try:
        worksheet = _worksheet_or_404(conn, worksheet_id)
        store.delete_item(conn, item_id)
        items = store.list_items(conn, worksheet_id)
    finally:
        conn.close()

    totals = calc.compute_totals(items, worksheet)
    return {"totals": totals.__dict__}


@router.get("/{worksheet_id}/preview.pdf")
async def equalizer_preview_pdf(worksheet_id: int, _: None = Depends(require_auth)):
    conn = get_connection()
    try:
        worksheet = _worksheet_or_404(conn, worksheet_id)
        items = store.list_items(conn, worksheet_id)
    finally:
        conn.close()

    pdf_bytes = await run_in_threadpool(pdf.render_worksheet_pdf, worksheet, items)
    return Response(
        content=pdf_bytes, media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="equalizer-{worksheet["matter_display_number"]}.pdf"'},
    )


@router.post("/{worksheet_id}/finalize", response_class=HTMLResponse)
async def equalizer_finalize(worksheet_id: int, request: Request, _: None = Depends(require_auth)):
    from web.app import render

    conn = get_connection()
    try:
        worksheet = _worksheet_or_404(conn, worksheet_id)
        items = store.list_items(conn, worksheet_id)
    finally:
        conn.close()

    if not items:
        totals = calc.compute_totals(items, worksheet)
        return render(request, "equalizer_worksheet.html", worksheet=worksheet, items=[], totals=totals,
                      error="Add at least one row before finalizing.")

    pdf_bytes = await run_in_threadpool(pdf.render_worksheet_pdf, worksheet, items)
    filename = f"equalizer-{datetime.today().strftime('%Y-%m-%d')}.pdf"

    conn = get_connection()
    try:
        try:
            session = clio_documents.build_session()
            document_id = await run_in_threadpool(
                clio_documents.upload_pdf, session, worksheet["matter_id"], filename, pdf_bytes,
            )
            store.finalize_worksheet(conn, worksheet_id, document_id)
        except RuntimeError as e:
            worksheet = store.get_worksheet(conn, worksheet_id)
            items = store.list_items(conn, worksheet_id)
            totals = calc.compute_totals(items, worksheet)
            enriched_items = [calc.enrich_item(i, worksheet) for i in items]
            return render(request, "equalizer_worksheet.html", worksheet=worksheet, items=enriched_items, totals=totals,
                          error=f"Finalize failed: {e}")

        worksheet = store.get_worksheet(conn, worksheet_id)
        items = store.list_items(conn, worksheet_id)
    finally:
        conn.close()

    # A Note pointing back to this worksheet is a secondary, best-effort
    # step — the PDF landing in Evidence is the artifact that actually
    # matters and has already succeeded by this point, so a Note failure
    # downgrades to a softer notice rather than failing the whole Finalize.
    notice = f"Finalized and saved to Clio as {filename} in the matter's Evidence folder."
    try:
        await run_in_threadpool(clio_notes.create_worksheet_note, session, worksheet["matter_id"], worksheet_id)
        notice += " A note linking back to this worksheet was also posted on the matter."
    except RuntimeError as e:
        notice += f" (Could not post a matter note linking back to it: {e})"

    totals = calc.compute_totals(items, worksheet)
    enriched_items = [calc.enrich_item(i, worksheet) for i in items]
    return render(request, "equalizer_worksheet.html", worksheet=worksheet, items=enriched_items, totals=totals,
                  notice=notice)
