"""
ringcentral_directory.py — Clio contacts/matters -> RingCentral company directory CSV.

Reads (Clio API, live — no manual export step):
  Open matters + their default client   matter_matching.fetch_open_matters()
                                         (fields extended with client{id})
  Opposing Counsel / Opposing Party      outlook_calendar.relationships.fetch_oc_op_contacts()
                                         (filtered to open matters here)
  Contact detail for every resolved ID   fetch_contacts() in this file
                                         (name, type, phone_numbers, company)

Writes:
  output/ringcentral_directory_YYYY-MM-DD.csv   RingCentral-ready import file
  output/ringcentral_conflicts_YYYY-MM-DD.csv   unresolved same-phone contacts (if any)
  logs/ringcentral_directory_YYYYMMDD.log
  data/clio_dashboard.db (ringcentral_sync_runs) — one row per run, including a hash
    of the built directory, so a run with no real change can be detected and skipped

RingCentral has no REST API for the shared company directory (only per-user personal
contacts support API writes — confirmed via RingCentral's own developer docs). The CSV
this writes must be uploaded by hand at:
  https://service.ringcentral.com/application/admin/tools/externalSharedContactsDirectory
Since that import replaces the whole directory, re-uploading an unchanged file is
noise — this script only opens that page when today's build actually differs from the
last run.

Usage:
  uv run src/ringcentral_directory.py             # build + open import page if changed
  uv run src/ringcentral_directory.py --no-open    # build only, never open a browser
"""

import argparse
import csv
import hashlib
import json
import logging
import os
import sys
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(ENV_PATH)

from matter_matching import fetch_open_matters  # noqa: E402
from outlook_calendar.relationships import fetch_oc_op_contacts  # noqa: E402

# ---------------------------------------------------------------------------
# Clio API
# ---------------------------------------------------------------------------

BASE_URL = os.getenv("CLIO_BASE_URL", "https://app.clio.com").rstrip("/")
ACCESS_TOKEN = os.getenv("CLIO_ACCESS_TOKEN", "")
CONTACTS_ENDPOINT = f"{BASE_URL}/api/v4/contacts.json"
# Subfields need explicit selection (phone_numbers{...} / company{...}) — a bare
# "phone_numbers" only returns {id, etag} stubs, confirmed against this account.
CONTACTS_FIELDS = (
    "id,name,first_name,last_name,type,"
    "phone_numbers{name,number,default_number},company{id,name},"
    "email_addresses{address}"
)
CONTACTS_PAGE_SIZE = 200
MATTER_FIELDS_WITH_CLIENT = "id,display_number,custom_number,status,client{id}"

# Real-data check (2026-07-21): every one of this firm's phone conflicts turned
# out to be Opposing Counsel at the same firm sharing a general office line —
# Clio's own `company` relationship is basically never set on OC/OP contacts,
# but their email domain reliably is. Personal domains never count as a match
# (millions of unrelated people share gmail.com).
PERSONAL_EMAIL_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "icloud.com",
    "aol.com", "live.com", "msn.com", "me.com", "mac.com", "ymail.com",
    "protonmail.com", "proton.me", "comcast.net", "sbcglobal.net",
}

RINGCENTRAL_IMPORT_URL = "https://service.ringcentral.com/application/admin/tools/externalSharedContactsDirectory"

# ---------------------------------------------------------------------------
# RingCentral's own documented import requirements (rolodex/rolodex.md §5, §7) —
# reused as platform facts, not ported implementation.
# ---------------------------------------------------------------------------

