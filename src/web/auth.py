"""
auth.py — "Sign in with Clio" for the dashboard, plus two access levels.

Replaced the shared passphrase 2026-10-22. Each person signs in with their own
Clio account (OAuth, same app registration as clio_auth.py, redirect URI
https://cap.lan:8421/oauth/callback). Their access/refresh token is stored in
clio_user_tokens, so the open apps can make Clio calls *as that person* —
which is what makes Documents, Notes and field edits show the real staff
member in Clio instead of whoever authorized the shared .env token.

Access levels:
  require_login — any signed-in staff member. Used by the open apps:
                  Equalizer, Moore/Marsden, Date Calculator, Client
                  Assignment, Court Calendar.
  require_admin — signed in AND in ADMIN_USER_IDS. Used by billing &
                  operations (everything else). `require_auth` is kept as
                  an alias so those routers didn't need touching.

Admin apps still call Clio with the shared .env token (CLIO_ACCESS_TOKEN):
only Ted/Dalinah use them, and several run unattended/CLI paths that have no
signed-in user. The audit_log table records who triggered every change from
the dashboard regardless of which token made the Clio call.
"""

import os
import secrets
import threading
import time
from urllib.parse import urlencode

import requests
from fastapi import Request

CLIENT_ID = os.getenv("CLIO_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("CLIO_CLIENT_SECRET", "")
BASE_URL = os.getenv("CLIO_BASE_URL", "https://app.clio.com").rstrip("/")
OAUTH_REDIRECT_URI = os.getenv("CAP_OAUTH_REDIRECT_URI", "https://cap.lan:8421/oauth/callback")
SCOPES = "openid profile email offline_access matters activities"

# Billing & operations access. Clio user ids (see `uv run src/clio_users.py`),
# not names — a renamed Clio user keeps their access.
ADMIN_USER_IDS = {
    359072911,  # Ted Walker
    359103693,  # Dalinah Espinoza
}

# Refresh a stored access token this long before Clio says it expires, so a
# token never dies mid-way through a multi-call page load.
REFRESH_MARGIN_SECONDS = 300

# Serializes refreshes: two requests from the same person refreshing at once
# would both send the same refresh token, and if Clio rotates it the second
# one fails and needlessly signs them out.
_refresh_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS clio_user_tokens (
    clio_user_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT,
    access_token TEXT NOT NULL,
    refresh_token TEXT,
    expires_at INTEGER NOT NULL,          -- unix seconds
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT DEFAULT CURRENT_TIMESTAMP,
    clio_user_id INTEGER,
    user_name TEXT,
    method TEXT NOT NULL,
    path TEXT NOT NULL,
    status_code INTEGER
);
CREATE INDEX IF NOT EXISTS idx_audit_log_at ON audit_log(at);
"""
SCHEMA_COLUMNS: list[tuple[str, str, str]] = []


class AuthRequired(Exception):
    """Not signed in (or the stored Clio token is gone) — app.py redirects to /login."""


class AdminRequired(Exception):
    """Signed in, but not in ADMIN_USER_IDS — app.py renders a no-access page."""


# --- session identity ---------------------------------------------------------

def current_user(request: Request) -> dict | None:
    """{"id", "name"} of the signed-in person, or None. Only identity lives in
    the (signed) session cookie; tokens stay server-side in clio_user_tokens."""
    user_id = request.session.get("clio_user_id")
    if not user_id:
        return None
    return {"id": user_id, "name": request.session.get("clio_user_name", "")}


def is_authenticated(request: Request) -> bool:
    return current_user(request) is not None


def is_admin(user: dict | None) -> bool:
    return bool(user) and user["id"] in ADMIN_USER_IDS


async def require_login(request: Request) -> dict:
    """FastAPI dependency — any signed-in staff member. Returns the user dict."""
    user = current_user(request)
    if user is None:
        raise AuthRequired()
    return user


async def require_admin(request: Request) -> dict:
    """FastAPI dependency — signed in and in ADMIN_USER_IDS."""
    user = await require_login(request)
    if not is_admin(user):
        raise AdminRequired()
    return user


require_auth = require_admin


# --- OAuth flow ---------------------------------------------------------------

def authorize_url(request: Request, next_path: str) -> str:
    """Builds the Clio authorize URL and stashes a CSRF `state` + where to land
    afterward in the session."""
    state = secrets.token_urlsafe(24)
    request.session["oauth_state"] = state
    request.session["oauth_next"] = next_path
    return f"{BASE_URL}/oauth/authorize?" + urlencode({
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": OAUTH_REDIRECT_URI,
        "scope": SCOPES,
        "state": state,
    })


def _token_request(data: dict) -> dict:
    resp = requests.post(f"{BASE_URL}/oauth/token", data={
        "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET, **data,
    }, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(f"Clio token request failed: {resp.status_code} {resp.text[:300]}")
    return resp.json()


def complete_sign_in(request: Request, code: str, state: str) -> str:
    """Exchanges the callback's code for tokens, identifies the user, stores
    their tokens, and signs them into the session. Returns the path to land on."""
    expected = request.session.pop("oauth_state", None)
    next_path = request.session.pop("oauth_next", "/") or "/"
    if not expected or not secrets.compare_digest(state, expected):
        raise RuntimeError("Sign-in state didn't match — start again from the sign-in page.")

    tokens = _token_request({"grant_type": "authorization_code", "code": code, "redirect_uri": OAUTH_REDIRECT_URI})
    who = requests.get(
        f"{BASE_URL}/api/v4/users/who_am_i.json",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
        params={"fields": "id,name,email"}, timeout=30,
    )
    if who.status_code != 200:
        raise RuntimeError(f"Couldn't identify the Clio user: {who.status_code} {who.text[:300]}")
    me = who.json()["data"]

    _save_tokens(me["id"], me.get("name", ""), me.get("email", ""), tokens)
    request.session.pop("authenticated", None)  # pre-2026-10-22 passphrase cookie
    request.session["clio_user_id"] = me["id"]
    request.session["clio_user_name"] = me.get("name", "")
    return next_path if next_path.startswith("/") else "/"


# --- per-user token storage + Clio session -------------------------------------

def _save_tokens(user_id: int, name: str, email: str, tokens: dict, conn=None) -> None:
    from web import db

    own = conn is None
    conn = conn or db.get_connection()
    try:
        expires_at = int(time.time()) + int(tokens.get("expires_in", 3600))
        conn.execute(
            """
            INSERT INTO clio_user_tokens (clio_user_id, name, email, access_token, refresh_token, expires_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(clio_user_id) DO UPDATE SET
                name = excluded.name, email = excluded.email,
                access_token = excluded.access_token,
                -- Clio doesn't always rotate the refresh token; keep the old one if none came back
                refresh_token = COALESCE(excluded.refresh_token, clio_user_tokens.refresh_token),
                expires_at = excluded.expires_at,
                updated_at = CURRENT_TIMESTAMP
            """,
            (user_id, name, email, tokens["access_token"], tokens.get("refresh_token"), expires_at),
        )
        conn.commit()
    finally:
        if own:
            conn.close()


def _access_token_for(user: dict) -> str:
    from web import db

    with _refresh_lock:
        conn = db.get_connection()
        try:
            return _access_token_locked(conn, user)
        finally:
            conn.close()


def _access_token_locked(conn, user: dict) -> str:
    row = conn.execute("SELECT * FROM clio_user_tokens WHERE clio_user_id = ?", (user["id"],)).fetchone()
    if row is None:
        raise AuthRequired()
    if row["expires_at"] - REFRESH_MARGIN_SECONDS > time.time():
        return row["access_token"]
    if not row["refresh_token"]:
        raise AuthRequired()
    try:
        tokens = _token_request({"grant_type": "refresh_token", "refresh_token": row["refresh_token"]})
    except RuntimeError:
        # Revoked/expired refresh token — make them sign in again rather than 500.
        conn.execute("DELETE FROM clio_user_tokens WHERE clio_user_id = ?", (user["id"],))
        conn.commit()
        raise AuthRequired()
    _save_tokens(row["clio_user_id"], row["name"], row["email"], tokens, conn=conn)
    return tokens["access_token"]


def clio_session(user: dict) -> requests.Session:
    """A requests.Session that calls Clio as the signed-in person — same shape
    as every module's build_session(), just their token instead of .env's."""
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {_access_token_for(user)}", "Content-Type": "application/json"})
    return session


# --- audit log ----------------------------------------------------------------

def record_audit(user: dict | None, method: str, path: str, status_code: int) -> None:
    from web import db

    conn = db.get_connection()
    try:
        conn.execute(
            "INSERT INTO audit_log (clio_user_id, user_name, method, path, status_code) VALUES (?, ?, ?, ?, ?)",
            (user["id"] if user else None, user["name"] if user else None, method, path, status_code),
        )
        conn.commit()
    finally:
        conn.close()
