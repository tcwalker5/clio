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

# Outlook Calendar Migration only — see that section below
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
├── src/
│   ├── clio_auth.py              # OAuth token management (shared)
│   ├── matter_matching.py        # Shared name -> Clio matter ID lookup (all subprojects)
│   ├── clio_users.py             # Clio staff directory (/users.json), cached to SQLite
│   ├── printer_expenses.py       # Printer Expenses
│   ├── bradford_invoice.py       # Bradford Invoice Import
│   ├── legs_expenses.py          # Legs Expenses (OCR'd statement PDF -> ExpenseEntry)
│   ├── court_calendar/           # Court Calendar Sync
│   │   ├── normalizer.py         #   court-text parsing, dept/purpose normalization
│   │   ├── court_fetch.py        #   one-click scrape of the SD Superior Court site
│   │   ├── clio_calendar.py      #   /calendar_entries.json client
│   │   ├── matcher.py            #   matter-ID-first + text-fallback diff
│   │   ├── matter_fields.py      #   live Responsible/Originating Attorney, Court Case #
│   │   ├── clio_matter_update.py #   the one write path — pushes Court Case Number to Clio
│   │   ├── store.py              #   SQLite persistence for parsed court events
│   │   └── client_list.py        #   client court-date report (HTML + Word)
│   └── web/                      # Dashboard — FastAPI app wrapping all subprojects
│       ├── app.py                #   routes, dashboard home
│       ├── auth.py               #   shared-passphrase login, signed session cookie
│       ├── db.py                 #   SQLite schema (data/clio_dashboard.db)
│       ├── preview_store.py      #   in-memory dry-run-preview -> confirm-and-post handoff
│       ├── routes_*.py           #   one router per app (bradford/printer/calendar/legs/
│       │                         #   ringcentral/trust)
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

**Known gap, corrected 2026-08-01:** the prior version of this note claimed `data/` had
*no* `.gitignore` entry at all and stayed out of git purely by nobody running
`git add -A`/`git add .` — that was wrong (or has since been fixed elsewhere and not
updated here). A root `.gitignore` already exists and covers `*.csv`, `*.pdf`, `*.txt`,
`*.db` — which catches every file actually in `data/` today except one stray
screenshot (`.png`). The real residual gap is narrower: any file dropped into `data/`
with an extension not on that list (a screenshot, a `.docx`, anything) would still slip
through `git add -A` uncaught. An explicit blanket `data/` ignore rule (with narrow
`!data/.gitkeep`-style exceptions if anything in there ever needs to be tracked) would
close that gap for good instead of relying on the extension list staying exhaustive.

Each subproject script has its own `main()` and can be run directly via `uv run src/<script>.py`.
The web dashboard (`src/web/app.py`) wraps Printer Expenses, Bradford Invoice Import, Legs
Expenses, and Court Calendar Sync with a browser UI — see "Web Dashboard" below. It does not
replace the CLI entry points, which still work standalone.

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

