"""
app.py — Clio dashboard FastAPI application.

Run (HTTPS — required for Sign in with Clio; certs from `uv run src/cap_certs.py`):
  uv run uvicorn web.app:app --app-dir src --host 0.0.0.0 --port 8421 ^
      --ssl-keyfile data/certs/cap-server.key --ssl-certfile data/certs/cap-server.crt

(--app-dir src puts src/ on sys.path, matching how the standalone scripts in
this repo resolve flat imports like `import matter_matching`.)
"""

import json
import os
import secrets
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

ENV_PATH = Path(__file__).resolve().parent.parent.parent / ".env"
load_dotenv(ENV_PATH)

from court_calendar.normalizer import judge_last_name  # noqa: E402
from web import db  # noqa: E402
from web.auth import (  # noqa: E402
    AdminRequired,
    AuthRequired,
    authorize_url,
    complete_sign_in,
    current_user,
    is_admin,
    record_audit,
    require_admin,
    require_login,
)

WEB_DIR = Path(__file__).resolve().parent

PACIFIC = ZoneInfo("America/Los_Angeles")


def local_time(value: str | None, fmt: str = "%Y-%m-%d %I:%M %p") -> str:
    """Render a SQLite `CURRENT_TIMESTAMP` string (always UTC, no tz marker) as Pacific time.

    Every `*_sync_runs`/`*_at` column that relies on the column-level
    `DEFAULT CURRENT_TIMESTAMP` (see web/db.py) is stored in UTC — SQLite has
    no concept of a local default. Displaying that raw string reads like a
    local time and is off by 7-8 hours (confirmed live 2026-08-25: a sync run
    at ~11:40am Pacific stored/displayed as 18:39:58).
    """
    if not value:
        return ""
    utc_dt = datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=ZoneInfo("UTC"))
    return utc_dt.astimezone(PACIFIC).strftime(fmt)


app = FastAPI(title="Collier Automation Platform")



async def audit_changes(request: Request, call_next):
    """Logs who made every non-GET request (the dashboard's state-changing
    calls) to audit_log — covers admin apps too, whose Clio writes still go
    through the shared token. GETs aren't logged: they're page views."""
    response = await call_next(request)
    if request.method != "GET" and not request.url.path.startswith("/static"):
        try:
            record_audit(current_user(request), request.method, request.url.path, response.status_code)
        except Exception as e:  # never fail the user's request over a log write
            print(f"WARNING: audit_log write failed: {e}")
    return response


# Order matters: middleware added later wraps earlier ones, so SessionMiddleware
# (added second) runs first and request.session is populated for audit_changes.
app.add_middleware(BaseHTTPMiddleware, dispatch=audit_changes)
SECRET_KEY = os.getenv("CLIO_DASHBOARD_SECRET") or secrets.token_urlsafe(32)
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, same_site="lax", https_only=True)
app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")

templates = Jinja2Templates(directory=WEB_DIR / "templates")
templates.env.filters["judge_last_name"] = judge_last_name
templates.env.filters["tojson"] = json.dumps
templates.env.filters["money"] = lambda value, decimals=2: f"{value:,.{decimals}f}"
templates.env.filters["initials"] = lambda name: "".join(w[0].upper() for w in (name or "").split() if w[0].isalpha())
templates.env.filters["local_time"] = local_time

# Cache-busting query param for static assets (?v=<mtime>) — browsers were
# found to keep serving a stale cached style.css after an edit even on a
# plain reload, with no obvious visual sign anything was wrong (a CSS rule
# just silently didn't apply). Computed once at process startup, which is
# already the point any static asset edit takes effect (see CLAUDE.md's
# "Dashboard Restart Gotcha").
STATIC_VERSION = int((WEB_DIR / "static" / "style.css").stat().st_mtime)


@app.exception_handler(AuthRequired)
async def auth_required_handler(request: Request, exc: AuthRequired):
    # Drop a stale identity (e.g. their stored Clio token was revoked) so
    # /login doesn't bounce them straight back here.
    request.session.pop("clio_user_id", None)
    request.session.pop("clio_user_name", None)
    return RedirectResponse(url=f"/login?next={request.url.path}", status_code=303)


