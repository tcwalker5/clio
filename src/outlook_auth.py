"""
outlook_auth.py — Microsoft Graph OAuth token manager for the Outlook
calendar migration (subproject 5). Mirrors clio_auth.py's structure exactly,
targeting Microsoft's v2.0 endpoints instead of Clio's.

Reuses calendar-check's existing Azure app registration and its already
-registered redirect URI (http://localhost:3020/api/auth/callback) — no new
Azure Portal setup needed, just a one-time browser consent click.

Usage:
  uv run src/outlook_auth.py             # full browser flow (first time)
  uv run src/outlook_auth.py --refresh   # use refresh token (token expired)

Updates MICROSOFT_ACCESS_TOKEN and MICROSOFT_REFRESH_TOKEN in .env.

Scope: Calendars.Read only — this is a one-time read-only backfill, nothing
in this project writes back to Outlook.
"""

import argparse
import os
import re
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import requests
from dotenv import load_dotenv

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(ENV_PATH)

SCOPES = "openid profile offline_access User.Read Calendars.Read"

CLIENT_ID     = os.getenv("MICROSOFT_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("MICROSOFT_CLIENT_SECRET", "")
TENANT_ID     = os.getenv("MICROSOFT_TENANT_ID", "common")
REDIRECT_URI  = os.getenv("MICROSOFT_REDIRECT_URI", "http://localhost:3020/api/auth/callback")
REFRESH_TOKEN = os.getenv("MICROSOFT_REFRESH_TOKEN", "")

AUTHORITY         = f"https://login.microsoftonline.com/{TENANT_ID}"
AUTHORIZE_ENDPOINT = f"{AUTHORITY}/oauth2/v2.0/authorize"
TOKEN_ENDPOINT     = f"{AUTHORITY}/oauth2/v2.0/token"

if not CLIENT_ID or not CLIENT_SECRET:
    print("ERROR: MICROSOFT_CLIENT_ID and MICROSOFT_CLIENT_SECRET must be set in .env")
    sys.exit(1)


def update_env(access_token: str, refresh_token: str) -> None:
    text = ENV_PATH.read_text(encoding="utf-8")

    def replace_or_append(t: str, key: str, value: str) -> str:
        pattern = rf"^{re.escape(key)}=.*$"
        line = f"{key}={value}"
        if re.search(pattern, t, re.MULTILINE):
            return re.sub(pattern, line, t, flags=re.MULTILINE)
        return t + f"\n{line}"

    text = replace_or_append(text, "MICROSOFT_ACCESS_TOKEN", access_token)
    if refresh_token:
        text = replace_or_append(text, "MICROSOFT_REFRESH_TOKEN", refresh_token)
        print(f"  MICROSOFT_REFRESH_TOKEN : {refresh_token[:12]}...")
    else:
        print("  MICROSOFT_REFRESH_TOKEN : unchanged (Microsoft did not issue a new one)")
    ENV_PATH.write_text(text, encoding="utf-8")
    print(f"  MICROSOFT_ACCESS_TOKEN  : {access_token[:12]}...")


def exchange_tokens(payload: dict) -> tuple[str, str]:
    r = requests.post(TOKEN_ENDPOINT, data=payload, timeout=30)
    if r.status_code != 200:
        print(f"ERROR: Token request failed — {r.status_code}  {r.text[:300]}")
        sys.exit(1)
    tokens = r.json()
    access  = tokens.get("access_token", "")
    refresh = tokens.get("refresh_token", "")
    if not access:
        print(f"ERROR: No access_token in response: {tokens}")
        sys.exit(1)
    return access, refresh


def do_refresh() -> None:
    if not REFRESH_TOKEN:
        print("ERROR: MICROSOFT_REFRESH_TOKEN not set in .env — run without --refresh for full browser flow")
        sys.exit(1)
    print("Refreshing token...")
    access, refresh = exchange_tokens({
        "grant_type":    "refresh_token",
        "refresh_token": REFRESH_TOKEN,
        "client_id":     CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "scope":         SCOPES,
    })
    print("Done. .env updated.")
    update_env(access, refresh)


def do_browser_flow() -> None:
    parsed = urlparse(REDIRECT_URI)
    host = parsed.hostname or "localhost"
    port = parsed.port or 80

    auth_url = (
        f"{AUTHORIZE_ENDPOINT}?"
        + urlencode({
            "response_type": "code",
            "client_id":     CLIENT_ID,
            "redirect_uri":  REDIRECT_URI,
            "scope":         SCOPES,
            "response_mode": "query",
            "prompt":        "select_account",
        })
    )

    auth_code: list[str] = []

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            params = parse_qs(urlparse(self.path).query)
            if "code" in params:
                auth_code.append(params["code"][0])
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"<h2>Authorization successful! You can close this window.</h2>")
            else:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"<h2>No code received. Try again.</h2>")

        def log_message(self, *args):
            pass

    server = HTTPServer((host, port), CallbackHandler)
    t = threading.Thread(target=server.handle_request)
    t.start()

    print("Opening browser for Microsoft authorization...")
    print(f"Scopes: {SCOPES}\n")
    print(f"NOTE: sign in as {os.getenv('MICROSOFT_CALENDAR_OWNER_EMAIL', 'the calendar owner')} when prompted.\n")
    webbrowser.open(auth_url)
    t.join(timeout=120)

    if not auth_code:
        print("ERROR: No authorization code received within 2 minutes.")
        sys.exit(1)

    print("Authorization code received. Exchanging for tokens...")
    access, refresh = exchange_tokens({
        "grant_type":    "authorization_code",
        "code":          auth_code[0],
        "client_id":     CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "redirect_uri":  REDIRECT_URI,
        "scope":         SCOPES,
    })
    print("Done. .env updated.")
    update_env(access, refresh)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--refresh", action="store_true", help="Use refresh token instead of browser flow")
    args = ap.parse_args()

    if args.refresh:
        do_refresh()
    else:
        do_browser_flow()


if __name__ == "__main__":
    main()