**This app currently has:** Matters, Activities (the original `matters activities`
scope), plus **Billing (Read)** — granted 2026-07-22 for Bradford's live matter-rate
display, see Bradford Invoice Import below — and **Accounting** — granted 2026-07-30
for Trust Monitor's `account_balances` fetch, see Trust Monitor & Replenishment
Requests below. (A prior version of this note listed only Matters/Activities and
wasn't updated as those two were added.) **Confirmed missing:** Court Rules —
`GET /court_rules/*` returns `403 Forbidden` until it's checked in the Portal for this
app (not currently needed — see Court Rules Automation, cancelled).

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

**Branding:** rebranded 2026-07-29 from "Clio Dashboard" to **Collier Automation
Platform (CAP)** — dark ink/brass visual identity, channel-grid home page (each
subproject shown as a "channel" with its data flow, e.g. `PDF -> Clio`). This is
step one of the bigger "CAP" vision described later in this doc (see "CAP — Collier
Automation Platform") — this dashboard *is* the platform, not a separate thing that
happens to share its name; what that section still describes as unbuilt is the
automated Windows Service + Scheduler layer on top of it.

**Purpose:** Browser UI for the whole repo — a home page linking to drag-and-drop
versions of Printer Expenses, Bradford Invoice Import, and Legs Expenses, plus Court
Calendar Sync, RingCentral Directory Sync, and Trust Monitor & Replenishment
Requests. Wraps each script's existing `run_pipeline()` function; does not duplicate
matching/posting logic.

**Run:**
```powershell
uv run uvicorn web.app:app --app-dir src --host 0.0.0.0 --port 8421
# or just double-click start-dashboard.bat (syncs deps, then starts)
```
`--app-dir src` puts `src/` on `sys.path`, matching how the standalone scripts resolve
flat imports (`import matter_matching`, etc.) — same convention, one process.

**Concurrency:** `start-dashboard.bat` launches uvicorn with no `--workers` — one process,
one event loop, serving every LAN user. Fixed 2026-08-03: the drag-and-drop
preview/confirm routes (Legs, Printer, Bradford) and the live-pipeline routes (Trust,
RingCentral) originally called their subproject's `run_pipeline()` directly inside an
`async def` handler — a fully synchronous, blocking call (Legs' page-by-page Tesseract
OCR and everyone else's live `requests`-based Clio fetches aren't asyncio-aware) sitting
on the one event loop thread. Confirmed live: with Legs OCR running, a *second browser
tab* loading an unrelated page (e.g. `/calendar`) hung until the OCR finished — one
person's upload froze the dashboard for the whole office, not just their own request.
Every such call is now wrapped in `fastapi.concurrency.run_in_threadpool(...)` so it
runs on a worker thread instead of the event loop. `routes_trust.py`'s `_load_home` had
to become `async def` for this, since it's the shared re-render path after every
mutating action. Deliberately NOT wrapped: `routes_trust.py`'s `_send_one()` (the
Clio-POST that actually creates a TrustRequest) — it shares a `sqlite3.Connection`
created on the request thread, and `web/db.py`'s `get_connection()` doesn't set
`check_same_thread=False`, so handing that connection to a threadpool worker would raise
`ProgrammingError` rather than fix anything. Its blocking window is small (a handful of
selected matters per send, not a few-hundred-matter fetch) so it was left as future work
rather than papered over. Adding uvicorn `--workers` instead of threadpooling was
considered and rejected — the dashboard's dry-run-preview -> confirm handoff
(`preview_store.PREVIEWS`) is an in-memory dict, not shared across separate worker
processes, so multiple workers would break "Confirm & Post" whenever the confirm
request landed on a different worker than the preview did.

**Auth:** Single shared passphrase (`CLIO_DASHBOARD_PASSPHRASE` in `.env`), not per-staff
login — the Clio side already uses one shared app registration. Session is a signed cookie
(`CLIO_DASHBOARD_SECRET`). This is LAN-only gatekeeping, not intended as a public-internet
login system.

**Storage:** SQLite at `data/clio_dashboard.db` — court events, purpose mappings,
staff cache, RingCentral sync run history, trust request settings/lifecycle. No
external database service. **Not actually gitignored yet** — see the `data/`
"Known gap" note under Project Structure; this file now also holds trust-request
history, which raises the stakes on finally closing that gap.

**Drag-and-drop apps (Bradford Invoice, Printer Expenses, Legs Expenses):** upload a file -> dry-run
preview (payloads + exceptions, same categories as the CLI) -> "Confirm & Post" button ->
live run. Mirrors the CLI's `--dry-run` workflow; the confirm step is the only path that
posts to Clio.

**Remote access off the office LAN:** install [Tailscale](https://tailscale.com) on the
machine running the dashboard and on any device that needs to reach it — no port
forwarding or public-facing server required. The dashboard itself only ever binds to the
LAN/Tailscale interface, never a public one.

**On-LAN access:** `CAP Dashboard.url` (repo root, added 2026-07-28) is an internet
shortcut pointed at `http://cap.lan:8421/` — meant to be copied to individual staff
desktops or emailed as an attachment so non-technical staff double-click instead of
typing an IP and port. `cap.lan` is a local DNS/hosts-file name resolved on the office
LAN (not something this repo configures or documents further — it's network-side setup,
same category as the Windows Scheduled Task registration for RingCentral sync).

**New `.env` keys:**
```
CLIO_DASHBOARD_SECRET       # session cookie signing key
CLIO_DASHBOARD_PASSPHRASE   # shared login passphrase
```

---

# Printer Expenses

**Script:** `src/printer_expenses.py`

**Purpose:** Convert monthly Papercut print/copy/scan report to Clio ExpenseEntry API calls.

**Input:** `data/print_copy_summary_by_account.csv` (Papercut export)

**Matter lookup:** live Clio API (`matter_matching.fetch_open_matters()` +
`index_by_display_name()`) at run time — no matters export file to keep current.

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
    "COLTON": 1234567890,  # Clio matter ID — look up display_number in Clio
}
```

## Workflow
```powershell
# 1. Drop new Papercut export into data/

# 2. Dry run — check output/exceptions_YYYY-MM.csv for unmatched names
uv run src/printer_expenses.py --dry-run

# 3. Fix exceptions: add matter IDs to MANUAL_MATTER_MAP, re-run dry-run

# 4. Live run
uv run src/printer_expenses.py
```

## Exception types
- **No matching open matter** — client name not found among live open Clio matters
- **Ambiguous** — multiple open matters for same display name; add to MANUAL_MATTER_MAP
- **Joint client** — name contains " & "; split across matters manually

---

# Bradford Invoice Import

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

**User:** All entries posted under `USER_ID_PAM` (359115091). The dashboard's Matched/
Posted entries table shows a **Posted by** column (`PAM_INITIALS = "PB"`, a display-only
constant) so this isn't just implicit — it's the same for every row today since Bradford
always posts under Pam, but visible rather than assumed.

**Rate display (dashboard-only, never sent to Clio):** attorney entries still omit
`price` from the real POST payload as above — but `/bradford` shows Pam's *actual*
matter-defined rate next to each row instead of a vague "matter rate" placeholder,
fetched via `custom_rate{type,rates}` on the same `fetch_open_matters()` call
(`index_pam_rate_by_matter_id()`). This needs the Clio app's **Billing (Read)**
permission — granted 2026-07-22, confirmed working: without it, `custom_rate` comes
back as `{"redacted": true}` rather than a 403, so it fails quietly unless you know to
check for that shape specifically. A shallow field selector (`custom_rate{type,rates}`)
already returns each rate entry fully expanded (`rate`, `user{id,name}`) — a deeper
selector like `rates{rate,user}` is rejected as invalid, don't try to nest further.
Not every matter has a rate on file for Pam specifically (real example: `FULMER`,
`LASHGARI`, `SWEET` on this account had rates for other staff but not her) — for those,
`fetch_pam_standard_rate()` falls back to her standard rate (Clio's `User.rate` field,
confirmed live 2026-07-23 against known figures — Ted $150, Dalinah $200, Sandy $300,
Pam $450, all matching the firm's existing rate history). The firm is moving toward a
standard-rate system rather than maintaining custom per-matter rates going forward, so
this fallback is the expected common case now, not just a display curiosity for edge
cases — "rate unknown" only shows if even the standard-rate fetch itself fails.
`payload["data"]` is the only thing ever POSTed to Clio (`post_entry` sends
`{"data": payload["data"]}` explicitly) — `display_rate`/`posted_by` are sibling keys
on the payload dict, present in the local JSON output for audit purposes but never
part of the API call.

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
- **No matching open matter** — client last name not in Clio open matters. The
  exceptions CSV includes a `suggested_match`/`suggested_matter_id` column
  (`difflib.get_close_matches()` against all open-matter last names, cutoff 0.6) —
  Bradford's invoices frequently misspell/truncate names (real examples:
  "LASHGHARI" → LASHGARI, "BULTIMIER" → BULTEMEIER, the same client already in
  `MANUAL_MATTER_MAP` under yet another misspelling, "BULTMIERE" — the contractor
  isn't consistent invoice to invoice). This is a suggestion only, never
  auto-applied — still fails loud per this project's philosophy; a misrouted
  billing entry is a real problem, so a human confirms it explicitly.
- **Ambiguous** — multiple open matters share the same last name; add to MANUAL_MATTER_MAP
- **Closed matter** — client matter closed in Clio; redirect to active matter via MANUAL_MATTER_MAP

## Resolving exceptions from the dashboard
`/bradford`'s exceptions table has, per row: a one-click **"Use suggested match"**
button (only shown when `suggested_matter_id` is populated) and a free-text matter-ID
field for anything else (wrong suggestion, ambiguous case, no suggestion at all).
Either one POSTs to `/bradford/resolve-exception`, which appends to
`data/bradford_manual_matter_map.csv` (`name,matter_id,note,added_at` — created with
a header on first write, BOM only on that first write since re-opening in append
mode with `utf-8-sig` would otherwise inject a fresh BOM into the middle of the file
on every save) and re-runs the dry-run preview in place, so the resolved entry moves
from the exceptions table into the matched-entries table immediately, same token,
same "Confirm & Post" button at the end — no separate "final import" button needed,
since Confirm & Post already is that step once exceptions are cleared.

**Why a data file instead of editing `MANUAL_MATTER_MAP` directly:** the code
constant is for deliberate, reviewed, permanent overrides (committed to git); the
dashboard flow is a paralegal resolving something mid-import without touching Python
source. `effective_manual_matter_map()` merges both at run time (loaded fresh every
`run_pipeline()` call), code constant winning on conflict. Persisted overrides apply
to *future* invoice imports too, not just the one being resolved — same
`load_persisted_matter_map()` call whether triggered from the CLI or the dashboard.

---

# Legs Expenses

**Script:** `src/legs_expenses.py`

**Purpose:** Parse Legs Legal Support, Inc.'s monthly statement PDF and post pass-through
costs (process serving, filing, delivery, copies, deposition officer fees) to Clio as
ExpenseEntry activities, billed **at cost** (no markup).

**Input:** `data/*.pdf` (any filename — the monthly Legs statement, e.g. `June 2026.pdf`)

**Vendor:** Legs Legal Support, Inc. — process serving / courier / court filing.

**This PDF has zero embedded text** — confirmed via `pdfplumber`, 0 characters extracted
per page. It's a scanned/faxed document, one full-page image per page, unlike Bradford's
PDF which has a real text layer. Everything goes through local **Tesseract OCR**
(`pytesseract`), not `pdfplumber.extract_text()`. Requires Tesseract installed as a
system binary (not just a Python package) — `winget install --id UB-Mannheim.TesseractOCR
-e` on Windows. `ensure_tesseract()` fails loud with the install command at startup if
it's missing, and falls back to checking `C:\Program Files\Tesseract-OCR\tesseract.exe`
directly if it's not on PATH (common gotcha — the Windows installer doesn't always add it).

**Two page types, classified by content, not a hardcoded page-count split or the
"Statement"/"Invoice" heading word** (that heading word was found to drop out of OCR
entirely on some pages that otherwise have perfectly good content — a segmentation
issue, not a content one). A page is a **Statement** page if it contains at least one
recognizable `INV #L###### . Amount $X.XX` row; otherwise it's an **Invoice** page if it
has any real content at all:
- **Statement pages** (first few): every invoice number + amount for the month, no
  client names. **This is the authoritative dollar-amount source** — tested and found
  the per-page `Total` field is *not* reliably OCR'able (its position moves with how
  many line items precede it, and it came out garbled or missing entirely on multiple
  real pages), while the Statement's table OCR'd cleanly and completely on every row.
- **Invoice pages** (the rest): one per invoice, one client per page. Multiple service
  line items (`FILE/CONFORM RUSH`, `DELIVERY`, `PHOTOCOPYING/SCANNING`, `FEE ADVANCE`,
  `PROCESS SERVING/RUSH/SPECIAL`, `DEPOSITION OFFICER FEE`, ...), used only for the
  client identifier and a human-readable note — not machine-parsed field-by-field.

**Invoice number extraction — a targeted header crop, not full-page OCR:** full-page OCR
only recovered the "Invoice #" field on ~60% of real sample pages; cropping just that
fixed-position header box (`HEADER_CROP_FRACTIONS`) and re-OCRing with `--psm 6`
recovered it on 100% of the same pages. The `#L` prefix before an invoice number
frequently misreads as `41` or `1` (`L606098` → `41606098`) but the trailing 6 digits
themselves come through clean every time, so invoice numbers are normalized to "last 6
digits of whatever digit run was found," never matched as an exact `L######` string.

**Client identifier:** the last non-blank, non-boilerplate, name-shaped line on an
invoice page — either a bare last name (`TANGUAY`) or a case caption (`ROGERS V
KRINSKY`). Empirically the identifier's position even on pages where the numeric table
got scrambled elsewhere in the OCR output. Court hearing purpose codes (`RFO`, `FRC`, …
reused from `court_calendar.normalizer.PURPOSE_CODES`) are explicitly excluded, since
one can appear as its own trailing line *after* the real identifier (real example: a
`CROSSON V SAMUELS` invoice with `RFO` on the line below it).

**Matching a case caption or joint identifier — try every candidate, four tiers each:**
a caption doesn't reliably say which side is our client (real example: `CROSSON V
SAMUELS` is filed in Clio as `SAMUELS`, not the first-listed party — mediation/divorce
matters can be opened under either name). For each candidate name, in order:
1. `MANUAL_MATTER_MAP` / persisted override
2. Exact last-name match
3. Compound-surname substring match (e.g. `FOOKS-WEBB` on the invoice vs. `WEBB` in
   Clio) — reuses `court_calendar.normalizer.party_names_match()`, the same approach
   already established there, rather than a new one
4. **Opposing Party field match** — some invoices (process serving, deposition officer
   fees) only ever name the *opposing* party, never our own client at all (real
   example: matter `VERSTRAETE, MARY PAULA` has Opposing Party `GARRON, MARK` on file;
   a real invoice's only identifier was the bare word `GARRON`, no caption, no other
   text linking it back to Verstraete). `index_opposing_party_by_last_name()` pulls
   this live via `custom_field_values{field_name,value}` (`MATTERS_FIELDS_WITH_OP`) —
   the same nested-selector gotcha as Court Calendar Sync's Court Case Number, not a
   plain field on the base Matter resource.

Only if none of the four tiers resolve does it become an exception.

**Firm overhead (excluded from client billing, not billed to any client):**
- The monthly retainer line item — detected by the word `RETAINER` in the page body.
- `FIRM_OVERHEAD_IDENTIFIERS` (currently `{"COLLIER"}`) — client identifiers that are
  never a real client, because Legs falls back to the firm's own attorney name when an
  invoice has no distinct client attached (real example: a `FILE IN RECORDERS OFFICE`
  invoice with no case caption came through as a bare `COLLIER`). Add more identifiers
  here as they're confirmed, rather than re-investigating the same one every month.

Both categories show in the dry-run preview's own "Firm overhead" table — an explicit,
named exclusion, not a silent drop and not a generic exception needing resolution.

**Reconciliation check:** since the Statement independently lists every invoice's
amount, every parsed invoice (matched, exception, or firm-overhead) is checked against
it by invoice number and by total. A mismatch — a missed page, a misread amount —
surfaces as a loud warning in both the log and the dashboard (a red banner) rather than
silently under- or over-billing a client. This is a stronger check than Bradford gets,
because Legs' own statement happens to give an independent total to check against.

**Page thumbnails:** each invoice page also gets a small JPEG (100dpi, ~64KB,
`save_page_thumbnail()`) saved to `output/{stem}_thumbnails/page_N.jpg`, served via the
auth-gated `GET /legs/thumbnail/{stem}/{page}` route and shown as a clickable column in
every dashboard table (Exceptions, Firm overhead, Matched) — click to open full-size.
Given OCR is a real, expected error source here (unlike Bradford's clean text
extraction), this lets staff visually spot-check a row against the actual scan before
confirming.

**Billing rule:** `price = <Legs' invoice total from the Statement>`, `quantity = 1` —
at cost, no markup (firm decision). `payload["data"]` is the only thing ever POSTed to
Clio (`post_entry` sends `{"data": payload["data"]}` explicitly) — `page` is a sibling
key on the payload dict, present for the dashboard's thumbnail links but never part of
the API call.

**Outputs:**
- `output/{stem}_payloads.json` — API payloads (always written)
- `output/{stem}_exceptions.csv` — unmatched client identifiers (if any)
- `output/{stem}_thumbnails/page_N.jpg` — per-invoice-page thumbnails
- `logs/legs_expenses_YYYYMMDD.log`

## Manual overrides
Same two-tier pattern as Bradford: a `MANUAL_MATTER_MAP` constant at the top of the
script for deliberate, reviewed, permanent overrides, plus
`data/legs_manual_matter_map.csv` for dashboard-resolved overrides
(`effective_manual_matter_map()`, code constant wins on conflict). Persisted overrides
apply to *future* statements too — resolving "KARANJIA" once means it auto-resolves
every month after, not just the statement being resolved.

## Workflow
```powershell
# 1. Save the Legs statement PDF to data/

# 2. Dry run — check log for exceptions
uv run src/legs_expenses.py --input "data/June 2026.pdf" --dry-run

# 3. Fix exceptions: add matter IDs to MANUAL_MATTER_MAP, re-run dry-run

# 4. Live run
uv run src/legs_expenses.py --input "data/June 2026.pdf"
```

## Resolving exceptions from the dashboard
`/legs`'s exceptions table works like Bradford's (suggested-match button + manual
resolve, same `/legs/resolve-exception` -> persisted-override -> re-run-dry-run flow),
but the manual-entry field is a **type-to-filter matter-name search**
(`matter_search.js` + a `tojson` Jinja2 filter registered app-wide in `web/app.py`,
reusable by any future page) instead of a raw Matter ID field — staff know client names,
not Clio's internal IDs. The full open-matters list (`RunResult.all_matters`, ~240
entries) is embedded once per page load; filtering is client-side, no per-keystroke
network call.

---

# PaperCut Shared Account Sync

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

# CAP — Collier Automation Platform (dashboard is step one; Service + Scheduler not started)

**Resolved 2026-07-30:** the web dashboard (`src/web/`, rebranded 2026-07-29 — see "Web
Dashboard" above) is **step one of this vision, not a separate thing.** It already
delivers the core goal below — one branded UI wrapping every subproject's
`run_pipeline()`, instead of scattered one-off scripts — for on-demand/manual use. What's
still not built is the automated half described in this section: a Windows Service +
Scheduler layer for unattended/scheduled runs (e.g. Court Calendar Sync's still-planned
morning run) that don't require a human to open the dashboard.

Idea from 2026-07-27, expanded 2026-07-28: rather than keep adding one-off scripts for
each new Clio-adjacent integration, consolidate them into one maintainable platform —
**CAP (Collier Automation Platform)**, chosen over narrower names like "Clio Automation
Service" because the intent is for this to eventually be the integration layer for the
whole practice, not just a Clio-facing tool. One platform to maintain, not many one-offs.

**Architecture direction for the remaining (unattended/scheduled) piece:** a Windows
Service + Scheduler, with each integration as a plug-in module rather than its own
standalone script — sitting alongside the dashboard (not replacing it) for the runs
that shouldn't need a human at the keyboard:
```
                Clio
                  │
        ┌─────────┼─────────┐
        │         │         │
    Contacts   Matters   Activities
        │         │         │
        ▼         ▼         ▼
              CAP (Windows Service + Scheduler)
 ┌────────┬──────────┬─────────┬──────────┬───────────┐
 │PaperCut│RingCentral│Outlook │Accounting│Reporting  │
 └────────┴──────────┴─────────┴──────────┴───────────┘
```
Every module talks to Clio through **one internal API client**, not directly — a future
Clio API change (or a repeat of this repo's own custom-field/nested-selector gotchas)
gets fixed in one place instead of N scripts each needing the same fix separately.

**Modules to fold in — status as of 2026-07-30:**
- PaperCut account synchronization — still just an idea, not started (see PaperCut
  Shared Account Sync above)
- RingCentral contact synchronization — built, has a dashboard page (`/ringcentral`)
  *and* its own standalone daily Windows Scheduled Task (`sync-ringcentral.bat`), so
  it already achieves "unattended scheduled run" per-subproject, just not through a
  unified CAP service — worth noting as a pattern (one `.bat` + `schtasks` per
  subproject) that could cover a lot of this section without a full service ever
  getting built
- Matter-based print cost exports — built as Printer Expenses, dashboard page only,
  no scheduling yet
- Court Calendar Sync — dashboard page only (on-demand); the scheduled-morning-run-
  with-emailed-report upgrade is still not built

**Explicitly not needed:** pushing Clio contacts out to individual staff phones
(iPhone/Android). RingCentral already resolves caller name via CallerID off the
existing Directory Sync — a separate device-level contact push would be solving an
already-solved problem.

**Candidate modules — captured for future evaluation, none of this is scoped or
committed work yet:**
- **Matter close cleanup** — nightly job to close/archive PaperCut accounts, document
  folders, and shared drives for matters that just closed in Clio, instead of leaving
  stale access around indefinitely
- **Automatic print-cost disbursements** — Printer Expenses today is a monthly manual
  drag-and-drop import; this would post the Clio ExpenseEntry automatically as PaperCut
  records usage, no monthly file needed
- **Billing intelligence** — nightly cross-check of calendar entries, phone calls, and
  emails against entered time, flagging likely missed billable events (directly
  supports the firm's stated collections priority)
- **A/R automation** — aging invoice reminders escalating from client email/text ->
  attorney notification at 60 days -> collections queue at 90
- **Conflict-check assist** — on new-contact creation, search existing clients, related
  contacts, and opposing parties for a possible conflict
- **Document intelligence** — auto-route new documents to the right matter subfolder
  by type (motions, declarations, financials, etc.)
- **Office dashboard** — a shared-screen view of daily firm-wide stats: open matters,
  today's hearings, new consultations, outstanding A/R, pages printed, hours entered
  vs. missing
- **Employee productivity summaries** — daily per-attorney/staff rollup of billable vs.
  admin time, emails, calls, documents, appointments, estimated utilization
- **Reception call-lookup / screen pop** — on an inbound RingCentral call, look up the
  caller in Clio and surface matter info (client: balance due, trust, WIP, next court
  date; opposing counsel/party: which matter they're opposing) to reception before they
  answer (distinct from CallerID name display, which is already solved). Feasibility
  discussed 2026-08-01, not scoped or started — see below.

  **Trigger:** RingCentral's directory *export* is push-only (see RingCentral Directory
  Sync above), but that's a different API surface than call events — RingCentral's
  Notification/Subscription API supports real-time webhooks on Telephony Session events
  (ringing, with caller ANI), which is what a screen pop would listen on. Not yet
  verified against this account's RingCentral plan/app permissions — first thing to
  confirm before building.

  **Lookup:** needs its own phone→matter reverse index built straight from Clio contact
  data, *not* reused from `ringcentral_directory.py`'s output CSV — that output collapses
  contacts sharing a phone number into one merged directory row (see "Phone dedup" under
  RingCentral Directory Sync), which loses the individual matter link a screen pop needs.

  **Matter data:** trust/WIP math already exists in `trust_monitor.py` (WIP =
  `unbilled_amount` + draft/awaiting_approval bill totals, not `unbilled_amount` alone —
  see Trust Monitor & Replenishment Requests); next court date is a
  `calendar_entries.json?matter_id=...` query, same as Court Calendar Sync uses.

  **Main open risk — latency, not data availability:** a phone rings for maybe 15-20
  seconds. Live-summing WIP across bill states on every ring is cutting that close.
  Leaning toward a cached snapshot table (refreshed every few minutes, keyed by phone
  number, same shape as `trust_requests`) that the screen pop reads instantly, rather
  than hitting Clio live per call — revisit this tradeoff when actually scoping it.

  **Also undecided:** how the pop is actually displayed at reception's desk (always-on-
  top window vs. a dashboard browser tab pushed to via WebSocket/SSE — the dashboard is
  currently request/response only, no server push exists yet).
- **Matter timeline** — unify calls, emails, documents, billing entries, hearings, and
  notes for a matter into one searchable timeline
- **AI matter assistant** — auto-generated per-matter summary: last hearing, upcoming
  deadlines, outstanding discovery, balance due, last client contact

**Not decided yet:** whether this becomes a new top-level module wrapping the existing
scripts' logic, a rewrite, or a scheduler that just orchestrates the existing CLI entry
points unchanged — revisit when this is actually picked up. Bradford Invoice Import and
Legs Expenses stay as their own document-import tools rather than CAP modules — they
parse a contractor's PDF invoice, which isn't the "keep an external system synced with
Clio" pattern the rest of this platform is built around.

---

# Court Calendar Sync

**Modules:** `src/court_calendar/` (used through the web dashboard, `/calendar`)

**Purpose:** Verify every SD Superior Court hearing has a matching Clio calendar entry —
a Python/Clio port of the standalone `calendar-check` project, which compared against
Outlook via Microsoft Graph. Comparison itself is read-only; **corrected 2026-08-01** —
a prior version of this doc claimed the whole subproject never writes to Clio, but
there is now one explicit, single-matter write path (Court Case Number, see below),
added along with case-number reconciliation and never updated here at the time.

**Input — two ways to get court text in, both feed the same comparison:**
1. **One-click fetch** (`court_calendar/court_fetch.py`, ported from `calendar-check`'s
   `backend/src/routes/fetch.js` + its `isTargetAttorney()` filter) — the normal path
   now. Pick a staff name from the `/calendar` dropdown; `POST /calendar/fetch` scrapes
   the SD Superior Court's public calendar search directly (no login needed — same
   "public site, unauthenticated" pattern as nothing else in this repo touches, unlike
   every other subproject which only ever talks to Clio/RingCentral/Outlook APIs) and
   filters server-side to that attorney via `is_target_attorney()` — an AND-logic word
   match on a normalized name, so "HEIDI COLLIER" matches "HEIDI D. COLLIER, ESQ" but a
   bare "COLLIER" alone does not.
2. **Manual paste** (`POST /calendar/import`, the original fixed-width-text textarea) —
   still there as a fallback for whenever the live scrape fails or a different search is
   needed.

Both call the same `upsert_court_events()` + comparison run.

**Matching strategy — matter-ID-first, text-fallback:**
1. Each court event's party name resolves to a Clio matter ID via `matter_matching.py`
   (same lookup Printer Expenses/Bradford Invoice Import use).
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

**The one write path — Court Case Number:** `POST /calendar/update-case-number`
(`court_calendar/clio_matter_update.py`'s `update_matter_case_number()`) PATCHes a
single matter's "Court Case Number" custom field, triggered by an explicit per-row
button click in the comparison table — never automatic, never bulk. Everything else in
`court_calendar/` only ever reads Clio. The custom field's numeric id is looked up once
via `/custom_fields.json?parent_type=Matter&query=Court+Case+Number` and cached
in-process (`_find_case_number_field_id()`) rather than re-fetched every call.

**Client court-date report:** `/calendar/client-list` (HTML preview) and
`/calendar/client-list/download` (Word doc) — one section per client with their upcoming
court dates plus **Responsible Attorney**, **Originating Attorney**, and **Responsible
Staff**, fetched live from Clio (`court_calendar/matter_fields.py`).

**Explicitly out of scope:** the Timeslips "Billing Readiness" A/R matching feature from
`calendar-check` was dropped — Clio's own trust accounting replaces it; there's no A/R
CSV import or `client_case_mappings`-style conflict resolution here.

**Comparison reason flags:** every non-matched row in `/calendar`'s comparison table gets
a specific `reason` (ported from `calendar-check`'s `findMismatchReason()`): "No matter in
Clio", "Ambiguous matter", "No calendar event", "Wrong date", or a comma-joined "Time/Dept/
Purpose mismatch" — never a blank "Missing" badge with no explanation. Full detail is a
hover tooltip on that cell (`matcher.py`'s `changes` list, one sentence per reason).

**Matter owner column, and the CSV-export myth:** `matter_fields.py` fetches
**Responsible Attorney/Staff, Originating Attorney, and Court Case Number live** from
Clio — a prior version of this doc claimed these weren't exposed by the API and were
CSV-export-only (`data/clio-matters.csv`); that was wrong, just undiscovered. They're
not plain fields on the base Matter resource (a flat `fields=` list returns nothing for
them, same gotcha as Bradford's `custom_rate`) — Responsible/Originating Attorney and
Responsible Staff are nested User relationships (`responsible_attorney{name}` etc.),
and Court Case Number is a Matter custom field (`custom_field_values{field_name,value}`).
Confirmed live 2026-07-24 against a real matter, matching the Clio UI exactly, at a time
when `data/clio-matters.csv` had already drifted stale (last exported weeks earlier) and
was showing wrong values for that same matter. This means `/calendar` no longer touches
`data/clio-matters.csv` at all — no export step for any staff member to know how to run.
The **Owner** column in the comparison table (`matcher.py`'s `matter_owner_initials`) is
Responsible Staff if set, else Responsible Attorney, since Responsible Staff is left
blank on plenty of matters in practice.

---

# Outlook Calendar Migration

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
  same shared lookup Printer Expenses/Bradford Invoice Import/Court Calendar Sync use.
  Unlike the court calendar sync, there's no case number here to disambiguate a client
  with two open matters — those go straight
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

# Court Rules Automation (cancelled)

Was planned: auto-apply Clio Court Rules to matched court events (RFO, Trial, etc.) so
the deadline chain generates without a paralegal doing it by hand per matter. Blocked
on a `403 Forbidden` (Court Rules permission not granted to this app in the Clio
Developer Portal) and never unblocked. **Cancelled** — no longer planned.

---

# RingCentral Directory Sync

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

# Trust Monitor & Replenishment Requests

**Modules:** `src/trust_monitor.py`, dashboard page at `/trust`
(`src/web/routes_trust.py`, `src/web/templates/trust.html`)

**Purpose:** Two independent things on one page.

1. **WIP-vs-trust monitor** (informational) — flags open matters where the
   "cushion" (trust balance minus WIP) has dropped below $2,500, an early
   warning that unbilled work is outpacing trust before it's even billed.
2. **Trust replenishment request review** (the actual point of the tool) —
   lets billing staff bulk-review and send trust top-up requests to Clio as
   unapproved drafts (`approved: false`), with per-matter pause and target
   override, so nothing goes out without a human clicking Send.

**WIP is not `BillableMatter.unbilled_amount` alone.** That field only
counts activity never added to *any* bill — it silently excludes activity
already sitting on a draft or awaiting-approval bill. Confirmed live
(matter WELLS, ANDREW): the field showed $150 vs. his real $11,275.82 WIP.
Correct WIP = `unbilled_amount` + the total of that matter's bills in state
`draft`/`awaiting_approval`. "Outstanding" (already invoiced, unpaid) is a
separate figure — sum of `Bill.balance` for bills in state
`awaiting_payment` — shown for context only, never part of either the
monitor's cushion or a trust request.

**Trust balance comes from `Matter.account_balances`** (`type == "Trust"`),
not `BillableMatter.amount_in_trust` — the latter only returns a record for
matters with nonzero *never-billed* activity, too narrow to use as the
matter universe (confirmed live: 60 of 186 real matters with pending bill
activity were entirely absent from it). `account_balances` needs the
**Accounting** permission checked in the Developer Portal (came back
`{"redacted": true}` before that, same signature as Bradford's `custom_rate`
gotcha) plus the full `clio_auth.py` browser re-auth, not `--refresh`.

**Requested amount is deliberately trust-balance-only, not WIP-based** —
this took two wrong turns to land on (see project memory
`project_trust_monitoring.md` if working on this again). The rule: a matter
becomes a request candidate once its raw trust balance drops below
`ACTION_GATE` ($2,000, fixed); the requested amount tops it back up to
`TRUST_MINIMUM` ($2,500 default, overridable per matter), rounded up to the
next $100. WIP is intentionally excluded from this calculation — billing
stays 100% manual (Clio's own UI, monthly on the 1st + occasional ad hoc,
with trust-application-on-approval and duplicate-billing prevention already
built into that process) rather than this tool trying to pre-fund unbilled
work via a trust request ahead of billing. **Outstanding balances are never
folded into a trust request either** — trust deposits can't legally absorb
card processing fees (would effectively skim client trust funds), but a
direct bill payment can pass the surcharge to the client, so collecting an
overdue bill needs its own separate flow (not built) rather than being
mixed into a trust top-up.

**Request lifecycle**, backed by two tables in `data/clio_dashboard.db`
(`trust_matter_settings`: per-matter target override + pause, persists
indefinitely until unpaused or the matter closes; `trust_requests`:
lifecycle log, since Clio's API has no GET/list endpoint for TrustRequest at
all — this table is the *only* record of what's already been requested). A
candidate with no pending request is new; one with a pending request whose
recorded trust balance still matches current trust is "already requested"
(client hasn't paid yet); a real difference marks the old request stale and
generates a fresh one.

**Credit card fee exposure:** the review table shows a live-updating summary
(count, total requested, estimated card processing fee risk at 2.95% —
`trust_monitor.CARD_FEE_RATE`) for whatever rows are currently checked —
purely informational, recalculated client-side, not applied to any request
or persisted anywhere. Exists because trust deposits can't legally pass the
card surcharge to the client the way a direct bill payment can, so the firm
absorbs it if a client pays a trust request by card — worth seeing before a
large batch send, not after.

**Not yet live-tested:** no Send has ever actually fired against production
Clio. The behavioral assumption that `approved: false` parks a TrustRequest
for internal review without notifying the client has never been confirmed —
do the first real send with a human watching Clio's UI before relying on it.

**Deferred, not built:** email notification for the WIP early-warning (no
email infrastructure exists anywhere in this repo yet — dashboard-only for
now). Interim/automated bill creation was considered and explicitly
rejected — billing stays manual.

## Workflow
```powershell
# Read-only report (writes output/trust_monitor_YYYY-MM-DD.csv + a log)
uv run src/trust_monitor.py
```
The request-review workflow (pause, target override, send) is dashboard-only
via `/trust` — no CLI equivalent, since it's inherently interactive.

---

# Related Projects (legacy — do not duplicate)

| Project | Path | Status |
|---|---|---|
| rolodex | `~/projects/rolodex` | Complete — MyCase contact/case import done |
| clio2ts | `~/projects/clio2ts` | Active — Clio time → Timeslips TSImport (PS1) |
| clio-rate-import | `~/projects/clio-rate-import` | Complete — matter-level billing rates migrated |
