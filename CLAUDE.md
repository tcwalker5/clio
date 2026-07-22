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

### App permissions (Developer Portal) — not a freely-requestable OAuth scope string

Unlike Microsoft Graph (where you just list scopes in the authorize URL — see
`outlook_auth.py`), **Clio's permissions are checkboxes on the developer application
itself**, configured at Clio Settings > Developer Applications (or
`https://app.clio.com/settings/development`) for the app matching `CLIO_CLIENT_ID`.
The `scope` string passed in `clio_auth.py` can only grant a subset of whatever the
app was checked for there — adding a permission name to the code does nothing on its
own. To add a permission: check it (Read or Read/Write) for the app in the Developer
Portal, then re-run `uv run src/clio_auth.py` (full browser flow, not `--refresh` —
existing tokens don't retroactively pick up newly-granted permissions).

Full permission list (Clio's own descriptions, for reference — each is independently
Read/Write toggleable per app):

| Permission | Description |
|---|---|
| Activities | Activities, time entries, expenses, timers, UTBMS codes |
| Accounting | Bank account and bank transaction information |
| Api | API |
| Billing | Bills, billable clients, bill themes, line items |
| Calendars | Calendar entries and reminders you have edit permission on |
| Communications | Logged phone calls, emails, secure messages |
| Contacts | Clients, companies, external co-counsel — includes notes/log entries on contacts |
| Court rules | Court Rules. **Available on select plans only** |
| Custom fields | Custom fields recording extra info on contacts and matters |
| Documents | Documents and folders uploaded/created in Clio, including Document Templates |
| Imports | Imports from Activities, Calendars, Contacts, Matters, Notes, Tasks into the firm's account |
| General | General |
| Matters | Matters, including notes and practice areas |
| Payment distributions | Payment info on bills, trust payments, credit memos, allocations |
| Reporting | Reports generated in Clio |
| Settings | Setting preferences, including text snippets and bill settings |
| Tasks | Tasks, task lists, task types — priority, due date, reminder details |
| Users | User/group info with login ability (not Clio Connect users) |
| Webhooks | Any information a webhook has been created against |
| Custom actions | Create Custom Actions within Clio, scoped to actions this app created |
| Client share permissions | Client share permissions |
| Grants | Grants within Clio (Legal Aid US Services) |
| Personal injury | Medical Records Details, Medical Records, Medical Bills, Damages, Liens |
| Clio payments | Create payment links, access resulting payment details |

**This app currently has:** Matters, Activities (per the `matters activities` scope
that already works). **Confirmed missing:** Court Rules — `GET /court_rules/*`
returns `403 Forbidden` until it's checked in the Portal for this app (not currently
needed — see Subproject 6, cancelled).

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

# Subproject 5 — Outlook Calendar Migration

**Modules:** `src/outlook_auth.py`, `src/outlook_calendar/`, `src/outlook_migration.py`

**Purpose:** Migrate Heidi Collier's Outlook calendar into Clio — the calendar of
record since June 1, 2026 (`data/clio-matters.csv` and the Clio account both start
there). Started as a one-time backfill; now run on an ongoing basis (re-run
periodically with an updated `--to-date`, including future-scheduled events) since
Outlook is still where hearings/calls get entered day to day.

**How it writes to Clio:** it doesn't, directly. This script only *reads* Outlook
(Microsoft Graph) and Clio (matters + existing calendar entries, to skip anything
already migrated). All writing happens when the generated CSV is imported through
Clio's own UI (Settings > Data Import > Calendar Events) — chosen over the API
specifically because Clio's import can be undone from there if something's off; a
bulk API-created batch can't be undone that easily.

**Auth:** reuses `calendar-check`'s existing Azure app registration and its exact
registered redirect URI (`http://localhost:3020/api/auth/callback`) — no Azure Portal
changes needed, just a one-time browser consent:
```powershell
uv run src/outlook_auth.py             # first time (browser flow)
uv run src/outlook_auth.py --refresh   # token expired
```
Scope is `Calendars.Read` only — nothing here writes back to Outlook.

