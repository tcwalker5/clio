# Clio Project

Central Python monorepo for all Clio Manage API interactions at Collier Law.

Replaces scattered PowerShell scripts from rolodex, clio2ts, and clio-rate-import.
No .ps1 scripts — Python only.

---

## Before you touch subproject code

This file is a hub — it has environment setup, API auth basics, safety rules, and the
project map. Subproject detail (workflows, exception handling, design decisions,
gotchas) lives in per-module docs, **not here**. Before modifying or debugging any
subproject's code, read its doc first — see "Where the details live" below. Five
modules live in their own `src/` subfolder and get a real `CLAUDE.md` Claude Code
auto-loads when you read/edit files there. Everything else is a flat script directly in
`src/` with no subfolder to anchor that — for those, open the linked `reference/*.md`
file yourself; nothing loads it automatically.

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
CAP_BASE_URL                # optional — LAN URL used in links posted back to Clio (Equalizer's
                             # matter Note); defaults to http://cap.lan:8421, same host as
                             # "CAP Dashboard.url"

# Outlook Calendar Migration only — see src/outlook_calendar/CLAUDE.md
MICROSOFT_CLIENT_ID
MICROSOFT_CLIENT_SECRET
MICROSOFT_TENANT_ID
MICROSOFT_REDIRECT_URI              # http://localhost:3020/api/auth/callback (calendar-check's registered URI)
MICROSOFT_ACCESS_TOKEN
MICROSOFT_REFRESH_TOKEN
MICROSOFT_CALENDAR_OWNER_EMAIL      # whose Outlook calendar to read (Heidi)
```

**Corrected 2026-08-01 — this block was out of date on two counts:**
1. It was missing the entire `MICROSOFT_*` group (added above) that
   `outlook_auth.py`/`outlook_calendar/graph_client.py` actually require — anyone
   setting up Outlook Calendar Migration from a plain `.env.example` copy would have
   had no idea these were needed.
2. Of the `USER_ID_*` vars, **only `USER_ID_PAM` is actually read by any script today**
   (`bradford_invoice.py`'s `PAM_USER_ID`) — a prior version of this note claimed "two
   scripts" used one of these each, but a code check found just the one. The other five
   (`USER_ID_HEIDI`/`SANDY`/`DALINAH`/`TED`/`DAHANN`) aren't read anywhere in `src/` —
   `clio_users.py`'s own docstring says it was written specifically to replace
   hardcoded per-user env vars like these. They're kept in `.env`/`.env.example` as
   vestigial, not active config; harmless to leave, safe to prune if this file is ever
   cleaned up.

Staff shown in the dashboard (court calendar attorney/staff assignment, matter lookups)
come live from Clio's `/users.json` via `src/clio_users.py`, not from any `USER_ID_*` env
var. The live directory is what stays current as staff change, e.g. paralegals added
after this file was last edited.

---

# Project Structure

```
clio/
├── data/               # Input files — CSV reports, matter exports, invoice PDFs
├── output/             # Generated payloads and exception reports
├── logs/               # Per-run API logs
├── reference/           # openapi.json + the flat-script doc groups (not auto-loaded)
├── src/
│   ├── clio_auth.py              # OAuth token management (shared)
│   ├── matter_matching.py        # Shared name -> Clio matter ID lookup (all subprojects)
│   ├── clio_users.py             # Clio staff directory (/users.json), cached to SQLite
│   ├── printer_expenses.py       # Printer Expenses               → reference/printer-papercut.md
│   ├── generate_papercut_accounts.py  # PaperCut Shared Account Sync → reference/printer-papercut.md
│   ├── papercut_balance_migration.py  # PaperCut Shared Account Sync → reference/printer-papercut.md
│   ├── bradford_invoice.py       # Bradford Invoice Import        → reference/invoices.md
│   ├── legs_expenses.py          # Legs Expenses (OCR'd statement PDF -> ExpenseEntry) → reference/invoices.md
│   ├── ringcentral_directory.py  # RingCentral Directory Sync     → reference/ringcentral.md
│   ├── trust_monitor.py          # Trust Monitor & Replenishment Requests → reference/billing-monitors.md
│   ├── collections_monitor.py    # Collections — unpaid, already-issued bills (read-only) → reference/billing-monitors.md
│   ├── collections_flarpl.py     # Collections helper             → reference/billing-monitors.md
│   ├── collections_payment_plan.py  # Collections helper          → reference/billing-monitors.md
│   ├── staff_unbilled_monitor.py # Staff Unbilled Report — unbilled activity by staff member (read-only) → reference/billing-monitors.md
│   ├── client_assignment.py      # Client Assignment — Responsible/Originating Attorney, Responsible Staff → reference/client-assignment.md
│   ├── outlook_auth.py           # Outlook Calendar Migration (OAuth) → src/outlook_calendar/CLAUDE.md
│   ├── outlook_migration.py      # Outlook Calendar Migration     → src/outlook_calendar/CLAUDE.md
│   ├── outlook_migration_tag.py  # Outlook Calendar Migration     → src/outlook_calendar/CLAUDE.md
│   ├── outlook_exceptions_availability.py  # Outlook Calendar Migration → src/outlook_calendar/CLAUDE.md
│   ├── outlook_recurring_availability.py   # Outlook Calendar Migration → src/outlook_calendar/CLAUDE.md
│   ├── equalizer/                # Equalizer — asset/debt division worksheets → src/equalizer/CLAUDE.md
│   │   ├── calc.py               #   equity, after-tax, 50/50 equalization math
│   │   ├── store.py              #   SQLite persistence for worksheets/items (owns its own schema fragment)
│   │   ├── clio_parties.py       #   default party names from the matter's client + OP contact
│   │   ├── pdf.py                #   renders a worksheet to PDF (reportlab)
│   │   ├── clio_documents.py     #   uploads the finalized PDF to the matter's Evidence folder
│   │   └── clio_notes.py         #   posts a matter Note linking back to the live worksheet
│   ├── moore_marsden/            # Moore/Marsden Calculator — community-interest calculation → src/moore_marsden/CLAUDE.md
│   │   ├── calc.py               #   recursive multi-refinance formula + capital improvements
│   │   ├── store.py              #   SQLite persistence for worksheets/segments/improvements (owns its own schema fragment)
│   │   ├── clio_parties.py       #   default owner/non-owner labels from the matter's client + OP contact
│   │   ├── clio_matter_dates.py  #   reads/writes the matter's Date of Marriage/Separation custom fields
│   │   ├── pdf.py                #   renders a worksheet to PDF (reportlab)
│   │   ├── clio_documents.py     #   uploads the saved PDF to the matter's Evidence folder
│   │   └── clio_notes.py         #   posts a matter Note linking back to the live worksheet
│   ├── court_calendar/           # Court Calendar Sync → src/court_calendar/CLAUDE.md
│   │   ├── normalizer.py         #   court-text parsing, dept/purpose normalization
│   │   ├── court_fetch.py        #   one-click scrape of the SD Superior Court site
│   │   ├── clio_calendar.py      #   /calendar_entries.json client
│   │   ├── matcher.py            #   matter-ID-first + text-fallback diff
│   │   ├── matter_fields.py      #   live Responsible/Originating Attorney, Court Case #
│   │   ├── clio_matter_update.py #   the one write path — pushes Court Case Number to Clio
│   │   ├── store.py              #   SQLite persistence for parsed court events
│   │   └── client_list.py        #   client court-date report (HTML + Word)
│   ├── outlook_calendar/         # Outlook Calendar Migration support code → src/outlook_calendar/CLAUDE.md
│   │   ├── graph_client.py       #   Microsoft Graph API client
│   │   ├── event_parser.py       #   raw Graph event -> normalized event
│   │   ├── recurrence.py         #   recurring-series expansion
│   │   ├── relationships.py      #   matter/contact matching for calendar events
│   │   ├── csv_export.py         #   Clio calendar-import CSV writer
│   │   ├── interactive_resolve.py #  human-in-the-loop exception resolution
│   │   ├── call_overrides.py     #   manual per-event overrides
│   │   └── calls_report.py       #   call-log reporting
│   └── web/                      # Dashboard — FastAPI app wrapping all subprojects → src/web/CLAUDE.md
│       ├── app.py                #   routes, dashboard home
│       ├── auth.py               #   shared-passphrase login, signed session cookie
│       ├── db.py                 #   SQLite schema (data/clio_dashboard.db)
│       ├── preview_store.py      #   in-memory dry-run-preview -> confirm-and-post handoff
│       ├── routes_*.py           #   one router per app (bradford/printer/calendar/legs/
│       │                         #   ringcentral/papercut/trust/collections/staff_unbilled/
│       │                         #   client_assignment/equalizer/moore_marsden)
│       ├── templates/            #   Jinja2 templates
│       └── static/                #   CSS + drag-and-drop JS
├── start-dashboard.bat            # Launches the dashboard (uv sync + uvicorn)
├── start-dashboard-silent.vbs     # Same, without a visible console window
├── CAP Dashboard.url              # Desktop shortcut to http://cap.lan:8421/, for staff
├── .env                # Credentials (gitignored)
├── .env.example
├── .gitignore
├── pyproject.toml
└── CLAUDE.md
```

**Gap closed 2026-08-12:** `data/` used to be covered only by extension-specific
`.gitignore` rules (`*.csv`, `*.pdf`, `*.txt`, `*.db`), which missed anything with a
different extension dropped in there — confirmed for real during a commit that found a
`.JPG` and two `.png` screenshots sitting alongside real client billing PDFs/CSVs/the
dashboard DB, none of them csv/pdf/txt/db. `.gitignore` now has a blanket `data/` entry
instead, so nothing in this directory can be added by accident regardless of extension.

Each subproject script has its own `main()` and can be run directly via `uv run src/<script>.py`.
The web dashboard (`src/web/app.py`) wraps every other subproject with a browser UI — see
`src/web/CLAUDE.md`. It does not replace the CLI entry points, which still work standalone.

---

## Where the details live

| Subproject | Path(s) | Doc | Auto-loaded? |
|---|---|---|---|
| Web Dashboard (CAP) | `src/web/` | `src/web/CLAUDE.md` | Yes |
| Court Calendar Sync | `src/court_calendar/` | `src/court_calendar/CLAUDE.md` | Yes |
| Outlook Calendar Migration | `src/outlook_calendar/`, `src/outlook_*.py` | `src/outlook_calendar/CLAUDE.md` | Yes for the folder; open manually for the flat `outlook_*.py` scripts |
| Equalizer | `src/equalizer/` | `src/equalizer/CLAUDE.md` | Yes |
| Moore/Marsden Calculator | `src/moore_marsden/` | `src/moore_marsden/CLAUDE.md` | Yes |
| Bradford Invoice Import, Legs Expenses | `src/bradford_invoice.py`, `src/legs_expenses.py` | `reference/invoices.md` | No — open manually |
| Printer Expenses, PaperCut Shared Account Sync | `src/printer_expenses.py`, `src/generate_papercut_accounts.py`, `src/papercut_balance_migration.py` | `reference/printer-papercut.md` | No — open manually |
| RingCentral Directory Sync | `src/ringcentral_directory.py` | `reference/ringcentral.md` | No — open manually |
| Trust Monitor, Collections, Staff Unbilled Report | `src/trust_monitor.py`, `src/collections_*.py`, `src/staff_unbilled_monitor.py` | `reference/billing-monitors.md` | No — open manually |
| Client Assignment | `src/client_assignment.py` | `reference/client-assignment.md` | No — open manually |
| Clio API — full permission table, pagination gotcha | — | `reference/clio-api.md` | No — open manually, occasional reference |

Court Rules Automation is cancelled — see `src/outlook_calendar/CLAUDE.md` for why (it's
grouped there for historical reasons, not because it's related to Outlook).

---

# Clio API

## Reference
OpenAPI spec: `reference/openapi.json` — lives in **this** project (copied
2026-08-14 from the now-retired `clio-rate-import`, which should never be
read or referenced directly again — Ted has corrected this multiple times).
Use it only as a first pass to discover which resources/fields/endpoints
exist at all — never as the final answer for actual behavior. This spec has
repeatedly been wrong or incomplete about real behavior (nested fields
returning stubs unless explicitly requested via `fields=`, presigned URLs
requiring specific headers it doesn't mention, etc. — see `src/equalizer/CLAUDE.md`'s
Document upload notes for real examples). Always confirm shapes
and behavior live against the actual Clio account before relying on them.
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

Full permission list, current grants, and the pagination gotcha: see `reference/clio-api.md`.

## Safety rules (apply to ALL scripts)
- Dry-run mode required — `--dry-run` flag must work before any live run
- Log every request: timestamp, matter ID, payload, response status
- Retry on rate limit (429)
- Continue on failure — log error, move to next item
- Never silently overwrite existing data

## Designated test matter — use this, not an arbitrary real one
**DOE, JANE** (matter id `1784289301`, client "Jane Doe," status Pending) is
the standing target for any live testing against a real matter — creating
worksheets, uploading documents, posting notes, anything that needs a real
`matter_id` to exercise. Added 2026-08-14 after a real near-miss: earlier
Equalizer testing picked "whatever matter comes next alphabetically" and
landed on VINEY, MARY ANNE — a matter Ted was actively, genuinely using at
that exact moment. A test step nearly overwrote real client data before it
was caught via timestamp forensics and reverted. **Use DOE, JANE for any
future live-Clio testing across this whole project, not just Equalizer** —
picking "the next matter alphabetically" or any other real client's matter
as an improvised test target is exactly the pattern that caused the
near-miss. This matter needs its own **Evidence** folder created in Clio
before it can be used for a document-upload test specifically (as of
2026-08-14 it doesn't have one yet — verify live, don't assume, before
relying on that step).

---

# Development Philosophy

1. Auditability — every transformation inspectable from CSV/log output
2. Fail loud — hard errors on unmapped values, never silently skip
3. Explicit mappings — editable constants near top of each file
4. Rerunnable — safe to run multiple times
5. Readable over clever

---

# Related Projects (legacy — do not duplicate)

| Project | Path | Status |
|---|---|---|
| rolodex | `~/projects/rolodex` | Complete — MyCase contact/case import done |
| clio2ts | `~/projects/clio2ts` | Active — Clio time → Timeslips TSImport (PS1) |
| clio-rate-import | `~/projects/clio-rate-import` | Complete — matter-level billing rates migrated |
