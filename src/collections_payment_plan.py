"""
collections_payment_plan.py — Reads a matter's "Payment Plan" Clio custom field
(id 19347918, field_type checkbox — confirmed live 2026-08-25).

There's no Clio API for payment plans themselves (Clio doesn't expose that as
a resource) — this custom field is the firm's own stand-in, checked by hand
once a payment plan is actually set up. Read-only, same reasoning as
collections_flarpl.py's FLARPL Recorded field: the "Payment plan" Handling
option on /collections records our own INTENTION to set one up; whether one
is actually in place is a fact staff set directly on the matter in Clio, and
the dashboard only ever reflects that back.
"""

import os

import requests

BASE_URL = os.getenv("CLIO_BASE_URL", "https://app.clio.com").rstrip("/")
FIELD_NAME = "Payment Plan"


def fetch_active_by_matter(session: requests.Session, matter_ids: list[int]) -> dict[int, bool]:
    """Batched live read, one call for every matter_id given — only called
    for matters whose collections action is currently "Payment plan" (see
    routes_collections.py), not every unpaid-bill matter."""
    if not matter_ids:
        return {}

    result: dict[int, bool] = {}
    resp = session.get(
        f"{BASE_URL}/api/v4/matters.json",
        params={"fields": "id,custom_field_values{field_name,value}", "ids[]": matter_ids},
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Failed to batch-fetch matters: {resp.status_code} {resp.text[:200]}")

    for m in resp.json().get("data", []):
        value = False
        for cfv in m.get("custom_field_values", []):
            if cfv.get("field_name") == FIELD_NAME:
                value = bool(cfv.get("value"))
                break
        result[m["id"]] = value
    return result