**Matching — two event types, two paths (`outlook_calendar/event_parser.py`):**
- **Hearings** (FRC, RFO, MSC, etc.): reuses `court_calendar/normalizer.py`'s
  `extract_party_name()`/`party_names_match()` (already built for Outlook-subject-style
  text) and the existing `purpose_mappings` table, same as the court calendar sync.
- **Calls** (`TCON`/`OCON`): a deliberately separate, simpler path per project
  decision — no purpose_mappings involved. Recognizes `TCON`, `OCON`, `TCN` (typo
  variant), and `T/C`/`T-C`/`T.C.` in two subject conventions: `"NAME TCON"` (party
  immediately precedes the marker) and `"Last, First- [staff] t/c w/CL"` (party
  precedes the first dash). **Bare `"OC"` is deliberately NOT treated as a call
  marker** — real-data check found it almost always means "Opposing Counsel"
  (e.g. `"SATTERLY OC's Responsive Dec due"`), not "office conference"; only the
  unambiguous forms above are recognized. Hearings are tried first; calls are a
  fallback only when no hearing-style match is found.
- Both paths resolve to a Clio matter via `matter_matching.index_by_last_name()` — the
  same shared lookup Subprojects 1/2/4 use. Unlike the court calendar sync, there's no
  case number here to disambiguate a client with two open matters — those go straight
  to the exceptions file.
- **Known limitation:** the underlying substring party-matching (shared with the rest
  of the project) can mismatch when one client's exact last name is a literal substring
  of another client's compound name — e.g. an event for "NIALEA ORTEGA" incorrectly
  matched client "ORTEGA, LAURA L." instead of "ORTEGA-GUACHENA, NIALEA". Rare, but a
  reason to actually read the CSV before importing, not just skim it.

**Skips Dahann's and Pam's events entirely**, via Outlook category color (not text
parsing) — `SKIP_CATEGORIES` at the top of `outlook_migration.py` maps `"Purple
category"` -> Dahann and `"Green category"` -> Pam, confirmed against real subject
text. Extend that dict if another color convention shows up.

**OP/CL — RFOs only.** Whether an event concerns the Opposing Party or our Client
can't be reliably derived from the Outlook data, and only matters for RFOs (it
indicates who's asking for the order) — every RFO title has a literal `OP/CL`
placeholder for a paralegal to resolve by hand (search/replace in Excel); every
other purpose omits that token entirely rather than carrying a meaningless one.

**Event title format:** `{client last[, First Initial]} {purpose} [{OP/CL}] {Time}`, e.g.
`WELLS RFO OP/CL 9:00 AM` (RFO) vs `LARSEN FRC 9:00 AM` (no OP/CL). Missing purpose
shows as `?` rather than being silently dropped. Client last name comes from the
matched Clio matter's own `display_number` (not whatever text was parsed off the
Outlook subject — that can attach the wrong first name to a shared last name, e.g.
a "BONNIE LARSEN" call actually matching client LARSEN, NOEL). Widened to `Last, F`
(first initial) only when another *open* matter shares that last name — otherwise
the bare last name is enough and matches the existing hearing-title convention.

**Department moved to the description line, not the title.** Department (courtroom)
doesn't apply to calls (TCON/OCON) at all, so a `?` placeholder in the title couldn't
distinguish "not parsed" from "not applicable." Instead, `build_export()` appends
`— {code}` to the description only when a department was actually found, normalized
to `LETTER-NN` (e.g. `N-19`, `D-10`) or a bare number for Central (`601`) via
`normalizer.normalize_dept()` — no "Dept" label, the code alone is self-explanatory
to staff. The raw `location` column is left as Outlook reported it (phone number for
calls, courtroom text for hearings) — unrelated to the normalized dept in the
description.

