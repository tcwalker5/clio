"""
collections_flarpl.py — Reads a matter's "FLARPL Recorded" Clio custom field
(id 19226673, field_type checkbox — confirmed live 2026-08-18).

A FLARPL (Family Law Attorney's Real Property Lien) secures attorney's fees
against a property that will be sold later. Recording it is an external act
— filed with the county recorder, not something anyone does by clicking a
button in this dashboard. **Read-only, deliberately** (Ted, 2026-08-19): the
"FLARPL" Handling option on /collections records our own INTENTION to
pursue one; whether it has actually been recorded is a fact that lives in
Clio (staff update it there once the county confirms), and the dashboard
only ever reflects that back, never sets it. An earlier version of this
module also had a set_recorded() write path — removed, since a write
function nothing is allowed to call is just a live foot-gun sitting in the
code.
"""

import os

import requests

BASE_URL = os.getenv("CLIO_BASE_URL", "https://app.clio.com").rstrip("/")
FIELD_NAME = "FLARPL Recorded"


def fetch_recorded_by_matter(session: requests.Session, matter_ids: list[int]) -> dict[int, bool]:
    """Batched live read, one call for every matter_id given — only called
    for matters whose collections action is currently "FLARPL" (see
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
