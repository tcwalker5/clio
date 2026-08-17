"""
app.py — Clio dashboard FastAPI application.

Run:
  uv run uvicorn web.app:app --app-dir src --host 0.0.0.0 --port 8421

(--app-dir src puts src/ on sys.path, matching how the standalone scripts in
this repo resolve flat imports like `import matter_matching`.)
"""

import json
import os
import secrets
from pathlib import Path

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

ENV_PATH = Path(__file__).resolve().parent.parent.parent / ".env"
load_dotenv(ENV_PATH)

from court_calendar.normalizer import judge_last_name  # noqa: E402
from web import db  # noqa: E402
from web.auth import PASSPHRASE, AuthRequired, is_authenticated, require_auth  # noqa: E402

WEB_DIR = Path(__file__).resolve().parent

app = FastAPI(title="Collier Automation Platform")

SECRET_KEY = os.getenv("CLIO_DASHBOARD_SECRET") or secrets.token_urlsafe(32)
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, same_site="lax")
app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")

templates = Jinja2Templates(directory=WEB_DIR / "templates")
templates.env.filters["judge_last_name"] = judge_last_name
templates.env.filters["tojson"] = json.dumps
templates.env.filters["money"] = lambda value, decimals=2: f"{value:,.{decimals}f}"

# Cache-busting query param for static assets (?v=<mtime>) — browsers were
# found to keep serving a stale cached style.css after an edit even on a
# plain reload, with no obvious visual sign anything was wrong (a CSS rule
# just silently didn't apply). Computed once at process startup, which is
# already the point any static asset edit takes effect (see CLAUDE.md's
# "Dashboard Restart Gotcha").
STATIC_VERSION = int((WEB_DIR / "static" / "style.css").stat().st_mtime)


@app.exception_handler(AuthRequired)
async def auth_required_handler(request: Request, exc: AuthRequired):
    return RedirectResponse(url=f"/login?next={request.url.path}", status_code=303)


def render(request: Request, template: str, **context):
    context.setdefault("authenticated", is_authenticated(request))
    context.setdefault("static_version", STATIC_VERSION)
    return templates.TemplateResponse(request, template, context)


@app.on_event("startup")
def startup() -> None:
    db.init_db()
    if not PASSPHRASE:
        print("WARNING: CLIO_DASHBOARD_PASSPHRASE not set in .env — /login will reject all attempts.")


@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, next: str = "/"):
    if is_authenticated(request):
        return RedirectResponse(url=next, status_code=303)
    return render(request, "login.html", authenticated=False, next=next, error=None)


@app.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request, passphrase: str = Form(...), next: str = Form("/")):
    if PASSPHRASE and secrets.compare_digest(passphrase, PASSPHRASE):
        request.session["authenticated"] = True
        return RedirectResponse(url=next or "/", status_code=303)
    return render(request, "login.html", authenticated=False, next=next,
                  error="Incorrect passphrase.")


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)


@app.get("/", response_class=HTMLResponse)
async def dashboard_home(request: Request, _: None = Depends(require_auth)):
    return render(request, "dashboard.html")


from web.routes_bradford import router as bradford_router  # noqa: E402
from web.routes_calendar import router as calendar_router  # noqa: E402
from web.routes_collections import router as collections_router  # noqa: E402
from web.routes_equalizer import router as equalizer_router  # noqa: E402
from web.routes_legs import router as legs_router  # noqa: E402
from web.routes_moore_marsden import router as moore_marsden_router  # noqa: E402
from web.routes_printer import router as printer_router  # noqa: E402
from web.routes_ringcentral import router as ringcentral_router  # noqa: E402
from web.routes_trust import router as trust_router  # noqa: E402

app.include_router(bradford_router)
app.include_router(calendar_router)
app.include_router(collections_router)
app.include_router(equalizer_router)
app.include_router(legs_router)
app.include_router(moore_marsden_router)
app.include_router(printer_router)
app.include_router(ringcentral_router)
app.include_router(trust_router)