**OC/OP exception review (`outlook_calendar/relationships.py`):** matter_matching.py
only indexes Matters, so a TCON/OCON call with Opposing Counsel or an unrepresented
Opposing Party always lands in exceptions — Clio doesn't know that name as a *client*.
But Clio does already track both roles as Relationships (Contact <-> Matter, with a
free-text `description` like "Opposing Counsel"/"Opposing Party" — real taxonomy check
found ~200 of these at this firm, plus inconsistent variants like "Atty for Opposing
Party", matched with a regex, not an exact-string set). After writing the exceptions
file, the migration fetches `/relationships.json`, filters to OC/OP-flavored
descriptions, and cross-references each exception's party/subject text against those
contact names — one-directionally (contact name found IN the text, not the reverse):
the reverse direction produced two real false-positive classes in testing (short
leftover fragments from a failed party-name extraction trivially substring-matching
any long contact name; a bare client last name shared with the opposing party, e.g.
hearing "ROJAS FRC" falsely matching OP contact "ANEL ROJAS"). Runs across **all**
matter statuses, not just open — a closed matter can still have old Outlook calendar
history worth flagging (surfaced the firm's WELLS, BRITTNEY matter this way, invisible
to the regular open-matters-only client match). Report-only: nothing here is
auto-imported, and when a text matches more than one distinct (contact, matter) pair,
every candidate is listed in `note` rather than silently picking one.

**Calls report + interactive resolution (`outlook_calendar/calls_report.py`,
`interactive_resolve.py`, `call_overrides.py`):** every TCON/OCON call *and* DUE
deadline reminder (see below) the migration finds — regardless of outcome — also
gets written to its own review CSV (`_calls.csv`) so they can be visually compared
as one set instead of picked out by eye from the general exceptions file. Anything
still unresolved after the client-matter and OC/OP passes drops into an interactive
terminal prompt (runs automatically, not behind a flag): type part of a name to
search open matters, pick a number, or press Enter to skip. Events are grouped by
extracted party text first, so someone who called twice in the date range is only
asked about once. Every decision — matched *or* skipped — is persisted to
`data/outlook_call_overrides.csv` (gitignored, like the other `data/*.csv` inputs)
keyed by that party text **the instant it's made**, not batched until the whole
session finishes — Ctrl+C (or Ctrl+D/EOF) partway through only loses whatever
wasn't answered yet, not answers already given (verified: a KeyboardInterrupt or
EOFError mid-session is caught, doesn't crash the script, and lets the caller still
write every other output file for that run). Reloaded at the top of every future
run alongside `MANUAL_MATTER_MAP` — so a wider `--to-date` re-run (the normal
periodic workflow) doesn't re-prompt for someone already resolved, and a matched
party now also auto-resolves for *hearings*, not just calls, since it's merged into
the same lookup. Skipped entirely (with a warning, everything left unresolved) when
stdin isn't interactive — guarded by `isatty()` plus the same `EOFError` catch,
since `isatty()` alone wasn't reliable in every shell tested (returned `True` even
with stdin redirected from `/dev/null` in one test).

**DUE deadline reminders (purpose_code `"DUE"`, `event_parser.extract_due_event()`):**
a third fallback tier after hearings and calls — any subject containing "due" as a
whole word (e.g. `"WATERMAN OP'S RESPONSE TO DIVORCE DUE BY TODAY"`,
`"Metros docs due today"`). Party is read off the subject's leading word (there's no
purpose code to anchor on like hearings/calls have) — works for ~10 of 11 real
examples; the one miss was a name placed *after* "ON" instead of at the start
(`"RESPONSIVE DEC DUE ON PRECIADO"` extracted "RESPONSIVE") — that one correctly
falls through to the same manual-resolution prompt as any other unmatched event
rather than silently mismatching. **Always imported as an all-day event**
(`csv_export._is_all_day()`) regardless of what time Outlook recorded — a deadline
note has no meaningful clock time, and importing it at whatever time a reminder
happened to be set for would misrepresent it as the actual deadline time. Shares
the exact same review CSV, interactive prompt, and `call_overrides.py` persistence
as TCON/OCON calls (see above) — nothing deadline-specific there.

