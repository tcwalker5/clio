"""
routes_trust.py — Trust cushion monitor + trust replenishment request review.

The monitor table (trust_monitor.run_pipeline) is read-only, like Court
Calendar Sync — runs live on every page view rather than following
RingCentral's persisted-last-run pattern, since the underlying Clio calls
are cheap under real data volumes (a few hundred matters/bills, 1-2 pages
each).

The request-review section below it is NOT read-only: /trust/send-request
and /trust/send-selected create real TrustRequest drafts in Clio
(approved=False). Pause and target-override only touch the local
trust_matter_settings/trust_requests tables in data/clio_dashboard.db.

Every mutating route re-runs the pipeline and re-renders trust.html
directly in place (same pattern as Bradford's resolve-exception) rather
than redirecting — a stale query-param success message would be more
misleading than useful for something backed by a live recompute anyway.
"""

from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse

import trust_monitor
from web.auth import require_auth
from web.db import get_connection

router = APIRouter(prefix="/trust", tags=["trust"])


def _render(request: Request, **overrides):
    from web.app import render

    context = {"error": None, "notice": None, "statuses": None, "candidates": None}
    context.update(overrides)
    return render(request, "trust.html", **context)


async def _load_home(request: Request, **overrides):
    """Runs the full live pipeline + candidate evaluation and renders the
    page. Used for the plain GET and after every mutating action, so the
    table shown always reflects what's actually true right now."""
    try:
        statuses = await run_in_threadpool(trust_monitor.run_pipeline)
    except RuntimeError as e:
        return _render(request, error=str(e))

    conn = get_connection()
    try:
        candidates = trust_monitor.evaluate_request_candidates(conn, statuses)
    finally:
        conn.close()

    statuses_sorted = sorted(statuses, key=lambda s: s.cushion)
    flagged_count = sum(1 for s in statuses_sorted if s.flagged)
    total_owed = sum(s.shortfall for s in statuses_sorted if s.flagged)

    # Actionable candidates first (lowest trust balance first within that
    # group), then already-requested/paused for visibility below.
    candidates_sorted = sorted(candidates, key=lambda c: (c.state != "candidate", c.trust_balance))
    to_send_count = sum(1 for c in candidates_sorted if c.state == "candidate")

    context = dict(
        statuses=statuses_sorted,
        flagged_count=flagged_count,
        total_owed=total_owed,
        trust_minimum=trust_monitor.TRUST_MINIMUM,
        action_gate=trust_monitor.ACTION_GATE,
        card_fee_rate=trust_monitor.CARD_FEE_RATE,
        candidates=candidates_sorted,
        to_send_count=to_send_count,
    )
    context.update(overrides)
    return _render(request, **context)


@router.get("", response_class=HTMLResponse)
async def trust_home(request: Request, _: None = Depends(require_auth)):
    return await _load_home(request)


@router.get("/download")
async def trust_download(_: None = Depends(require_auth)):
    path = Path("output") / f"trust_monitor_{datetime.today().strftime('%Y-%m-%d')}.csv"
    if not path.exists():
        return HTMLResponse("No report generated yet — visit /trust first.", status_code=404)
    return FileResponse(path, filename=path.name, media_type="text/csv")


@router.post("/set-target", response_class=HTMLResponse)
async def trust_set_target(
    request: Request,
    matter_id: int = Form(...),
    target_amount: str = Form(""),
    note: str = Form(""),
    _: None = Depends(require_auth),
):
    # Displayed pre-filled with comma separators (e.g. "2,500.00") — strip
    # them back out so a re-save of an untouched field still parses; a typed
    # comma is tolerated the same way, a typed comma is not required.
    raw = target_amount.strip().replace(",", "")
    try:
        amount = float(raw) if raw else None
    except ValueError:
        return await _load_home(request, error=f"'{target_amount}' isn't a valid dollar amount — target not saved.")

    conn = get_connection()
    try:
        trust_monitor.set_matter_target(conn, matter_id, amount, note)
    finally:
        conn.close()
    return await _load_home(request)


@router.post("/pause", response_class=HTMLResponse)
async def trust_pause(request: Request, matter_id: int = Form(...), _: None = Depends(require_auth)):
    conn = get_connection()
    try:
        trust_monitor.set_matter_paused(conn, matter_id, True)
    finally:
        conn.close()
    return await _load_home(request)


@router.post("/unpause", response_class=HTMLResponse)
async def trust_unpause(request: Request, matter_id: int = Form(...), _: None = Depends(require_auth)):
    conn = get_connection()
    try:
        trust_monitor.set_matter_paused(conn, matter_id, False)
    finally:
        conn.close()
    return await _load_home(request)


def _send_one(conn, session, candidate) -> tuple[bool, str]:
    try:
        clio_id = trust_monitor.create_trust_request(
            session, candidate.client_id, candidate.matter_id, candidate.requested_amount,
        )
        trust_monitor.record_trust_request(
            conn, candidate.matter_id, candidate.target_amount, candidate.trust_balance,
            candidate.requested_amount, clio_id,
        )
        return True, ""
    except RuntimeError as e:
        return False, str(e)


@router.post("/send-request", response_class=HTMLResponse)
async def trust_send_request(request: Request, matter_id: int = Form(...), _: None = Depends(require_auth)):
    try:
        statuses = await run_in_threadpool(trust_monitor.run_pipeline)
    except RuntimeError as e:
        return _render(request, error=str(e))

    conn = get_connection()
    try:
        candidates = trust_monitor.evaluate_request_candidates(conn, statuses)
        match = next((c for c in candidates if c.matter_id == matter_id and c.state == "candidate"), None)

        error = None
        notice = None
        if match is None:
            error = f"Matter {matter_id} is no longer a request candidate — its cushion may have changed, or it was already sent. Refreshed below."
        else:
            session = trust_monitor.build_session()
            ok, err = _send_one(conn, session, match)
            if ok:
                notice = f"Trust request for ${match.requested_amount:,.2f} sent to Clio as a draft for {match.display_number}."
            else:
                error = f"Failed to send trust request for {match.display_number}: {err}"
    finally:
        conn.close()

    return await _load_home(request, error=error, notice=notice)


@router.post("/send-selected", response_class=HTMLResponse)
async def trust_send_selected(request: Request, _: None = Depends(require_auth)):
    form = await request.form()
    matter_ids = {int(v) for v in form.getlist("matter_ids")}

    if not matter_ids:
        return _load_home(request, error="No matters selected.")

    try:
        statuses = await run_in_threadpool(trust_monitor.run_pipeline)
    except RuntimeError as e:
        return _render(request, error=str(e))

    conn = get_connection()
    sent, failed = 0, []
    try:
        candidates = trust_monitor.evaluate_request_candidates(conn, statuses)
        by_id = {c.matter_id: c for c in candidates if c.state == "candidate"}
        session = trust_monitor.build_session()
        for mid in matter_ids:
            candidate = by_id.get(mid)
            if candidate is None:
                failed.append(f"matter {mid} (no longer a candidate)")
                continue
            ok, err = _send_one(conn, session, candidate)
            if ok:
                sent += 1
            else:
                failed.append(f"{candidate.display_number}: {err}")
    finally:
        conn.close()

    notice = f"Sent {sent} trust request(s)." if sent else None
    error = ("Failed: " + "; ".join(failed)) if failed else None
    return await _load_home(request, notice=notice, error=error)
