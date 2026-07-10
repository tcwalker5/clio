# Clio Project

Central Python monorepo for all Clio Manage API interactions at Collier Law.

Replaces scattered PowerShell scripts from rolodex, clio2ts, and clio-rate-import.
No .ps1 scripts — Python only.

---

# Environment

## Platform
- Windows 11 / PowerShell primary
- Python 3.14
- `uv` package manager

Cross-platform compatible (macOS secondary).

## Setup
```powershell
uv sync
copy .env.example .env   # then fill in credentials
```

## .env keys
```
CLIO_CLIENT_ID
CLIO_CLIENT_SECRET
CLIO_REDIRECT_URI
CLIO_ACCESS_TOKEN       # Bearer token for all API calls
CLIO_REFRESH_TOKEN
CLIO_BASE_URL=https://app.clio.com

USER_ID_HEIDI
USER_ID_SANDY
USER_ID_DALINAH
USER_ID_PAM
USER_ID_TED
USER_ID_DAHANN

CLIO_DASHBOARD_SECRET       # web dashboard session signing key
CLIO_DASHBOARD_PASSPHRASE  # web dashboard shared login passphrase
```

Staff shown in the dashboard (court calendar attorney/staff assignment, matter lookups)
come live from Clio's `/users.json` via `src/clio_users.py`, not just the `USER_ID_*` env
vars above — those stay for the two scripts that post activities under a specific user
(Bradford invoice's `PAM_USER_ID`). The live directory is what stays current as staff
change, e.g. paralegals added after this file was last edited.

---

# Project Structure

```
clio/
├── data/               # Input files — CSV reports, matter exports, invoice PDFs
├── output/             # Generated payloads and exception reports
├── logs/               # Per-run API logs
├── src/
│   ├── clio_auth.py              # OAuth token management (shared)
│   ├── matter_matching.py        # Shared name -> Clio matter ID lookup (all subprojects)
│   ├── clio_users.py             # Clio staff directory (/users.json), cached to SQLite
│   ├── printer_expenses.py       # Subproject 1
│   ├── bradford_invoice.py       # Subproject 2
│   ├── court_calendar/           # Subproject 4 — court calendar sync
│   │   ├── normalizer.py         #   court-text parsing, dept/purpose normalization
│   │   ├── clio_calendar.py      #   /calendar_entries.json client
│   │   ├── matcher.py            #   matter-ID-first + text-fallback diff
│   │   ├── store.py              #   SQLite persistence for parsed court events
│   │   └── client_list.py        #   client court-date report (HTML + Word)
│   └── web/                      # Dashboard — FastAPI app wrapping all subprojects
│       ├── app.py                #   routes, auth, dashboard home
│       ├── db.py                 #   SQLite schema (data/clio_dashboard.db)
│       ├── routes_*.py           #   one router per app (bradford/printer/calendar)
│       ├── templates/            #   Jinja2 templates
│       └── static/                #   CSS + drag-and-drop JS
├── start-dashboard.bat            # Launches the dashboard (uv sync + uvicorn)
├── start-dashboard-silent.vbs     # Same, without a visible console window
├── .env                # Credentials (gitignored)
├── .env.example
├── pyproject.toml
└── CLAUDE.md
```

Each subproject script has its own `main()` and can be run directly via `uv run src/<script>.py`.
The web dashboard (`src/web/app.py`) wraps subprojects 1, 2, and 4 with a browser UI — see
"Web Dashboard" below. It does not replace the CLI entry points, which still work standalone.

---

# Clio API

## Reference
OpenAPI spec: `C:\Users\TEDMINI\projects\clio-rate-import\data\openapi.json`
Always derive endpoints, payload shapes, and required fields from this file.
Do NOT invent endpoints.

## Authentication
```http
Authorization: Bearer {CLIO_ACCESS_TOKEN}
```

### Token management
One Clio app registration covers all subprojects in this repo.
Required scopes: `matters activities`

```powershell
# First time (browser flow)
uv run src/clio_auth.py

# Token expired (no browser needed)
uv run src/clio_auth.py --refresh
```

Both commands update `CLIO_ACCESS_TOKEN` and `CLIO_REFRESH_TOKEN` in `.env` automatically.

### Pagination gotcha
`meta.paging.next` in a Clio API list response is a **full URL** (query params already
embedded, including `page_token`) — fetch it as-is. Re-extracting a token and rebuilding
your own params dict around it produces a URL nested inside a query param value, which
Clio rejects with `"page_token is invalid"` once there's a page 2. `matter_matching.py`
and `clio_users.py` both follow `next` directly for this reason.

## Safety rules (apply to ALL scripts)
- Dry-run mode required — `--dry-run` flag must work before any live run
- Log every request: timestamp, matter ID, payload, response status
- Retry on rate limit (429)
- Continue on failure — log error, move to next item
- Never silently overwrite existing data

---

# Development Philosophy

1. Auditability — every transformation inspectable from CSV/log output
2. Fail loud — hard errors on unmapped values, never silently skip
3. Explicit mappings — editable constants near top of each file
4. Rerunnable — safe to run multiple times
5. Readable over clever

---

# Web Dashboard

**App:** `src/web/app.py` (FastAPI + Jinja2 + vanilla JS — no Node/npm toolchain)

**Purpose:** Browser UI for the whole repo — a home page linking to drag-and-drop versions
of Subprojects 1 and 2, plus the Court Calendar Sync (Subproject 4). Wraps each script's
existing `run_pipeline()` function; does not duplicate matching/posting logic.

**Run:**
```powershell
uv run uvicorn web.app:app --app-dir src --host 0.0.0.0 --port 8420
# or just double-click start-dashboard.bat (syncs deps, then starts)
```
`--app-dir src` puts `src/` on `sys.path`, matching how the standalone scripts resolve
flat imports (`import matter_matching`, etc.) — same convention, one process.

**Auth:** Single shared passphrase (`CLIO_DASHBOARD_PASSPHRASE` in `.env`), not per-staff
login — the Clio side already uses one shared app registration. Session is a signed cookie
(`CLIO_DASHBOARD_SECRET`). This is LAN-only gatekeeping, not intended as a public-internet
login system.

**Storage:** SQLite at `data/clio_dashboard.db` (gitignored) — court events, purpose
mappings, staff cache. No external database service.

**Drag-and-drop apps (Bradford Invoice, Printer Expenses):** upload a file -> dry-run
preview (payloads + exceptions, same categories as the CLI) -> "Confirm & Post" button ->
live run. Mirrors the CLI's `--dry-run` workflow; the confirm step is the only path that
posts to Clio.

**Remote access off the office LAN:** install [Tailscale](https://tailscale.com) on the
machine running the dashboard and on any device that needs to reach it — no port
forwarding or public-facing server required. The dashboard itself only ever binds to the
LAN/Tailscale interface, never a public one.

**New `.env` keys:**
```
CLIO_DASHBOARD_SECRET       # session cookie signing key
CLIO_DASHBOARD_PASSPHRASE   # shared login passphrase
```

---

# Subproject 1 — Printer Expenses

**Script:** `src/printer_expenses.py`

**Purpose:** Convert monthly Papercut print/copy/scan report to Clio ExpenseEntry API calls.

**Input:** `data/print_copy_summary_by_account.csv` (Papercut export)

**Matter lookup:** `data/clio-matters.csv` (export from Clio; refresh as needed)

**Rate:** $0.10 per page (firm policy, hardcoded as `PRICE_PER_PAGE`)

**Grouping:** All PRINT + SCAN + COPY aggregated into one ExpenseEntry per matter.

**Date:** Extracted from the report header comment ("To date = ...").

**Outputs:**
- `output/expenses_YYYY-MM.json` — API payloads (always written)
- `output/exceptions_YYYY-MM.csv` — names that didn't match (manual resolution needed)
- `logs/printer_expenses_YYYYMMDD.log`

## Manual overrides
When a name doesn't auto-match (different spelling, joint client, etc.), add it to
`MANUAL_MATTER_MAP` at the top of the script:
```python
MANUAL_MATTER_MAP: dict[str, int] = {
    "COLTON": 1234567890,  # Clio matter ID from clio-matters.csv
}
```

## Workflow
```powershell
# 1. Drop new Papercut export into data/
# 2. Refresh matter list if needed (export from Clio → data/clio-matters.csv)

# 3. Dry run — check output/exceptions_YYYY-MM.csv for unmatched names
uv run src/printer_expenses.py --dry-run

# 4. Fix exceptions: add matter IDs to MANUAL_MATTER_MAP, re-run dry-run

# 5. Live run
uv run src/printer_expenses.py
```

## Exception types
- **No matching open matter** — client name not found in clio-matters.csv
- **Ambiguous** — multiple open matters for same display name; add to MANUAL_MATTER_MAP
- **Joint client** — name contains " & "; split across matters manually

---

# Subproject 2 — Bradford Invoice Import

**Script:** `src/bradford_invoice.py`

**Purpose:** Parse PL Bradford Law LLC monthly invoice PDF and post time entries to Clio
as TimeEntry activities under Pamela Bradford (PAM).

**Input:** `data/Invoice-NNNNN THROUGH MONTH DD, YYYY.pdf` (Bradford invoice PDF)

**Contractor:** PL Bradford Law LLC — Pamela Bradford, Esq. + paralegal Taijah Miles.

**Two invoice formats in one PDF:**
- Pages 1-3: Attorney time — `Hours  CLIENT-DATE-ACTIVITY  PRICE  QTY  TOTAL`
- Pages 4-8: Paralegal time — Clio export attachment with Date / Duration / Description / Case columns

**Billing rules:**
- Attorney entries: omit `price` from payload — Clio applies PAM's matter-defined rate
- Paralegal entries: `price = $150/hr` (PARALEGAL_RATE constant), posted under PAM user ID
- ADMIN entries on main invoice: skipped — firm absorbs, not billed to clients
- "PARALEGAL TIME ***SEE ATTACHED" summary lines: skipped — detail comes from pages 4-8

**User:** All entries posted under `USER_ID_PAM` (359115091)

**Hours rounding:** Pre-rounded to nearest 0.1h using half-up rounding before sending
to Clio (matches Clio's own billing increment; avoids post-upload surprises).

**Outputs:**
- `output/{stem}_payloads.json` — API payloads (always written)
- `output/{stem}_exceptions.csv` — unmatched client names (if any)
- `logs/bradford_invoice_YYYYMMDD.log`

## Manual overrides
Invoice uses only last names. When auto-match fails or is ambiguous, add to
`MANUAL_MATTER_MAP` at the top of the script:
```python
MANUAL_MATTER_MAP: dict[str, int] = {
    "LARSON": 1786834653,  # Clio: LARSEN, NOEL (typo on invoice)
}
```

## Workflow
```powershell
# 1. Save Bradford PDF to data/

# 2. Dry run — check log for exceptions or unmatched names
uv run src/bradford_invoice.py --input "data/Invoice-*.pdf" --dry-run

# 3. Fix exceptions: add matter IDs to MANUAL_MATTER_MAP, re-run dry-run

# 4. Live run
uv run src/bradford_invoice.py --input "data/Invoice-*.pdf"

# 5. Export TimeEntries from Clio and reconcile against invoice
```

## Exception types
- **No matching open matter** — client last name not in Clio open matters
- **Ambiguous** — multiple open matters share the same last name; add to MANUAL_MATTER_MAP
- **Closed matter** — client matter closed in Clio; redirect to active matter via MANUAL_MATTER_MAP

---

# Subproject 3 — PaperCut Shared Account Sync

**Script:** (planned) `src/generate_papercut_accounts.py`

**Purpose:** Generate a PaperCut-compatible TSV from Clio open matters so PaperCut's
shared accounts stay automatically aligned with active Clio matters. Eliminates
manual name-matching and the mismatch problem in printer_expenses.py.

**Flow:**
1. Pull all open matters from Clio API
2. Write TSV in PaperCut batch import format
3. PaperCut sync feature reads the file hourly/nightly

**PaperCut TSV columns (tab-delimited, no header row):**
```
A  Parent Account Name   → matter Display Number  e.g. "ALCANTAR, JUAN"
B  Sub-account Name      → blank (top-level accounts)
C  Enabled               → Y (open matters only)
D  Account PIN/Code      → Clio matter Unique ID  (makes expense matching exact)
E  Credit Balance        → blank
F  Restricted Status     → blank
G  Users                 → blank (use Groups instead)
H  Groups                → [All Users] or specific staff group TBD
I  Invoice Option        → ALWAYS_INVOICE
J  Comment Option        → blank (COMMENT_OPTIONAL default)
K  Notes                 → blank
```

**Key design decision:** Storing the Clio matter `Unique ID` as the PaperCut PIN/Code
means `printer_expenses.py` can match on PIN/Code instead of fuzzy name matching —
no more manual overrides needed.

**PaperCut sync docs:** Accounts > Shared Account Sync > Text file source.
File location must be accessible from the PaperCut server (mapped drive or UNC path).
Sync runs Hourly or Overnight (nightly ~12:55am).

---

# Subproject 4 — Court Calendar Sync

**Modules:** `src/court_calendar/` (used through the web dashboard, `/calendar`)

**Purpose:** Verify every SD Superior Court hearing has a matching Clio calendar entry —
a Python/Clio port of the standalone `calendar-check` project, which compared against
Outlook via Microsoft Graph. Read-only: shows what's out of sync, never writes to Clio.

**Input:** Pasted SD Superior Court calendar search results (same fixed-width text
`calendar-check` scraped) — paste into the textarea on `/calendar`.

**Matching strategy — matter-ID-first, text-fallback:**
1. Each court event's party name resolves to a Clio matter ID via `matter_matching.py`
   (same lookup Subprojects 1/2 use).
2. If resolved, look for a Clio calendar entry already linked to that matter
   (`calendar_entries.json?matter_id=...`) on the same date. Compare time/dept/purpose
   (parsed from the entry's `summary`/`description`) to flag drift.
3. If the party doesn't resolve to a matter, fall back to substring-matching the party
   name against unlinked calendar entries' free text — the same approach
   `calendar-check` used against Outlook subjects, used here only as a fallback.
4. No match either way -> flagged as missing from Clio.

This is more reliable than `calendar-check`'s original text-only approach whenever staff
link a Matter on the calendar entry (which they usually do) — no free-text parsing needed
for the common case.

**Attorney/staff assignment:** read directly from the matched Clio calendar entry's
`calendar_owner`/`attendees` (their `name` field, as returned by Clio) — not a regex
search for a name in the summary text. Note: `calendar_owner`/`attendees` are Clio
Calendar/Attendee records, not User records, so there's no shared numeric ID to cross-
reference against `/users.json`; the name Clio reports is used as-is.

**Purpose code mappings:** `purpose_mappings` table in `data/clio_dashboard.db`, seeded
from `calendar-check`'s original mapping list. Edit via SQLite directly if a new hearing
type needs a code (no UI yet — matches this repo's "explicit mappings, edited directly"
philosophy rather than adding a settings page for something that changes rarely).

**Client court-date report:** `/calendar/client-list` (HTML preview) and
`/calendar/client-list/download` (Word doc) — one section per client with their upcoming
court dates plus **Responsible Attorney**, **Originating Attorney**, and **Responsible
Staff**, pulled from `data/clio-matters.csv` (those fields aren't exposed by the Clio API
itself, only the CSV export — keep it refreshed per Subproject 1's workflow).

**Explicitly out of scope:** the Timeslips "Billing Readiness" A/R matching feature from
`calendar-check` was dropped — Clio's own trust accounting replaces it; there's no A/R
CSV import or `client_case_mappings`-style conflict resolution here.

---

# Related Projects (legacy — do not duplicate)

| Project | Path | Status |
|---|---|---|
| rolodex | `~/projects/rolodex` | Complete — MyCase contact/case import done |
| clio2ts | `~/projects/clio2ts` | Active — Clio time → Timeslips TSImport (PS1) |
| clio-rate-import | `~/projects/clio-rate-import` | Complete — matter-level billing rates migrated |
