"""
routes_moore_marsden.py — Moore/Marsden calculator (see CLAUDE.md's
"Moore/Marsden Calculator" section).

Same live-spreadsheet shape as Equalizer: segment CRUD and the "Add
Refinance" button are small JSON endpoints the page's own JS calls directly,
autosaving as staff edit, rather than a submit-and-reload form. One
structural difference from Equalizer, though — Moore/Marsden's calculation
is *recursive* (each segment's cumulative community interest carries into
every later segment's math), so editing or deleting any one segment can
change every downstream segment's figures, not just its own row. Every
segment-mutating endpoint therefore returns the *entire* re-enriched segment
list, not just the one row that was touched, so the client-side grid always
re-renders in full rather than trying to patch one row in place.

/preview.pdf regenerates the PDF live from current draft data and never
touches Clio; /save is the only route in this file that writes anything
outside data/clio_dashboard.db — same repeatable, non-locking Save to Clio
behavior as Equalizer (a worksheet stays editable after saving, and Save to
Clio can be clicked again any time).
"""

import logging
import re
from datetime import datetime

from fastapi import APIRouter, Body, Depends, Form, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse, Response

import matter_matching
import moore_marsden.calc as calc
import moore_marsden.clio_documents as clio_documents
import moore_marsden.clio_notes as clio_notes
import moore_marsden.clio_parties as clio_parties
import moore_marsden.pdf as pdf
import moore_marsden.store as store
from web.auth import require_auth
from web.db import get_connection

router = APIRouter(prefix="/moore-marsden", tags=["moore_marsden"])


def _worksheet_or_404(conn, worksheet_id: int) -> dict:
    worksheet = store.get_worksheet(conn, worksheet_id)
    if worksheet is None:
        raise HTTPException(status_code=404, detail=f"No such worksheet: {worksheet_id}")
    return worksheet


# Same allowlist as equalizer/routes_equalizer.py's _sanitize_filename — the
# filename ends up as a URL path segment in Clio's own presigned S3 upload
# URL, so keeping it to a conservative safe set avoids relying on Clio's
# backend to correctly encode whatever a staff member happens to type.
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
    than trusting any client-supplied name. create_worksheet() also seeds
    the required purchase + valuation rows (see moore_marsden/store.py)."""
    session = clio_documents.build_session()
    try:
        matter = await run_in_threadpool(clio_parties.fetch_matter_summary, session, matter_id)
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return store.create_worksheet(conn, matter_id, matter.get("display_number", str(matter_id)))


def _totals_dict(worksheet: dict, segments: list[dict]):
    enriched = calc.compute_segments(worksheet, segments)
    totals = calc.compute_final(enriched)
    return enriched, totals


@router.get("", response_class=HTMLResponse)
async def moore_marsden_home(request: Request, _: None = Depends(require_auth)):
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
        # Open + Pending, same reasoning as Equalizer's own matter search —
        # a real matter can plausibly need this calculation started before
        # its status flips to Open.
        matters_raw = await run_in_threadpool(
            matter_matching.fetch_open_matters, session, fields="id,display_number", status="open,pending",
        )
        all_matters = sorted(
            ({"id": int(m["id"]), "name": m["display_number"]} for m in matters_raw if m.get("display_number")),
            key=lambda m: m["name"],
        )
    except RuntimeError as e:
        error = str(e)

    return render(request, "moore_marsden.html", worksheets=worksheets, all_matters=all_matters, error=error)


@router.get("/lookup", response_class=HTMLResponse)
async def moore_marsden_lookup(request: Request, matter_id: int, matter_name: str = "", _: None = Depends(require_auth)):
    """A matter with no worksheets yet goes straight to a fresh one; a
    matter with existing worksheets (draft or saved) shows the list to
    recall from instead of silently creating a duplicate — same pattern as
    Equalizer's /equalizer/lookup."""
    from web.app import render

    conn = get_connection()
    try:
        existing = store.list_worksheets_by_matter(conn, matter_id)
        if not existing:
            worksheet_id = await _create_worksheet_for_matter(conn, matter_id)
            return RedirectResponse(url=f"/moore-marsden/{worksheet_id}", status_code=303)
        display_name = matter_name or existing[0]["matter_display_number"]
    finally:
        conn.close()

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

    return render(request, "moore_marsden_matter.html", matter_id=matter_id, matter_display_number=display_name,
                  worksheets=existing, trashed_by_id=trashed_by_id)