**Event type = Heidi:** Clio has a `calendar_entry_event_type` concept already in use
at this firm (types named "Heidi", "Dahann", "Pam", "Staff" — ids from
`/calendar_entry_event_types.json`, currently Heidi = `591618`). The CSV import has
no column for this, so it's a separate follow-up step, `src/outlook_migration_tag.py`
— run it *after* you've imported the CSV in Clio. It re-queries Clio for the same
matter+date and matches the summary text verbatim against your CSV to find the exact
entries it just created (not a "created recently" heuristic, which could tag someone
else's entry), then PATCHes each one's type to Heidi.

**Outputs:**
- `output/outlook_migration_{from}_{to}.csv` — ready to import into Clio as-is (after
  resolving `OP/CL` placeholders on RFOs)
- `output/outlook_migration_{from}_{to}_exceptions.csv` — unmatched/ambiguous events
- `output/outlook_migration_{from}_{to}_oc_op_candidates.csv` — exceptions that are
  likely calls with Opposing Counsel/Opposing Party rather than a missing client,
  matched against Clio's own Relationship contacts — review by hand, not auto-imported
- `output/outlook_migration_{from}_{to}_calls.csv` — every TCON/OCON call found, with
  its resolution (matched/OC/OP/skipped/unresolved) — for visual comparison
- `data/outlook_call_overrides.csv` — persisted manual call decisions (party -> matched
  matter or skipped), not date-stamped like the other outputs; grows across runs
- `output/outlook_migration_{from}_{to}_already_in_clio.csv` — matched events skipped
  because Clio already has an entry for that matter on that date (safe to re-run)
- `output/outlook_migration_{from}_{to}_tag_not_found.csv` — from the tagging step:
  CSV rows whose matching Clio entry couldn't be found (not actually imported yet, or
  the summary drifted on import — check by hand)
- `logs/outlook_migration_YYYYMMDD.log`, `logs/outlook_migration_tag_YYYYMMDD.log`

## Workflow
```powershell
# 1. Auth (if token expired)
uv run src/outlook_auth.py --refresh

# 2. Generate the CSV — always start from 2026-06-01 (Clio's start date);
#    push --to-date out far enough to catch future-scheduled events too
uv run src/outlook_migration.py --from-date 2026-06-01 --to-date 2028-06-01

# 3. Open the CSV, resolve every "OP/CL" placeholder (RFOs only), fix any names in
#    MANUAL_MATTER_MAP (top of outlook_migration.py) that showed up in the
#    exceptions file, then re-run if you changed the map. Read it, don't just
#    skim it — see the substring-matching limitation noted above.

# 4. Import output/outlook_migration_*.csv in Clio: Settings > Data Import > Calendar Events
#    (undo from there if something looks wrong)

# 5. Tag the entries you just imported with type = Heidi
uv run src/outlook_migration_tag.py --csv output/outlook_migration_2026-06-01_to_2028-06-01.csv

# 6. Import personal recurring series (dog pickup, workouts, birthdays, etc.) onto
#    Heidi's own calendar — always --dry-run first
uv run src/outlook_recurring_availability.py --from-date 2026-06-01 --to-date 2028-06-01 --dry-run
uv run src/outlook_recurring_availability.py --from-date 2026-06-01 --to-date 2028-06-01

# 7. Import everything left in the exceptions file onto Heidi's own calendar too —
#    same live-write safety rules, always --dry-run first
uv run src/outlook_exceptions_availability.py --from-date 2026-06-01 --to-date 2028-06-01 --dry-run
uv run src/outlook_exceptions_availability.py --from-date 2026-06-01 --to-date 2028-06-01
```

## Availability import — recurring series and exceptions (API writes)