RC_ALLOWED_CHARS = set(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789 @.-+:,_()'"
)
RC_COLUMNS = [
    "First Name", "Last Name", "Job Title", "Company", "Email",
    "Home Number", "Business Number", "Mobile Number", "Company Main Number",
    "Source", "External ID",
]
RC_HEADER_PREAMBLE = (
    '"Please follow the instructions carefully and ensure that information is accurate, properly assigned and formatted.",,,,,\r\n'
    '"You will be presented with the error list (if any) and you will have the option to download the file, fix the errors and re-upload.",,,,,\r\n'
    '"You will always have the option to fix any assignment mistakes one at a time by logging into your account.",,,,,\r\n'
    ',,,,,\r\n'
    '"Instructions",,,,,\r\n'
    '"Allowed file formats: .CSV",,,,,\r\n'
    '"Allowed encoding standard: UTF-8",,,,,\r\n'
    '"Required fields cannot be left blank.",,,,,\r\n'
    '"Enter the following information to add contacts to the system:",,,,,\r\n'
    '"First Name OR Last Name",,,,,\r\n'
    '"Home Number or Business Number or Mobile Number or Company Main Number",,,,,\r\n'
    ',,,,,\r\n'
    '"The \'Home Number\',\'Business Number\',\'Mobile Number\' and \'Company Main Number\' fields, must be provided in full-length format including \'+\' sign, country code, area code and local number parts.",,,,,\r\n'
    '"For best results, edit the file using a simple text editor like Notepad or Wordpad, or similar, because spreadsheet programs like Excel may change the formatting of the \'Home Number\',\'Business Number\',\'Mobile Number\' and \'Company Main Number\' fields in the downloaded data (e.g., the \'+\' sign may be removed).",,,,,\r\n'
    '"Add at least one empty row before uploading to delete all contacts from the directory.",,,,,\r\n'
    ',,,,,\r\n'
    '"DO NOT",,,,,\r\n'
    '"-  change the order of columns",,,,,\r\n'
    '"-  add any new columns",,,,,\r\n'
    '"-  setup more than 50000 contacts at once",,,,,\r\n'
    ',,,,,\r\n'
    '"---- BEGINNING of DATA ----",,,,,\r\n'
    ',,,,,\r\n'
)

KB_COLUMNS = ["Phone", "First Name", "Last Name", "Job Title", "Company", "Email"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def setup_logging(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"ringcentral_directory_{datetime.today().strftime('%Y%m%d')}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        force=True,
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(open(sys.stdout.fileno(), mode="w", encoding="utf-8", errors="replace", closefd=False)),
        ],
    )
    logging.info("Log file: %s", log_file)


def sanitize_text(raw: str) -> str:
    """Strip characters RingCentral's importer rejects (double quotes, non-ASCII
    symbols) — a platform requirement, not a data-quality workaround. Clean Clio
    data can still carry a quoted nickname typed straight in, so this stays
    defensive even though the source is no longer scraped Word docs."""
    if not raw:
        return ""
    text = raw.replace("‘", "'").replace("’", "'")
    text = text.replace("“", "").replace("”", "").replace('"', "")
    return "".join(ch for ch in text if ch in RC_ALLOWED_CHARS).strip()


def normalize_phone_e164(raw: str) -> str | None:
    """10-digit US number -> +1XXXXXXXXXX; anything else -> None (RingCentral
    requires full E.164 including country code)."""
    digits = "".join(ch for ch in (raw or "") if ch.isdigit())
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits[0] == "1":
        return f"+{digits}"
    return None


def _email_domain(email: str) -> str | None:
    """Lowercased domain from an email address, or None if unusable/personal."""
    email = (email or "").strip().lower()
    if "@" not in email:
        return None
    domain = email.rsplit("@", 1)[-1].strip()
    return domain if domain and domain not in PERSONAL_EMAIL_DOMAINS else None


def _company_label_from_domain(domain: str) -> str:
    """Best-effort readable label derived from a domain, used only when nobody
    in a domain-matched group has a real Company name from Clio to prefer."""
    return sanitize_text(domain.split(".")[0].upper())


def _csv_field(val) -> str:
    val = str(val)
    if any(ch in val for ch in (",", '"', "\n", "\r")):
        return '"' + val.replace('"', '""') + '"'
    return val


# ---------------------------------------------------------------------------
# Gather target contacts
# ---------------------------------------------------------------------------

@dataclass
class DirectoryContact:
    contact_id: int
    first_name: str
    last_name: str
    is_company: bool
    company_id: int | None
    company_name: str
    phone: str  # normalized E.164
    email_domain: str | None = None  # non-personal domain, if any


