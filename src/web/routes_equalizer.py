"""
routes_equalizer.py — Asset/debt division worksheets (see CLAUDE.md's
"Equalizer" section).

Unlike the rest of the dashboard (full-page HTML re-renders after every
mutating POST), the worksheet editor is a live spreadsheet-style grid — item
CRUD and the H/W/= assignment buttons are small JSON endpoints the page's own
JS calls directly, autosaving as staff edit, rather than a submit-and-reload
form. The list/new-worksheet page and the settings/save actions still
follow the usual full-page-render pattern used everywhere else.

/preview.pdf regenerates the PDF live from current draft data and never
touches Clio; /save is the only route in this file that writes anything
outside data/clio_dashboard.db. Unlike a typical dry-run/confirm split
elsewhere in this dashboard (Bradford/Legs/Printer), /save isn't a one-way
door — a worksheet stays editable after saving, and clicking Save to Clio
again just pushes a new Document version rather than locking anything (Ted,
2026-08-14: "nothing is ever final" in this line of work).
"""

import logging
import re
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


# Allowlist rather than a denylist — the filename ends up as a URL path
# segment in Clio's own presigned S3 upload URL
# (/uploads/document_version/file/{uuid}/{filename}), so keeping it to a
# conservative safe set avoids relying on Clio's backend to correctly
# encode whatever a staff member happens to type.
_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9 _\-().]")


def _sanitize_filename(raw: str, fallback: str) -> str:
    cleaned = re.sub(r"\s+", " ", _UNSAFE_FILENAME_CHARS.sub("", raw)).strip()[:150]
    if not cleaned:
        cleaned = fallback
    if not cleaned.lower().endswith(".pdf"):
        cleaned += ".pdf"
    return cleaned


async def _create_worksheet_for_matter(conn, matter_id: int) -> int:
    """Always resolves the matter's display_number live from Clio rather
    than trusting any client-supplied name — the one place a new worksheet
    row actually gets created, used both by the plain "start a worksheet"
    form and the matter-lookup route's zero-worksheets-found path. Starts
    with one blank item rather than an empty grid — a brand-new worksheet
    with zero rows looked broken (nothing to click H/W/= on, no obvious
    next step) versus one blank row waiting to be filled in."""
    session = clio_documents.build_session()
    try:
        matter = await run_in_threadpool(clio_parties.fetch_matter_summary, session, matter_id)
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    worksheet_id = store.create_worksheet(conn, matter_id, matter.get("display_number", str(matter_id)))
    store.add_item(conn, worksheet_id)
    return worksheet_id


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
        # Includes Pending, not just Open — Equalizer's own designated test
        # matter (DOE, JANE) is Pending, and a real matter can plausibly
        # need an asset/debt worksheet started before its status flips to
        # Open too. Every other caller of fetch_open_matters in this repo
        # keeps the plain "open" default untouched.
        matters_raw = await run_in_threadpool(
            matter_matching.fetch_open_matters, session, fields="id,display_number", status="open,pending",
        )
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
    extra click); a matter with existing worksheets (draft or saved to
    Clio) shows the list to recall from instead of silently creating a
    duplicate."""
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

    # Same live trash check as the worksheet editor page — a handful of
    # rows at most here, so checking each one is cheap, and this is exactly
    # where staff go to recall a saved worksheet, so a stale "saved" badge
    # would be actively misleading right where it matters most.
    trashed_by_id: dict[int, bool] = {}
    session = clio_documents.build_session()
    for w in existing:
        if w["clio_document_id"]:
            try:
                trashed_by_id[w["id"]] = await run_in_threadpool(
                    clio_documents.is_document_trashed, session, w["clio_document_id"],
                )
            except RuntimeError as e:
                logging.warning("Could not check Clio trash status for worksheet %s: %s", w["id"], e)

    return render(request, "equalizer_matter.html", matter_id=matter_id, matter_display_number=display_name,
                  worksheets=existing, trashed_by_id=trashed_by_id)


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
    """Blocked once a worksheet has ever been saved to Clio (clio_document_id
    set) — it already has a real Document + matter Note pointing at it there
    (see clio_documents.py/clio_notes.py), so deleting the local record would
    orphan both rather than clean anything up. Deliberately keyed off
    clio_document_id, not status — a saved worksheet stays editable
    indefinitely (Ted, 2026-08-14), so status alone no longer reliably means
    "hasn't touched Clio yet." return_to lets the three call sites (landing
    page, matter-lookup page, editor toolbar) each bounce back to where they
    were instead of always landing on the generic list; restricted to this
    router's own paths, same as the /login?next= pattern elsewhere in this
    dashboard, so it can't be used as an open redirect."""
    conn = get_connection()
    try:
        worksheet = _worksheet_or_404(conn, worksheet_id)
        if worksheet["clio_document_id"] is not None:
            raise HTTPException(status_code=400, detail="Only worksheets never saved to Clio can be deleted.")
        store.delete_worksheet(conn, worksheet_id)
    finally:
        conn.close()

    target = return_to if return_to.startswith("/equalizer") else "/equalizer"
    return RedirectResponse(url=target, status_code=303)


