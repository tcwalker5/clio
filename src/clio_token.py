"""
clio_token.py — the shared .env Clio token, renewed automatically when it expires.

Every module that calls Clio with the shared .env token (CLIO_ACCESS_TOKEN)
attaches CLIO_AUTH instead of building its own "Bearer ..." header:

    session.auth = CLIO_AUTH                 # or requests.get(..., auth=CLIO_AUTH)

CLIO_AUTH sends whatever token is current at request time. On a 401 from Clio
it renews the token with CLIO_REFRESH_TOKEN, saves both back to .env (exactly
what `uv run src/clio_auth.py --refresh` does by hand) and retries the request
once. Added 2026-10-26 (Ted: "let's autorenew if it fails") after the token
expired about a month after its last manual refresh and every Clio tool —
dashboard, RingCentral sync, scheduled monitors — failed at once with
`401 invalid_token`.

The dashboard and the Windows Scheduled Tasks are separate processes sharing
one .env, so a renewal is serialized with a lock file and first re-reads .env:
if another process already renewed (the token on disk differs from the one
that just failed), that token is used instead of renewing a second time.

If the refresh token itself is revoked/expired, renewal fails loudly with a
RuntimeError saying to run the full browser flow (`uv run src/clio_auth.py`).

Not used for signed-in staff tokens (web/auth.py clio_session()) — those are
stored per person in clio_user_tokens and already renewed there.
"""

import logging
import msvcrt
import os
import re
import threading
from contextlib import contextmanager
from pathlib import Path

import requests
from dotenv import dotenv_values, load_dotenv

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
LOCK_PATH = ENV_PATH.parent / "data" / "clio_token.lock"
load_dotenv(ENV_PATH)

BASE_URL = os.getenv("CLIO_BASE_URL", "https://app.clio.com").rstrip("/")

_thread_lock = threading.Lock()


def current_token() -> str:
    return os.getenv("CLIO_ACCESS_TOKEN", "")


def save_tokens(access_token: str, refresh_token: str) -> None:
    """Write the tokens into .env (in place, other lines untouched) and into
    this process's environment. Clio doesn't always rotate the refresh token —
    an empty one leaves the existing value alone."""
    text = ENV_PATH.read_text(encoding="utf-8")

    def replace_or_append(t: str, key: str, value: str) -> str:
        pattern = rf"^{re.escape(key)}=.*$"
        line = f"{key}={value}"
        if re.search(pattern, t, re.MULTILINE):
            return re.sub(pattern, lambda _: line, t, flags=re.MULTILINE)
        return t + f"\n{line}"

    text = replace_or_append(text, "CLIO_ACCESS_TOKEN", access_token)
    os.environ["CLIO_ACCESS_TOKEN"] = access_token
    if refresh_token:
        text = replace_or_append(text, "CLIO_REFRESH_TOKEN", refresh_token)
        os.environ["CLIO_REFRESH_TOKEN"] = refresh_token
    ENV_PATH.write_text(text, encoding="utf-8")


@contextmanager
def _env_file_lock():
    """Cross-process lock around a renewal. msvcrt.LK_LOCK retries for ~10s
    before raising, far longer than a token request takes."""
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOCK_PATH, "a+b") as f:
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
        try:
            yield
        finally:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)


def refresh_access_token(failed_token: str) -> str:
    """Return a working access token after `failed_token` got a 401 — either
    one another thread/process already saved to .env, or a freshly renewed one."""
    with _thread_lock, _env_file_lock():
        on_disk = dotenv_values(ENV_PATH)
        latest = on_disk.get("CLIO_ACCESS_TOKEN") or ""
        if latest and latest != failed_token:
            os.environ["CLIO_ACCESS_TOKEN"] = latest
            if on_disk.get("CLIO_REFRESH_TOKEN"):
                os.environ["CLIO_REFRESH_TOKEN"] = on_disk["CLIO_REFRESH_TOKEN"]
            logging.info("Clio access token was already renewed by another process — using it")
            return latest

        refresh_token = on_disk.get("CLIO_REFRESH_TOKEN") or os.getenv("CLIO_REFRESH_TOKEN", "")
        if not refresh_token:
            raise RuntimeError("Clio token expired and CLIO_REFRESH_TOKEN is not set in .env — run `uv run src/clio_auth.py`")
        resp = requests.post(f"{BASE_URL}/oauth/token", data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": os.getenv("CLIO_CLIENT_ID", ""),
            "client_secret": os.getenv("CLIO_CLIENT_SECRET", ""),
        }, timeout=30)
        if resp.status_code != 200 or not resp.json().get("access_token"):
            raise RuntimeError(
                f"Clio token expired and automatic renewal failed ({resp.status_code} {resp.text[:200]}) — "
                "run `uv run src/clio_auth.py` to sign in again"
            )
        tokens = resp.json()
        save_tokens(tokens["access_token"], tokens.get("refresh_token", ""))
        logging.warning("Clio access token expired — renewed automatically and saved to .env")
        return tokens["access_token"]


class ClioAuth(requests.auth.AuthBase):
    """requests auth for the shared .env token: current token on every request,
    one renew-and-retry on a 401 (same response-hook pattern requests' own
    HTTPDigestAuth uses to resend after a challenge)."""

    def __call__(self, r: requests.PreparedRequest) -> requests.PreparedRequest:
        r.headers["Authorization"] = f"Bearer {current_token()}"
        r.register_hook("response", self._retry_on_401)
        return r

    def _retry_on_401(self, resp: requests.Response, **kwargs) -> requests.Response:
        if resp.status_code != 401:
            return resp
        failed = resp.request.headers.get("Authorization", "").removeprefix("Bearer ")
        new_token = refresh_access_token(failed)

        resp.content  # drain so the connection can be reused
        resp.close()
        prep = resp.request.copy()
        prep.headers["Authorization"] = f"Bearer {new_token}"
        # connection.send() doesn't dispatch hooks, so the retry can't loop.
        retry = resp.connection.send(prep, **kwargs)
        retry.history.append(resp)
        retry.request = prep
        return retry


CLIO_AUTH = ClioAuth()