@router.post("/new")
async def moore_marsden_new(request: Request, matter_id: int = Form(...), _: None = Depends(require_auth)):
    conn = get_connection()
    try:
        worksheet_id = await _create_worksheet_for_matter(conn, matter_id)
    finally:
        conn.close()

    return RedirectResponse(url=f"/moore-marsden/{worksheet_id}", status_code=303)


@router.post("/{worksheet_id}/delete")
async def moore_marsden_delete_worksheet(
    worksheet_id: int, return_to: str = Form("/moore-marsden"), _: None = Depends(require_auth),
):
    """Blocked once a worksheet has ever been saved to Clio (clio_document_id
    set) — same reasoning as Equalizer: it already has a real Document +
    matter Note pointing at it there. return_to is restricted to this
    router's own paths, same open-redirect guard as Equalizer/the /login
    ?next= pattern."""
    conn = get_connection()
    try:
        worksheet = _worksheet_or_404(conn, worksheet_id)
        if worksheet["clio_document_id"] is not None:
            raise HTTPException(status_code=400, detail="Only worksheets never saved to Clio can be deleted.")
        store.delete_worksheet(conn, worksheet_id)
    finally:
        conn.close()

    target = return_to if return_to.startswith("/moore-marsden") else "/moore-marsden"
    return RedirectResponse(url=target, status_code=303)


@router.post("/{worksheet_id}/duplicate")
async def moore_marsden_duplicate(worksheet_id: int, _: None = Depends(require_auth)):
    """"Save As" — clone this worksheet's settings and every segment into a
    brand new draft for a variant scenario, same reasoning as Equalizer."""
    conn = get_connection()
    try:
        _worksheet_or_404(conn, worksheet_id)
        new_worksheet_id = store.duplicate_worksheet(conn, worksheet_id)
    finally:
        conn.close()

    return RedirectResponse(url=f"/moore-marsden/{new_worksheet_id}", status_code=303)


@router.get("/{worksheet_id}", response_class=HTMLResponse)
async def moore_marsden_editor(worksheet_id: int, request: Request, _: None = Depends(require_auth)):
    """Checks live whether the linked Clio document has been trashed
    directly in Clio — same soft-delete detection as Equalizer's editor
    page. Best-effort: a check failure just skips the warning."""
    from web.app import render

    conn = get_connection()
    try:
        worksheet = _worksheet_or_404(conn, worksheet_id)
        segments = store.list_segments(conn, worksheet_id)
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

    enriched, totals = _totals_dict(worksheet, segments)
    return render(request, "moore_marsden_worksheet.html", worksheet=worksheet, segments=enriched, totals=totals,
                  clio_document_trashed=clio_document_trashed)


@router.patch("/{worksheet_id}/settings")
async def moore_marsden_update_settings(worksheet_id: int, payload: dict = Body(...), _: None = Depends(require_auth)):
    conn = get_connection()
    try:
        _worksheet_or_404(conn, worksheet_id)
        try:
            store.update_worksheet_settings(conn, worksheet_id, **payload)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        worksheet = store.get_worksheet(conn, worksheet_id)
        segments = store.list_segments(conn, worksheet_id)
    finally:
        conn.close()

    enriched, totals = _totals_dict(worksheet, segments)
    return {"worksheet": worksheet, "segments": enriched, "totals": totals.__dict__}


@router.post("/{worksheet_id}/settings/autofill-names")
async def moore_marsden_autofill_names(worksheet_id: int, _: None = Depends(require_auth)):
    """Best-effort prefill for owner/non-owner spouse labels, pulled from
    the matter's client + Opposing Party contact. Never fails the request."""
    conn = get_connection()
    try:
        worksheet = _worksheet_or_404(conn, worksheet_id)
    finally:
        conn.close()

    session = clio_documents.build_session()
    owner_name, non_owner_name = await run_in_threadpool(
        clio_parties.fetch_default_names, session, worksheet["matter_id"]
    )
    return {"owner_spouse_label": owner_name, "non_owner_spouse_label": non_owner_name}


@router.post("/{worksheet_id}/segments")
async def moore_marsden_add_segment(worksheet_id: int, _: None = Depends(require_auth)):
    conn = get_connection()
    try:
        worksheet = _worksheet_or_404(conn, worksheet_id)
        try:
            store.add_segment(conn, worksheet_id)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        segments = store.list_segments(conn, worksheet_id)
    finally:
        conn.close()

    enriched, totals = _totals_dict(worksheet, segments)
    return {"segments": enriched, "totals": totals.__dict__}