Steps 6-7 above are the only scripts in this project that write to Clio directly
instead of generating a CSV for manual import — because Clio's CSV "Calendar
events" template has no way to target a specific calendar (no `calendar_owner`
column) or set recurrence (`recurrence_rule` only exists on the live API). Both
follow the project's standard live-write safety rules: `--dry-run` required, log
every request, retry on 429, continue on individual failures. Shared helpers
(Heidi's Calendar id, event type id, the retry/POST logic) live in
`outlook_calendar/clio_write.py`.

**`HEIDI_CALENDAR_ID = 8860113`** — Heidi's Clio *Calendar* id, **not** her
`USER_ID_HEIDI` env var. These are different Clio resources (see the Court
Calendar Sync note above: `calendar_owner`/`attendees` are Calendar/Attendee
records, no shared ID with `/users.json`). Found by inspecting an existing
Heidi-owned calendar entry's `calendar_owner.id` — if it ever needs
rediscovering, fetch any `calendar_entries.json` record you know is on her
calendar with `fields=calendar_owner{id,name}`.

**`outlook_recurring_availability.py`** — imports Heidi's PERSONAL recurring
Outlook series (dog pickup, workouts, Rotary, tax reminders, birthdays — not
client-related, but they do occupy real time on her calendar) as true recurring
`CalendarEntry` records. Real check of Heidi's calendar found 35 recurring
series, all personal — zero overlap with anything client-relevant — but the
script still runs `event_parser.looks_client_relevant()` as a guard and skips
(with a warning) anything that would look like a hearing/call/DUE, so a future
change in her calendar habits can't silently misfile a recurring client call
here instead of through the normal per-occurrence matching path.

Recurrence translation (`outlook_calendar/recurrence.py`) converts Microsoft
Graph's structured `recurrence.pattern` object into the RFC-5545 RRULE string
Clio's `recurrence_rule` field expects — confirmed against real data, not
guessed: a human created a real recurring entry in Clio's own UI and its
`recurrence_rule` was read back (`"FREQ=DAILY;WKST=SU"`), then a translated
`relativeMonthly` pattern ("third Thursday") was POSTed as a disposable test
entry, confirmed accepted, and deleted. Clio's own echo omits `INTERVAL=1` (the
default) — the translator matches that canonical form, which matters because
dedup compares a freshly generated RRULE string against what's already stored.
All 35 real series use `range.type: "noEnd"` (genuinely open-ended in Outlook)
— the translator only handles the pattern, not `UNTIL`/`COUNT`; extend it rather
than guess an untested format if a bounded series ever shows up. Dedup key is
`(subject, recurrence_rule)`, not subject alone — real data has two distinct
series both literally named "PAY PROPERTY TAXES BY THE 10TH" (different
installments, different schedules).

