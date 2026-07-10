"""
clio_users.py — Fetch and cache the firm's Clio staff directory.

Replaces hardcoded per-user env vars (USER_ID_HEIDI, USER_ID_PAM, etc.) for any
feature that needs to resolve a Clio user ID to a display name — e.g. the
court calendar sync's attorney/staff assignment, which reads a Clio calendar
entry's calendar_owner_id / attendees[].id rather than parsing free text.

Usage:
  from clio_users import get_staff_directory
  staff = get_staff_directory()   # {user_id: {"name": ..., "first_name": ..., "email": ...}}
"""

import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(ENV_PATH)

BASE_URL = os.getenv("CLIO_BASE_URL", "https://app.clio.com").rstrip("/")
ACCESS_TOKEN = os.getenv("CLIO_ACCESS_TOKEN", "")
USERS_ENDPOINT = f"{BASE_URL}/api/v4/users.json"
USERS_FIELDS = "id,name,first_name,last_name,email,enabled"
USERS_PAGE_SIZE = 200


def fetch_staff_directory(session: requests.Session | None = None) -> list[dict]:
    """Fetch all enabled users from the Clio API. Returns raw user dicts."""
    own_session = session is None
    if own_session:
        session = requests.Session()
        session.headers.update({
            "Authorization": f"Bearer {ACCESS_TOKEN}",
            "Content-Type": "application/json",
        })

    try:
        users: list[dict] = []
        next_url: str | None = None

        while True:
            # meta.paging.next is a full URL with page_token embedded — fetch
            # it as-is (see matter_matching.fetch_open_matters for why).
            if next_url:
                resp = session.get(next_url)
            else:
                resp = session.get(USERS_ENDPOINT, params={"fields": USERS_FIELDS, "limit": USERS_PAGE_SIZE})

            if resp.status_code != 200:
                raise RuntimeError(f"Failed to fetch users: {resp.status_code} {resp.text[:200]}")

            body = resp.json()
            users.extend(u for u in body.get("data", []) if u.get("enabled", True))

            next_url = (body.get("meta") or {}).get("paging", {}).get("next")
            if not next_url:
                break

        return users
    finally:
        if own_session:
            session.close()


def get_staff_directory() -> dict[int, dict]:
    """
    Fetch the current staff directory keyed by Clio user ID:
      { user_id: {"name": "Pamela Bradford", "first_name": "Pamela", "email": "..."} }

    Also persists to the staff_cache table (via web.db) so pages can render
    without an extra live API call; callers needing fresh data should call
    this directly rather than reading the cache.
    """
    from web.db import get_connection  # local import: avoids web deps for CLI-only scripts

    users = fetch_staff_directory()
    directory = {
        int(u["id"]): {
            "name": u.get("name", ""),
            "first_name": u.get("first_name", ""),
            "email": u.get("email", ""),
        }
        for u in users
        if u.get("id")
    }

    conn = get_connection()
    try:
        conn.executemany(
            """
            INSERT INTO staff_cache (id, name, first_name, email, updated_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(id) DO UPDATE SET
                name = excluded.name,
                first_name = excluded.first_name,
                email = excluded.email,
                updated_at = CURRENT_TIMESTAMP
            """,
            [(uid, d["name"], d["first_name"], d["email"]) for uid, d in directory.items()],
        )
        conn.commit()
    finally:
        conn.close()

    return directory


if __name__ == "__main__":
    if not ACCESS_TOKEN:
        print("ERROR: CLIO_ACCESS_TOKEN not set in .env")
        sys.exit(1)
    directory = get_staff_directory()
    for uid, info in sorted(directory.items(), key=lambda kv: kv[1]["name"]):
        print(f"{uid:>12}  {info['name']:<25} {info['email']}")
