"""
Tests for clio_token.py's automatic renewal of the shared .env Clio token.

Real failure this guards against (2026-10-26): the .env access token expired
about a month after its last manual refresh, and every Clio call in the
project failed with `401 invalid_token` until someone ran
`uv run src/clio_auth.py --refresh` by hand.

A tiny local HTTP server stands in for Clio: API calls succeed only with the
token "fresh", and /oauth/token hands out "fresh" for refresh token "r1".
Each test gets its own temp .env and lock file, so the real .env is never read
or written.
"""

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
import requests

import clio_token
from clio_token import CLIO_AUTH


class FakeClio(BaseHTTPRequestHandler):
    token_requests = 0
    refresh_ok = True

    def do_GET(self):
        ok = self.headers.get("Authorization") == "Bearer fresh"
        self._reply(200 if ok else 401, {"data": "ok"} if ok else {"error": "invalid_token"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode()
        type(self).token_requests += 1
        if self.path == "/oauth/token" and self.refresh_ok and "refresh_token=r1" in body:
            self._reply(200, {"access_token": "fresh", "refresh_token": "r2"})
        else:
            self._reply(400, {"error": "invalid_grant"})

    def _reply(self, status, payload):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


@pytest.fixture
def clio(tmp_path, monkeypatch):
    server = HTTPServer(("127.0.0.1", 0), FakeClio)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    FakeClio.token_requests = 0
    FakeClio.refresh_ok = True

    env = tmp_path / ".env"
    env.write_text("CLIO_CLIENT_ID=id\nCLIO_ACCESS_TOKEN=stale\nCLIO_REFRESH_TOKEN=r1\nOTHER=keep\n", encoding="utf-8")
    monkeypatch.setattr(clio_token, "ENV_PATH", env)
    monkeypatch.setattr(clio_token, "LOCK_PATH", tmp_path / "clio_token.lock")
    monkeypatch.setattr(clio_token, "BASE_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("CLIO_ACCESS_TOKEN", "stale")
    monkeypatch.setenv("CLIO_REFRESH_TOKEN", "r1")
    yield f"http://127.0.0.1:{server.server_port}", env
    server.shutdown()


def test_expired_token_is_renewed_saved_and_request_retried(clio):
    base, env = clio
    session = requests.Session()
    session.auth = CLIO_AUTH

    resp = session.get(f"{base}/api/v4/matters.json")

    assert resp.status_code == 200
    assert [r.status_code for r in resp.history] == [401]
    assert FakeClio.token_requests == 1
    text = env.read_text(encoding="utf-8")
    assert "CLIO_ACCESS_TOKEN=fresh\n" in text
    assert "CLIO_REFRESH_TOKEN=r2\n" in text
    assert "OTHER=keep\n" in text  # rest of .env untouched
    assert os.environ["CLIO_ACCESS_TOKEN"] == "fresh"

    # Later requests use the new token straight away — no second renewal.
    assert session.get(f"{base}/api/v4/matters.json").history == []
    assert FakeClio.token_requests == 1


def test_works_without_a_session(clio):
    base, _ = clio
    resp = requests.get(f"{base}/api/v4/users.json", auth=CLIO_AUTH)
    assert resp.status_code == 200


def test_token_already_renewed_by_another_process_is_reused(clio):
    """The dashboard and scheduled tasks share .env — if another process
    already renewed, don't burn the refresh token a second time."""
    base, env = clio
    env.write_text(env.read_text(encoding="utf-8").replace("CLIO_ACCESS_TOKEN=stale", "CLIO_ACCESS_TOKEN=fresh"), encoding="utf-8")

    resp = requests.get(f"{base}/api/v4/matters.json", auth=CLIO_AUTH)

    assert resp.status_code == 200
    assert FakeClio.token_requests == 0


def test_failed_renewal_says_how_to_fix_it(clio):
    base, env = clio
    FakeClio.refresh_ok = False

    with pytest.raises(RuntimeError, match="clio_auth.py"):
        requests.get(f"{base}/api/v4/matters.json", auth=CLIO_AUTH)
    assert "CLIO_ACCESS_TOKEN=stale\n" in env.read_text(encoding="utf-8")
