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
```

---

# Project Structure

```
clio/
├── data/               # Input files — CSV reports, matter exports
├── output/             # Generated payloads and exception reports
├── logs/               # Per-run API logs
├── src/                # Python scripts, one per subproject
├── .env                # Credentials (gitignored)
├── .env.example
├── pyproject.toml
└── CLAUDE.md
```

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

# Subproject 2 — Contractor Invoice Import

**Script:** (planned) `src/contractor_invoice.py`

**Input:** PDF invoice (e.g., `Invoice-40024 THROUGH JUNE 30, 2026.pdf`)

**Purpose:** Parse contractor time entries from PDF and post as Clio activities.

Details TBD once first subproject is complete.

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

# Related Projects (legacy — do not duplicate)

| Project | Path | Status |
|---|---|---|
| rolodex | `~/projects/rolodex` | Complete — MyCase contact/case import done |
| clio2ts | `~/projects/clio2ts` | Active — Clio time → Timeslips TSImport (PS1) |
| clio-rate-import | `~/projects/clio-rate-import` | Complete — matter-level billing rates migrated |
