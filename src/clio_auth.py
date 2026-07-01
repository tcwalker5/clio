"""
clio_auth.py — Clio OAuth token manager for the clio project.

Usage:
  uv run src/clio_auth.py             # full browser flow (first time)
  uv run src/clio_auth.py --refresh   # use refresh token (token expired)

Updates CLIO_ACCESS_TOKEN and CLIO_REFRESH_TOKEN in .env.

Required scopes for this project:
  matters    — read open matters for matching and PaperCut sync
  activities — post expense entries and contractor time entries
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

SCOPES = "openid profile email offline_access matters activities"

CLIENT_ID     = os.getenv("CLIO_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("CLIO_CLIENT_SECRET", "")
REDIRECT_URI  = os.getenv("CLIO_REDIRECT_URI", "http://127.0.0.1/callback")
REFRESH_TOKEN = os.getenv("CLIO_REFRESH_TOKEN", "")
BASE_URL      = os.getenv("CLIO_BASE_URL", "https://app.clio.com").rstrip("/")

if not CLIENT_ID or not CLIENT_SECRET:
    print("ERROR: CLIO_CLIENT_ID and CLIO_CLIENT_SECRET must be set in .env")
    sys.exit(1)


def update_env(access_token: str, refresh_token: str) -> None:
    text = ENV_PATH.read_text(encoding="utf-8")

    def replace_or_append(t: str, key: str, value: str) -> str:
        pattern = rf"^{re.escape(key)}=.*$"
        line = f"{key}={value}"
        if re.search(pattern, t, re.MULTILINE):
            return re.sub(pattern, line, t, flags=re.MULTILINE)
        return t + f"\n{line}"

    text = replace_or_append(text, "CLIO_ACCESS_TOKEN", access_token)
    # Only update refresh token if Clio returned a new one — they don't always rotate it
    if refresh_token:
        text = replace_or_append(text, "CLIO_REFRESH_TOKEN", refresh_token)
        print(f"  CLIO_REFRESH_TOKEN : {refresh_token[:12]}...")
    else:
        print("  CLIO_REFRESH_TOKEN : unchanged (Clio did not issue a new one)")
    ENV_PATH.write_text(text, encoding="utf-8")
    print(f"  CLIO_ACCESS_TOKEN  : {access_token[:12]}...")


def exchange_tokens(payload: dict) -> tuple[str, str]:
    r = requests.post(f"{BASE_URL}/oauth/token", data=payload, timeout=30)
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
        print("ERROR: CLIO_REFRESH_TOKEN not set in .env — run without --refresh for full browser flow")
        sys.exit(1)
    print("Refreshing token...")
    access, refresh = exchange_tokens({
        "grant_type":    "refresh_token",
        "refresh_token": REFRESH_TOKEN,
        "client_id":     CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    })
    print("Done. .env updated.")
    update_env(access, refresh)


def do_browser_flow() -> None:
    parsed = urlparse(REDIRECT_URI)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 80

    auth_url = (
        f"{BASE_URL}/oauth/authorize?"
        + urlencode({
            "response_type": "code",
            "client_id":     CLIENT_ID,
            "redirect_uri":  REDIRECT_URI,
            "scope":         SCOPES,
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

    print(f"Opening browser for Clio authorization...")
    print(f"Scopes: {SCOPES}\n")
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