@app.exception_handler(AdminRequired)
async def admin_required_handler(request: Request, exc: AdminRequired):
    return render(request, "no_access.html", status_code=403)


def render(request: Request, template: str, status_code: int = 200, **context):
    user = current_user(request)
    context.setdefault("authenticated", user is not None)
    context.setdefault("current_user", user)
    context.setdefault("is_admin", is_admin(user))
    context.setdefault("static_version", STATIC_VERSION)
    return templates.TemplateResponse(request, template, context, status_code=status_code)


@app.on_event("startup")
def startup() -> None:
    db.init_db()


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/", error: str | None = None):
    if current_user(request):
        return RedirectResponse(url=next, status_code=303)
    return render(request, "login.html", authenticated=False, next=next, error=error)


@app.get("/login/clio")
async def login_start(request: Request, next: str = "/"):
    return RedirectResponse(url=authorize_url(request, next), status_code=303)


@app.get("/oauth/callback")
async def oauth_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    if error or not code:
        return render(request, "login.html", authenticated=False, next="/",
                      error="Clio sign-in was cancelled or denied." if error else "No sign-in code came back from Clio.")
    try:
        next_path = await run_in_threadpool(complete_sign_in, request, code, state)
    except RuntimeError as e:
        return render(request, "login.html", authenticated=False, next="/", error=str(e))
    return RedirectResponse(url=next_path, status_code=303)


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)


@app.get("/cap-root-ca.crt")
async def root_ca_certificate():
    """The CAP Root CA's public certificate, for install-cap-cert.bat — open
    (no sign-in), since a PC needs it before it can trust this site at all."""
    return FileResponse(Path(__file__).resolve().parent.parent.parent / "data" / "certs" / "cap-root-ca.crt",
                        media_type="application/x-x509-ca-cert", filename="cap-root-ca.crt")


@app.get("/", response_class=HTMLResponse)
async def dashboard_home(request: Request, _: dict = Depends(require_login)):
    return render(request, "dashboard.html")


@app.get("/audit-log", response_class=HTMLResponse)
async def audit_log_page(request: Request, _: dict = Depends(require_admin)):
    conn = db.get_connection()
    try:
        rows = [dict(r) for r in conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 500")]
    finally:
        conn.close()
    return render(request, "audit_log.html", rows=rows)


from web.routes_bradford import router as bradford_router  # noqa: E402
from web.routes_calendar import router as calendar_router  # noqa: E402
from web.routes_client_assignment import router as client_assignment_router  # noqa: E402
from web.routes_collections import router as collections_router  # noqa: E402
from web.routes_date_calculator import router as date_calculator_router  # noqa: E402
from web.routes_equalizer import router as equalizer_router  # noqa: E402
from web.routes_legs import router as legs_router  # noqa: E402
from web.routes_moore_marsden import router as moore_marsden_router  # noqa: E402
from web.routes_papercut import router as papercut_router  # noqa: E402
from web.routes_printer import router as printer_router  # noqa: E402
from web.routes_ringcentral import router as ringcentral_router  # noqa: E402
from web.routes_soa_now_audit import router as soa_now_audit_router  # noqa: E402
from web.routes_staff_unbilled import router as staff_unbilled_router  # noqa: E402
from web.routes_trust import router as trust_router  # noqa: E402

app.include_router(bradford_router)
app.include_router(calendar_router)
app.include_router(client_assignment_router)
app.include_router(collections_router)
app.include_router(date_calculator_router)
app.include_router(equalizer_router)
app.include_router(legs_router)
app.include_router(moore_marsden_router)
app.include_router(papercut_router)
app.include_router(printer_router)
app.include_router(ringcentral_router)
app.include_router(soa_now_audit_router)
app.include_router(staff_unbilled_router)
app.include_router(trust_router)