def gather_target_contact_ids(session: requests.Session) -> set[int]:
    """Every Clio contact ID that belongs in the directory: the default client
    on an open matter, or Opposing Counsel/Opposing Party on an open matter."""
    matters = fetch_open_matters(session, fields=MATTER_FIELDS_WITH_CLIENT)
    client_ids = {int(m["client"]["id"]) for m in matters if (m.get("client") or {}).get("id")}

    oc_op = fetch_oc_op_contacts(session)
    oc_op_ids = {c.contact_id for c in oc_op if c.matter_status == "Open" and c.contact_id}

    logging.info("Target contacts: %d open-matter clients, %d open-matter OC/OP", len(client_ids), len(oc_op_ids))
    return client_ids | oc_op_ids


def fetch_contacts(session: requests.Session, contact_ids: set[int]) -> list[DirectoryContact]:
    """Fetch name/type/phone/company for exactly the contact IDs already known
    to be needed. Clio has no server-side "id in (...)" filter for an arbitrary
    set on this endpoint, so this pages through all contacts once and keeps only
    matches — the same approach clio_users.fetch_staff_directory() uses for the
    (smaller) users list."""
    results: list[DirectoryContact] = []
    next_url: str | None = None
    page = 1
    matched = 0

    while True:
        if next_url:
            resp = session.get(next_url)
        else:
            resp = session.get(CONTACTS_ENDPOINT, params={"fields": CONTACTS_FIELDS, "limit": CONTACTS_PAGE_SIZE})

        if resp.status_code != 200:
            raise RuntimeError(f"Failed to fetch contacts (page {page}): {resp.status_code} {resp.text[:200]}")

        body = resp.json()
        for c in body.get("data", []):
            cid = c.get("id")
            if not cid or int(cid) not in contact_ids:
                continue
            matched += 1

            phones = c.get("phone_numbers") or []
            ordered = sorted(phones, key=lambda p: not p.get("default_number", False))
            phone = next(filter(None, (normalize_phone_e164(p.get("number", "")) for p in ordered)), None)
            if not phone:
                continue  # no usable phone -> can't go in a phone directory

            is_company = c.get("type") == "Company"
            company = c.get("company") or {}
            emails = c.get("email_addresses") or []
            email_domain = next(filter(None, (_email_domain(e.get("address", "")) for e in emails)), None)

            results.append(DirectoryContact(
                contact_id=int(cid),
                first_name="" if is_company else sanitize_text((c.get("first_name") or "").upper()),
                last_name=sanitize_text(((c.get("name") if is_company else c.get("last_name")) or "").upper()),
                is_company=is_company,
                company_id=int(company["id"]) if company.get("id") else None,
                company_name=sanitize_text((company.get("name") or "").upper()),
                phone=phone,
                email_domain=email_domain,
            ))

        next_url = (body.get("meta") or {}).get("paging", {}).get("next")
        logging.info("Fetched contacts page %d", page)
        page += 1
        if not next_url:
            break

    logging.info("Resolved %d/%d target contacts with a usable phone", len(results), len(contact_ids))
    return results


# ---------------------------------------------------------------------------
# Phone knowledge base (manual conflict resolutions, persisted across runs)
# ---------------------------------------------------------------------------