@router.patch("/{worksheet_id}/segments/{segment_id}")
async def moore_marsden_update_segment(worksheet_id: int, segment_id: int, payload: dict = Body(...), _: None = Depends(require_auth)):
    conn = get_connection()
    try:
        worksheet = _worksheet_or_404(conn, worksheet_id)
        try:
            store.update_segment(conn, segment_id, **payload)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        segments = store.list_segments(conn, worksheet_id)
    finally:
        conn.close()

    if not any(s["id"] == segment_id for s in segments):
        raise HTTPException(status_code=404, detail=f"No such segment: {segment_id}")
    enriched, totals = _totals_dict(worksheet, segments)
    return {"segments": enriched, "totals": totals.__dict__}


@router.delete("/{worksheet_id}/segments/{segment_id}")
async def moore_marsden_delete_segment(worksheet_id: int, segment_id: int, _: None = Depends(require_auth)):
    conn = get_connection()
    try:
        worksheet = _worksheet_or_404(conn, worksheet_id)
        try:
            store.delete_segment(conn, segment_id)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        segments = store.list_segments(conn, worksheet_id)
    finally:
        conn.close()

    enriched, totals = _totals_dict(worksheet, segments)
    return {"segments": enriched, "totals": totals.__dict__}


@router.get("/{worksheet_id}/preview.pdf")
async def moore_marsden_preview_pdf(worksheet_id: int, _: None = Depends(require_auth)):
    conn = get_connection()
    try:
        worksheet = _worksheet_or_404(conn, worksheet_id)
        segments = store.list_segments(conn, worksheet_id)
    finally:
        conn.close()

    pdf_bytes = await run_in_threadpool(pdf.render_worksheet_pdf, worksheet, segments)
    return Response(
        content=pdf_bytes, media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="moore-marsden-{worksheet["matter_display_number"]}.pdf"'},
    )


@router.post("/{worksheet_id}/save", response_class=HTMLResponse)
async def moore_marsden_save_to_clio(
    worksheet_id: int, request: Request, filename: str = Form(""), _: None = Depends(require_auth),
):
    """Repeatable, not one-way — same as Equalizer: a worksheet stays
    editable after this and Save to Clio can be clicked again any time.
    Re-saves push a new Document *version* under the same clio_document_id
    rather than a duplicate file, and only the very first save posts the
    matter Note."""
    from web.app import render

    conn = get_connection()
    try:
        worksheet = _worksheet_or_404(conn, worksheet_id)
        segments = store.list_segments(conn, worksheet_id)
    finally:
        conn.close()

    def _render_editor(worksheet, segments, **extra):
        enriched, totals = _totals_dict(worksheet, segments)
        return render(request, "moore_marsden_worksheet.html", worksheet=worksheet, segments=enriched,
                      totals=totals, **extra)

    if len(segments) < 2:
        return _render_editor(worksheet, segments, error="A worksheet needs at least a purchase and a valuation row before saving to Clio.")

    pdf_bytes = await run_in_threadpool(pdf.render_worksheet_pdf, worksheet, segments)
    default_filename = f"moore-marsden-{datetime.today().strftime('%Y-%m-%d')}"
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
            segments = store.list_segments(conn, worksheet_id)
            return _render_editor(worksheet, segments, error=f"Save to Clio failed: {e}")

        worksheet = store.get_worksheet(conn, worksheet_id)
        segments = store.list_segments(conn, worksheet_id)
    finally:
        conn.close()

    if is_first_save:
        version_note = "."
    elif document_id == previous_document_id:
        version_note = " (new version)."
    else:
        version_note = " (the previous Clio document had been deleted, so this was saved as a new document)."
    notice = f"Saved to Clio as {safe_filename} in the matter's Evidence folder" + version_note
    if is_first_save:
        try:
            await run_in_threadpool(clio_notes.create_worksheet_note, session, worksheet["matter_id"], worksheet_id)
            notice += " A note linking back to this worksheet was also posted on the matter."
        except RuntimeError as e:
            notice += f" (Could not post a matter note linking back to it: {e})"

    return _render_editor(worksheet, segments, notice=notice)