@router.post("/{worksheet_id}/duplicate")
async def equalizer_duplicate(worksheet_id: int, _: None = Depends(require_auth)):
    """"Save As" — clone this worksheet's settings and rows into a brand
    new draft for a variant scenario (Ted, 2026-08-14: staff may run
    several scenarios for the same matter in one day), rather than
    retyping everything for a small change. The clone starts completely
    unlinked from Clio (own draft status, no clio_document_id) — it isn't
    a version of the original, it's an independent worksheet that happens
    to start with the same numbers."""
    conn = get_connection()
    try:
        _worksheet_or_404(conn, worksheet_id)
        new_worksheet_id = store.duplicate_worksheet(conn, worksheet_id)
    finally:
        conn.close()

    return RedirectResponse(url=f"/equalizer/{new_worksheet_id}", status_code=303)


@router.get("/{worksheet_id}", response_class=HTMLResponse)
async def equalizer_editor(worksheet_id: int, request: Request, _: None = Depends(require_auth)):
    """Checks live whether the linked Clio document has been trashed
    directly in Clio (Clio's own DELETE is a soft delete — deleted_at gets
    set, the record stays fetchable for 30 days) — otherwise the "Saved to
    Clio" badge would keep claiming that even after someone deleted it on
    the Clio side, which is exactly what happened to a real worksheet here
    2026-08-14. Best-effort: a failure to check (e.g. Clio temporarily
    unreachable) just skips the warning rather than breaking the page."""
    from web.app import render

    conn = get_connection()
    try:
        worksheet = _worksheet_or_404(conn, worksheet_id)
        items = store.list_items(conn, worksheet_id)
    finally:
        conn.close()

    clio_document_trashed = False
    if worksheet["clio_document_id"]:
        try:
            session = clio_documents.build_session()
            clio_document_trashed = await run_in_threadpool(
                clio_documents.is_document_trashed, session, worksheet["clio_document_id"],
            )
        except RuntimeError as e:
            logging.warning("Could not check Clio trash status for worksheet %s: %s", worksheet_id, e)

    totals = calc.compute_totals(items, worksheet)
    enriched_items = [calc.enrich_item(i, worksheet) for i in items]
    assign_label_a, assign_label_b = calc.assign_button_labels(worksheet)
    return render(request, "equalizer_worksheet.html", worksheet=worksheet, items=enriched_items, totals=totals,
                  assign_label_a=assign_label_a, assign_label_b=assign_label_b,
                  clio_document_trashed=clio_document_trashed)


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


