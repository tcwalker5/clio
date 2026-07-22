"""
matter_matching.py — Shared Clio open-matter lookup for name-based matching.

Used by printer_expenses.py, bradford_invoice.py, and court_calendar/matcher.py.
Each caller keeps its own MANUAL_MATTER_MAP constant (explicit, auditable,
per-source overrides) — only the fetch/index logic is shared here.

Two indexing strategies, because each source only has a different slice of
the client's name:
  - index_by_display_name(): printer report has "LAST, FIRST" — matches
    Clio's display_number format directly, preferring an entry with a
    custom_number (MUID) set when a display name is ambiguous.
  - index_by_last_name(): Bradford invoice and court calendar text only have
    a last name — ambiguous when multiple open matters share one.
"""

import logging
import re

import requests

BASE_URL_DEFAULT = "https://app.clio.com"
MATTERS_FIELDS = "id,display_number,custom_number,status"
MATTERS_PAGE_SIZE = 200


def normalize_name(raw: str) -> str:
    """Strip extra quotes/whitespace, normalize comma spacing, uppercase."""
    s = raw.strip().strip('"').strip("'").strip()
    s = re.sub(r",\s*", ", ", s)
    return s.upper()


def fetch_open_matters(
    session: requests.Session,
    base_url: str = BASE_URL_DEFAULT,
    fields: str = MATTERS_FIELDS,
) -> list[dict]:
    """Fetch all open matters from the Clio API, handling pagination.

    `fields` defaults to the id/display_number/custom_number/status callers
    normally need; pass a wider string (e.g. adding `,client{id}`) when a
    caller also needs a nested relationship — the `relationship{subfields}`
    syntax is the same one relationships.py already uses successfully.
    """
    matters_endpoint = f"{base_url.rstrip('/')}/api/v4/matters.json"
    matters: list[dict] = []
    next_url: str | None = None
    page = 1

    while True:
        # meta.paging.next is a full URL (with page_token already embedded) —
        # fetch it as-is rather than re-extracting a token to rebuild params;
        # passing the whole URL back as a "page_token" value is what Clio's
        # API rejects with "page_token is invalid" once there's a page 2.
        if next_url:
            resp = session.get(next_url)
        else:
            params = {
                "status": "open",
                "fields": fields,
                "limit": MATTERS_PAGE_SIZE,
            }
            resp = session.get(matters_endpoint, params=params)

        if resp.status_code != 200:
            raise RuntimeError(f"Failed to fetch matters (page {page}): {resp.status_code} {resp.text[:200]}")

        body = resp.json()
        records = body.get("data", [])
        matters.extend(records)

        next_url = body.get("meta", {}).get("paging", {}).get("next")
        logging.info("Fetched matters page %d — %d records", page, len(records))
        page += 1
        if not next_url:
            break

    logging.info("Fetched %d open matters from Clio API", len(matters))
    return matters


def index_by_display_name(matters: list[dict]) -> dict[str, int | None]:
    """
    normalized display_number ("LAST, FIRST") -> matter ID.
    If multiple open matters share a display name, prefer the one with a
    custom_number (MUID) set; if still ambiguous, None (needs manual override).
    """
    raw_index: dict[str, list[tuple[int, bool]]] = {}
    for m in matters:
        display = (m.get("display_number") or "").strip()
        if not display:
            continue
        matter_id = int(m["id"])
        has_ref = bool((m.get("custom_number") or "").strip())
        key = normalize_name(display)
        raw_index.setdefault(key, []).append((matter_id, has_ref))

    result: dict[str, int | None] = {}
    for name, entries in raw_index.items():
        if len(entries) == 1:
            result[name] = entries[0][0]
        else:
            with_ref = [mid for mid, has_ref in entries if has_ref]
            result[name] = with_ref[0] if len(with_ref) == 1 else None

    logging.info("Indexed %d distinct open matter display names", len(result))
    return result


def index_by_last_name(matters: list[dict]) -> dict[str, int | None]:
    """
    LAST name (from display_number's "LAST, FIRST") -> matter ID.
    None when multiple open matters share the same last name.
    """
    raw = index_by_last_name_all(matters)
    result: dict[str, int | None] = {name: (ids[0] if len(ids) == 1 else None) for name, ids in raw.items()}
    logging.info("Indexed %d distinct open matter last names", len(result))
    return result


def index_by_last_name_all(matters: list[dict]) -> dict[str, list[int]]:
    """
    LAST name -> every open matter ID sharing it (unlike index_by_last_name,
    doesn't collapse to None on ambiguity) — a client with two open matters
    (e.g. two different case numbers) shows up with both IDs, so a caller can
    disambiguate some other way (see court_calendar/matcher.py's case-number
    cross-check).
    """
    raw: dict[str, list[int]] = {}
    for m in matters:
        display = (m.get("display_number") or "").strip()
        if not display:
            continue
        last = display.split(",")[0].strip().upper()
        raw.setdefault(last, []).append(int(m["id"]))
    return raw
