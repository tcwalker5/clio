"""
matter_fields.py — Live Clio API helpers for matter fields once assumed to
be CSV-export-only: Responsible/Originating Attorney, Responsible Staff, and
Court Case Number.

That assumption (see git history's matters_csv.py, now removed) was wrong —
these just aren't plain fields on the base Matter resource, so a flat
`fields=` list returns nothing for them (the same gotcha bradford_invoice.py
already hit with `custom_rate`). Confirmed live 2026-07-24 against a real
matter (BASSETT, KIRSTEN) against what the Clio UI itself showed:
  - responsible_attorney / originating_attorney / responsible_staff are
    nested User relationships -> request with `{name}`.
  - Court Case Number is a Matter custom field -> only surfaces via
    `custom_field_values{field_name,value}`, not a plain field name.
Both matched the live UI exactly (Pamela Bradford / Patricia Payne / Heidi
Collier / 26FL001421N) at a time when data/clio-matters.csv had already
drifted stale (last exported weeks earlier) and showed different, wrong
values for the same matter. Reading these live instead of from that CSV
means any staff member can run a comparison without knowing how to export
matters from Clio.
"""

MATTER_SYNC_FIELDS = (
    "id,display_number,custom_number,status,"
    "responsible_attorney{name},responsible_staff{name},originating_attorney{name},"
    "custom_field_values{field_name,value}"
)

CASE_NUMBER_FIELD = "Court Case Number"


def _custom_field(matter: dict, field_name: str) -> str:
    for cfv in matter.get("custom_field_values") or []:
        if cfv.get("field_name") == field_name:
            return (cfv.get("value") or "").strip()
    return ""


def index_case_numbers_by_matter_id(matters: list[dict]) -> dict[int, str]:
    """Clio matter ID -> on-file Court Case Number (empty string if not set)."""
    return {int(m["id"]): _custom_field(m, CASE_NUMBER_FIELD).upper() for m in matters}


def index_matter_owner_by_matter_id(matters: list[dict]) -> dict[int, str]:
    """
    Clio matter ID -> matter owner full name: Responsible Staff if set, else
    Responsible Attorney (Responsible Staff is left blank on plenty of
    matters in practice — see matcher.py's matter_owner_initials note).
    """
    result: dict[int, str] = {}
    for m in matters:
        staff = (m.get("responsible_staff") or {}).get("name") or ""
        attorney = (m.get("responsible_attorney") or {}).get("name") or ""
        result[int(m["id"])] = staff.strip() or attorney.strip()
    return result


def index_report_fields_by_last_name(matters: list[dict]) -> dict[str, dict]:
    """
    LAST name -> {display_number, responsible_attorney, originating_attorney,
    responsible_staff} for client_list.py's client court-date report. First
    match wins on ambiguity (a client with two open matters only needs one
    set of these fields for the report).
    """
    index: dict[str, dict] = {}
    for m in matters:
        display = (m.get("display_number") or "").strip()
        if not display:
            continue
        last = display.split(",")[0].strip().upper()
        if last in index:
            continue
        index[last] = {
            "display_number": display,
            "responsible_attorney": (m.get("responsible_attorney") or {}).get("name") or "",
            "originating_attorney": (m.get("originating_attorney") or {}).get("name") or "",
            "responsible_staff": (m.get("responsible_staff") or {}).get("name") or "",
        }
    return index