@router.post("/{worksheet_id}/save", response_class=HTMLResponse)
async def equalizer_save_to_clio(
    worksheet_id: int, request: Request, filename: str = Form(""), _: None = Depends(require_auth),
):
    """Repeatable, not one-way (Ted, 2026-08-14: "nothing is ever final" in
    this line of work) — a worksheet stays editable after this and Save to
    Clio can be clicked again any time. Re-saves push a new Document
    *version* under the same clio_document_id (see clio_documents.py)
    rather than a duplicate file, and only the very first save posts the
    matter Note — the link it contains stays correct across later edits, so
    reposting one per save would just clutter the matter's Notes list.

    filename is staff-chosen (prompted client-side, equalizer.js) rather
    than always auto-generated — added 2026-08-14 so multiple scenario
    worksheets for the same matter/day don't collide on the same
    date-stamped name; falls back to the old auto-generated name if left
    blank."""
    from web.app import render

    conn = get_connection()
    try:
        worksheet = _worksheet_or_404(conn, worksheet_id)
        items = store.list_items(conn, worksheet_id)
    finally:
        conn.close()

    if not items:
        totals = calc.compute_totals(items, worksheet)
        assign_label_a, assign_label_b = calc.assign_button_labels(worksheet)
        return render(request, "equalizer_worksheet.html", worksheet=worksheet, items=[], totals=totals,
                      assign_label_a=assign_label_a, assign_label_b=assign_label_b,
                      error="Add at least one row before saving to Clio.")

    pdf_bytes = await run_in_threadpool(pdf.render_worksheet_pdf, worksheet, items)
    default_filename = f"equalizer-{datetime.today().strftime('%Y-%m-%d')}"
    safe_filename = _sanitize_filename(filename, default_filename)
    previous_document_id = worksheet["clio_document_id"]
    is_first_save = previous_document_id is None

    conn = get_connection()
    try:
        try:
            session = clio_documents.build_session()
            document_id = await run_in_threadpool(
                clio_documents.upload_pdf, session, worksheet["matter_id"], safe_filename, pdf_bytes,
                previous_document_id,
            )
            store.mark_saved_to_clio(conn, worksheet_id, document_id, safe_filename)
        except RuntimeError as e:
            worksheet = store.get_worksheet(conn, worksheet_id)
            items = store.list_items(conn, worksheet_id)
            totals = calc.compute_totals(items, worksheet)
            enriched_items = [calc.enrich_item(i, worksheet) for i in items]
            assign_label_a, assign_label_b = calc.assign_button_labels(worksheet)
            return render(request, "equalizer_worksheet.html", worksheet=worksheet, items=enriched_items, totals=totals,
                          assign_label_a=assign_label_a, assign_label_b=assign_label_b,
                          error=f"Save to Clio failed: {e}")

        worksheet = store.get_worksheet(conn, worksheet_id)
        items = store.list_items(conn, worksheet_id)
    finally:
        conn.close()

    # document_id can differ from previous_document_id even on a "re-save"
    # if the old document had been trashed/deleted directly in Clio since
    # the last save — upload_pdf() falls back to creating a fresh document
    # in that case rather than trying to version one that's gone (see
    # clio_documents.py). Get the wording right for each of the three
    # distinct cases rather than assuming "not the first save" always means
    # "new version of the same file."
    if is_first_save:
        version_note = "."
    elif document_id == previous_document_id:
        version_note = " (new version)."
    else:
        version_note = " (the previous Clio document had been deleted, so this was saved as a new document)."
    notice = f"Saved to Clio as {safe_filename} in the matter's Evidence folder" + version_note
    if is_first_save:
        # Secondary, best-effort step — the PDF landing in Evidence is the
        # artifact that actually matters and has already succeeded by this
        # point, so a Note failure downgrades to a softer notice rather
        # than failing the whole save.
        try:
            await run_in_threadpool(clio_notes.create_worksheet_note, session, worksheet["matter_id"], worksheet_id)
            notice += " A note linking back to this worksheet was also posted on the matter."
        except RuntimeError as e:
            notice += f" (Could not post a matter note linking back to it: {e})"

    totals = calc.compute_totals(items, worksheet)
    enriched_items = [calc.enrich_item(i, worksheet) for i in items]
    assign_label_a, assign_label_b = calc.assign_button_labels(worksheet)
    return render(request, "equalizer_worksheet.html", worksheet=worksheet, items=enriched_items, totals=totals,
                  assign_label_a=assign_label_a, assign_label_b=assign_label_b,
                  notice=notice)