**`outlook_exceptions_availability.py`** — imports every remaining
`outlook_migration.py` exception (couldn't be matched to a client matter) as a
one-off `CalendarEntry` on Heidi's personal calendar. Deliberately
**unfiltered** by explicit decision (2026-07-15): this includes exceptions
where a party *was* extracted but didn't match a matter (might really be
unresolved client work) and genuinely private items — rather than guess which
is which, everything goes in as-is; a duplicate, matter-less copy on Heidi's
personal calendar is harmless if something's later resolved to a real matter.
Reuses `outlook_migration.gather_matched_events()` directly (the same function
`outlook_migration.py`'s own `main()` calls) instead of reimplementing the
fetch/match pipeline, so the two scripts can't define "exception" differently
and drift apart — this also means running this script triggers the same
interactive call-resolution prompts as a normal run for anything not yet
resolved (already-resolved parties from `data/outlook_call_overrides.csv` won't
re-prompt). Dedup key is `(subject, start_at)` — one-off entries, no
recurrence_rule to key on.

**`outlook_migration.py`'s own exceptions file excludes personal recurring
series** — `gather_matched_events()` fetches distinct recurring series
(`graph_client.fetch_recurring_series()`) and drops any occurrence belonging to
a series that isn't client-relevant, so a daily/weekly series' 100+ individually
-expanded occurrences don't flood the exceptions file; they're handled once
each by `outlook_recurring_availability.py` instead.

---

# Subproject 6 — Court Rules Automation (cancelled)

Was planned: auto-apply Clio Court Rules to matched court events (RFO, Trial, etc.) so
the deadline chain generates without a paralegal doing it by hand per matter. Blocked
on a `403 Forbidden` (Court Rules permission not granted to this app in the Clio
Developer Portal) and never unblocked. **Cancelled** — no longer planned.

---

# Subproject 7 — RingCentral Directory Sync

**Script:** `src/ringcentral_directory.py`, dashboard page at `/ringcentral`
(`src/web/routes_ringcentral.py`)

**Purpose:** Keep RingCentral's company phone directory in sync with Clio so office
phones can dial clients and opposing counsel/party by name. Replaces the legacy
`~/projects/rolodex` PowerShell pipeline (`clio-to-ringcentral.ps1`, itself a
successor to an even older Word-doc-scraping era) — this version pulls live from the
Clio API instead of manual CSV exports.

**RingCentral has no REST API for the company-wide directory** — confirmed via
RingCentral's own developer docs ("No REST APIs exist that allow you to add contacts
to your company's directory"). Only per-user *personal* contacts support API writes,
which isn't the shared directory office phones use. So this can never be a fully
unattended push — the deliverable is a ready-to-upload CSV, and a human uploads it at
RingCentral's own admin page:
```
https://service.ringcentral.com/application/admin/tools/externalSharedContactsDirectory
```
No RingCentral credentials are needed in `.env` — this subproject makes zero calls to
RingCentral's API, only Clio reads plus a local browser-open.

**Scope:** every Clio contact that's the default client on an open matter, or tagged
Opposing Counsel/Opposing Party (via `/relationships.json`, reusing
`outlook_calendar/relationships.py`'s `fetch_oc_op_contacts()`) on an open matter.
Matter→client linkage uses `matter_matching.fetch_open_matters()` with its `fields`
param extended to include `client{id}` (the same nested-field-selection syntax
`relationships.py` already used) — a small additive change, not a new pattern.

**Contact field shapes** (confirmed live against this account, not guessed): a plain
`fields=phone_numbers` only returns `{id, etag}` stubs — needs explicit subfield
selection, `phone_numbers{name,number,default_number}`. `type` (`"Person"`/`"Company"`)
and `company{id,name}` both populate correctly and are used instead of the
email-domain firm-guessing rolodex needed — Clio structurally tells you Person vs.
Company already.

**Phone dedup:** RingCentral requires unique phone numbers per directory row — and
this is a hard, global constraint, not per-field. **Confirmed live** (2026-07-21,
2-row test upload): putting the same number in two different contacts' rows, even
in different columns (one in Mobile Number, the other in Business Number), gets the
second row rejected outright with `Object with desired [+1...] value exists.` So
there's no way to give two people sharing a phone their own separate directory
entries — every shared-phone group has to become exactly one row.

A phone shared by exactly one contact goes straight through (Person → Mobile
Number, Company → Company Main Number). Contacts sharing a phone that are *all*
linked to the same Clio Company, or that agree on one non-personal email domain
(`_resolve_by_email_domain()` — real-data check found Clio's `company` relationship
is almost never actually set on Opposing Counsel/Party contacts even when they're
obviously colleagues at the same firm, but their email domain reliably is; contacts
with no email don't block the match), collapse into a single row. **That merged
row's Last Name is every individual's full name, comma-separated** (`_merged_last_name()`,
e.g. `"DANIEL C. HERBERT, NICOLE MARTINEZ"`), with the firm name in Company for
context — not the other way around. This is deliberate: RingCentral's directory
search only covers First/Last Name, not Company or Job Title (confirmed via
RingCentral's own support docs/community reports), so putting only the firm name in
Last Name would make individual attorneys unfindable by name even though their
number is technically in the directory. Anything still unresolved checks
`data/ringcentral_phone_knowledge_base.csv` (manually-resolved canonical
name/company per phone — empty template created on first run, not seeded from
rolodex's old data since that existed to work around messy Word-doc-era conflicts
that don't apply to clean API data) before falling through to
`output/ringcentral_conflicts_{date}.csv` for manual review. In practice this chain
resolved all 15 real conflicts found on this account's first run down to 0.

**Change detection:** RingCentral's import isn't a literal wipe-and-recreate — it
reconciles by matching key (the `External ID` column, set to the Clio contact ID)
and only touches what actually differs: new rows get added, rows with no match in
the upload get deleted, and byte-identical matches are left alone. **Confirmed live**
(first real upload, 2026-07-22): RingCentral reported `321 new`, `26 unchanged`,
`275 deleted` — `26 + 321 = 347` (the exact row count of the uploaded CSV), and
`26 + 275 = 301` (the directory's prior size, mostly leftover entries from the old
`rolodex` pipeline manually run in the past, which used this same
Clio-contact-ID-as-External-ID convention — that's why 26 matched exactly). Net
effect is still a full replace in terms of end state — the directory always ends up
equal to the uploaded CSV — so re-uploading an unchanged file is still pure noise.
Each run computes a hash of the built directory and stores it in
`ringcentral_sync_runs` (in the shared `data/clio_dashboard.db`) alongside the
previous run's hash — `changed` is true only when something actually differs
(contact added/removed, phone changed, etc.).

**CLI behavior:** `uv run src/ringcentral_directory.py` builds the CSV, and — only
when `changed` is true — opens the RingCentral import page in the default browser
(`--no-open` suppresses this). This is what the daily Windows Scheduled Task runs;
on a no-op day it's silent.

**Dashboard behavior:** `/ringcentral` is read-only status (last sync time, row/
conflict counts, changed flag) backed by `ringcentral_sync_runs` — no live Clio call
just to view the page. "Sync now" runs the pipeline synchronously; unlike
Bradford/Printer there's no separate confirm step, since this never writes to Clio or
RingCentral. The dashboard never calls `webbrowser.open()` itself (it's also reachable
over Tailscale from other devices) — it shows the import-page link instead.

**Outputs:**
- `output/ringcentral_directory_{date}.csv` — ready to upload (RingCentral's own
  documented column order + instruction-header preamble)
- `output/ringcentral_conflicts_{date}.csv` — unresolved phone conflicts (only
  written if non-empty)
- `logs/ringcentral_directory_YYYYMMDD.log`
- `data/ringcentral_phone_knowledge_base.csv` — persisted manual conflict resolutions

## Workflow
```powershell
# Manual run (safe to run any time — never writes to Clio or RingCentral)
uv run src/ringcentral_directory.py

# Build only, never pop a browser (e.g. for testing)
uv run src/ringcentral_directory.py --no-open
```

**Daily automation:** `sync-ringcentral.bat` (repo root) wraps the manual run above,
intended to run once a day via a Windows Scheduled Task on the same machine that runs
the dashboard:
```powershell
schtasks /create /tn "Clio RingCentral Sync" /tr "C:\Users\TEDMINI\projects\clio\sync-ringcentral.bat" /sc daily /st 07:00
```
Registering this is a deliberate, one-time manual step (persistent OS-level
automation is confirmed with the user before being created, not silently set up).

**Permission note:** `/contacts.json` wasn't exercised anywhere in this repo before
this subproject and there was some doubt it might need a separate **Contacts**
permission checked in the Clio Developer Portal (this app's confirmed-granted scopes
were previously just Matters + Activities). Tested live during development —
`/contacts.json` returned `200` with no permission changes needed, so Contacts reads
already work under the existing token. Worth remembering if a *write* to Contacts is
ever needed later (untested) — Clio's Read and Write permissions are independently
toggleable per the "App permissions" table above.

---

# Related Projects (legacy — do not duplicate)

| Project | Path | Status |
|---|---|---|
| rolodex | `~/projects/rolodex` | Complete — MyCase contact/case import done |
| clio2ts | `~/projects/clio2ts` | Active — Clio time → Timeslips TSImport (PS1) |
| clio-rate-import | `~/projects/clio-rate-import` | Complete — matter-level billing rates migrated |