def load_knowledge_base(path: Path) -> dict[str, dict]:
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            csv.writer(f).writerow(KB_COLUMNS)
        logging.info("Created empty knowledge base template at %s", path)
        return {}

    kb: dict[str, dict] = {}
    with open(path, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            phone = (row.get("Phone") or "").strip()
            if not phone:
                continue
            is_company = bool((row.get("Company") or "").strip())
            kb[phone] = {
                "First Name": row.get("First Name", ""),
                "Last Name": row.get("Last Name", ""),
                "Job Title": row.get("Job Title", ""),
                "Company": row.get("Company", ""),
                "Email": row.get("Email", ""),
                "Home Number": "",
                "Business Number": "",
                "Mobile Number": "" if is_company else phone,
                "Company Main Number": phone if is_company else "",
                "Source": "Clio Import",
            }
    logging.info("Loaded %d knowledge base entries from %s", len(kb), path)
    return kb


# ---------------------------------------------------------------------------
# Dedup by phone + build rows
# ---------------------------------------------------------------------------

def _row_for_single(c: DirectoryContact) -> dict:
    if c.is_company:
        return {
            "First Name": "", "Last Name": c.last_name, "Job Title": "",
            "Company": c.last_name, "Email": "",
            "Home Number": "", "Business Number": "",
            "Mobile Number": "", "Company Main Number": c.phone,
            "Source": "Clio Import", "External ID": str(c.contact_id),
        }
    return {
        "First Name": c.first_name, "Last Name": c.last_name, "Job Title": "",
        "Company": c.company_name, "Email": "",
        "Home Number": "", "Business Number": "",
        "Mobile Number": c.phone, "Company Main Number": "",
        "Source": "Clio Import", "External ID": str(c.contact_id),
    }


def _resolve_by_email_domain(group: list[DirectoryContact]) -> str | None:
    """None unless every contact in the group that has an email agrees on one
    non-personal domain. Prefers an existing Clio Company name from any group
    member over a label derived from the domain string, since the former is
    always better quality when available."""
    domains = {c.email_domain for c in group if c.email_domain}
    if len(domains) != 1:
        return None
    domain = domains.pop()
    named = next((c.company_name for c in group if c.company_name), None)
    return named or _company_label_from_domain(domain)


def _merged_last_name(group: list[DirectoryContact]) -> str:
    """RingCentral rejects the same phone number on a second row outright
    (confirmed live: "Object with desired [+1...] value exists.") — there's no
    way to give each person sharing a phone their own row, and RingCentral's
    directory search only covers First/Last Name, not Company or Job Title
    (confirmed via RingCentral's own support docs/community). So a merged row
    crams every individual's name into Last Name (comma-separated) instead of
    just the firm label, keeping the firm in Company for context — trades a
    tidy Last Name for staff actually being able to find these people by
    name."""
    names: list[str] = []
    for c in group:
        name = c.last_name if c.is_company else f"{c.first_name} {c.last_name}".strip()
        if name and name not in names:
            names.append(name)
    return ", ".join(names)


def build_directory_rows(
    contacts: list[DirectoryContact],
    knowledge_base: dict[str, dict],
) -> tuple[list[dict], list[dict]]:
    """RingCentral requires unique phone numbers, so contacts sharing a phone
    are grouped and resolved in order: a single contact on the phone goes
    straight through; contacts that all share the same Clio Company collapse
    to one company row; contacts that agree on an email domain (even without
    a Clio Company set) collapse the same way; anything left consults the
    knowledge base, falling through to an unresolved conflict for manual
    review."""
    by_phone: dict[str, dict[int, DirectoryContact]] = {}
    for c in contacts:
        by_phone.setdefault(c.phone, {}).setdefault(c.contact_id, c)

    rows: list[dict] = []
    conflicts: list[dict] = []

    for phone in sorted(by_phone):
        group = list(by_phone[phone].values())

        if len(group) == 1:
            rows.append(_row_for_single(group[0]))
            continue

        company_ids = [c.company_id for c in group]
        if all(cid is not None for cid in company_ids) and len(set(company_ids)) == 1:
            rep = group[0]
            rows.append({
                "First Name": "", "Last Name": _merged_last_name(group), "Job Title": "",
                "Company": rep.company_name, "Email": "",
                "Home Number": "", "Business Number": "",
                "Mobile Number": "", "Company Main Number": phone,
                "Source": "Clio Import",
                "External ID": "|".join(str(c.contact_id) for c in group),
            })
            continue

        # Clio's own Company relationship is rarely set on OC/OP contacts even
        # when they clearly work at the same firm — fall back to email domain.
        # Contacts with no email at all don't block the match (a shared phone
        # line plus a matching domain among those who *do* have email is
        # already strong evidence — validated against every real conflict at
        # this firm), but personal domains never count.
        label = _resolve_by_email_domain(group)
        if label:
            rows.append({
                "First Name": "", "Last Name": _merged_last_name(group), "Job Title": "",
                "Company": label, "Email": "",
                "Home Number": "", "Business Number": "",
                "Mobile Number": "", "Company Main Number": phone,
                "Source": "Clio Import",
                "External ID": "|".join(str(c.contact_id) for c in group),
            })
            continue

        if phone in knowledge_base:
            entry = dict(knowledge_base[phone])
            entry["External ID"] = "|".join(str(c.contact_id) for c in group)
            rows.append(entry)
            continue

        for c in group:
            conflicts.append({
                "Phone": phone,
                "Clio ID": c.contact_id,
                "First Name": c.first_name,
                "Last Name": c.last_name,
                "Company": c.company_name,
            })

    return rows, conflicts


# ---------------------------------------------------------------------------
# Output files
# ---------------------------------------------------------------------------

def write_directory_csv(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        f.write(RC_HEADER_PREAMBLE)
        f.write(",".join(RC_COLUMNS) + "\r\n")
        for row in rows:
            f.write(",".join(_csv_field(row.get(col, "")) for col in RC_COLUMNS) + "\r\n")


def write_conflicts_csv(conflicts: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=["Phone", "Clio ID", "First Name", "Last Name", "Company"])
        writer.writeheader()
        writer.writerows(conflicts)


def compute_snapshot_hash(rows: list[dict]) -> str:
    normalized = sorted(
        (r.get("External ID", ""), r.get("First Name", ""), r.get("Last Name", ""),
         r.get("Company", ""), r.get("Mobile Number", ""), r.get("Company Main Number", ""))
        for r in rows
    )
    blob = json.dumps(normalized, sort_keys=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Pipeline (shared by the CLI and the web dashboard)
# ---------------------------------------------------------------------------

@dataclass
class RunResult:
    rows: list[dict] = field(default_factory=list)
    conflicts: list[dict] = field(default_factory=list)
    changed: bool = False
    csv_path: Path | None = None
    conflicts_path: Path | None = None
    run_at: str = ""


def run_pipeline(output_dir: Path = Path("output"), data_dir: Path = Path("data")) -> RunResult:
    setup_logging(Path("logs"))

    if not ACCESS_TOKEN:
        raise RuntimeError("CLIO_ACCESS_TOKEN not set in .env")

    session = requests.Session()
    session.headers.update({
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json",
    })

    contact_ids = gather_target_contact_ids(session)
    contacts = fetch_contacts(session, contact_ids)

    kb_path = data_dir / "ringcentral_phone_knowledge_base.csv"
    knowledge_base = load_knowledge_base(kb_path)

    rows, conflicts = build_directory_rows(contacts, knowledge_base)

    today = datetime.today().strftime("%Y-%m-%d")
    output_dir.mkdir(exist_ok=True)
    csv_path = output_dir / f"ringcentral_directory_{today}.csv"
    write_directory_csv(rows, csv_path)
    logging.info("Wrote %d directory rows to %s", len(rows), csv_path)

    conflicts_path = None
    if conflicts:
        conflicts_path = output_dir / f"ringcentral_conflicts_{today}.csv"
        write_conflicts_csv(conflicts, conflicts_path)
        logging.warning("Wrote %d conflict rows to %s — resolve manually or add to %s",
                         len(conflicts), conflicts_path, kb_path.name)

    snapshot_hash = compute_snapshot_hash(rows)

    from web.db import get_connection  # local import: avoids web deps for CLI-only runs
    conn = get_connection()
    try:
        last = conn.execute(
            "SELECT snapshot_hash FROM ringcentral_sync_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        changed = last is None or last["snapshot_hash"] != snapshot_hash
        conn.execute(
            """INSERT INTO ringcentral_sync_runs
               (changed, included_count, conflict_count, csv_path, conflicts_path, snapshot_hash)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (int(changed), len(rows), len(conflicts),
             str(csv_path), str(conflicts_path) if conflicts_path else None, snapshot_hash),
        )
        conn.commit()
        run_at = conn.execute(
            "SELECT run_at FROM ringcentral_sync_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()["run_at"]
    finally:
        conn.close()

    logging.info("Summary: %d contacts resolved, %d directory rows, %d conflicts, changed=%s",
                 len(contacts), len(rows), len(conflicts), changed)

    return RunResult(rows=rows, conflicts=conflicts, changed=changed,
                      csv_path=csv_path, conflicts_path=conflicts_path, run_at=run_at)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-open", action="store_true",
                         help="Build the directory CSV but never open the RingCentral import page")
    args = parser.parse_args()

    try:
        result = run_pipeline()
    except RuntimeError as e:
        logging.error(str(e))
        sys.exit(1)

    if result.changed:
        logging.info("Directory changed since last sync (%d rows, %d conflicts)",
                      len(result.rows), len(result.conflicts))
        if not args.no_open:
            webbrowser.open(RINGCENTRAL_IMPORT_URL)
    else:
        logging.info("No change since last sync — nothing to upload")


if __name__ == "__main__":
    main()
