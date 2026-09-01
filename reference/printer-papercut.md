> Not auto-loaded — `printer_expenses.py` and `generate_papercut_accounts.py` /
> `papercut_balance_migration.py` are flat files in `src/`, no subfolder to hang a
> per-directory CLAUDE.md off of. Open this yourself before touching any of them.
> Root map: `../CLAUDE.md`.

# Printer Expenses

**Script:** `src/printer_expenses.py`

**Purpose:** Convert monthly Papercut print/copy/scan report to Clio ExpenseEntry API calls.

**Input:** `data/print_copy_summary_by_account.csv` (Papercut export)

**Matter lookup:** live Clio API (`matter_matching.fetch_open_matters()` +
`index_by_display_name()`) at run time — no matters export file to keep current.

**Rate:** $0.10 per page (firm policy, hardcoded as `PRICE_PER_PAGE`)

**Grouping:** All PRINT + SCAN + COPY aggregated into one ExpenseEntry per matter.

**Date:** Extracted from the report header's From/To range — the billing month is
whichever calendar month has the majority of days inside that range, not just the
"To date" month's own (`extract_report_date()`, fixed 2026-09-01). Papercut's export
window doesn't reliably land on a clean calendar-month boundary — real case that
mislabeled a whole month's expenses: "From date = Aug 2, 2026 ..., To date = Sep 1,
2026" was actually August's usage (30 of its 31 days), not September; taking "To date"
alone would have posted it as "Sep 2026".

**Report period sanity check (added 2026-09-01):** `check_report_period()` separately
flags anything other than the full prior calendar month as a banner in the dashboard
preview (`period_ok`/`period_note` on `RunResult`) — this import always represents last
month's usage, run early the following month, so a partial pull, the wrong month, or a
stale re-upload of an old file is worth catching loudly before posting rather than
silently billing the wrong period. Separate from `extract_report_date()`'s own tolerant
fallback (which still produces a best-guess date even from an odd range) — this is
purely an FYI check on top of that, same relationship Legs' reconciliation check has to
its own posting logic.

**Note text (changed 2026-09-01):** now `"Prints/Copies/Scans — <month>: N pages
(...)"`, was `"Copies/Printing — ..."` — if grepping old logs/Clio notes for the prior
wording, check both.

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

## Dashboard exception resolution (added 2026-09-01)
Exceptions previously had no resolution path except editing `MANUAL_MATTER_MAP` in
source — unlike Bradford/Legs. `/printer` now has the same persisted-override pattern:
each exception row gets a type-to-filter matter-name search
(`static/matter_search.js`, same client-side no-per-keystroke-network-call pattern as
Bradford/Legs — see `reference/invoices.md`) and a Save button that POSTs to
`/printer/resolve-exception`. That calls `save_persisted_override()`, appending one row
to `data/printer_manual_matter_map.csv`, then re-runs the dry-run in place so the
resolved name drops out of the exceptions table. `effective_manual_matter_map()` merges
that persisted CSV with the in-source `MANUAL_MATTER_MAP` at run time — the code
constant wins on conflict, since it's the deliberately-reviewed one.

## Exception types
- **No matching open matter** — client name not found among live open Clio matters
- **Ambiguous** — multiple open matters for same display name; add to MANUAL_MATTER_MAP
- **Joint client** — name contains " & "; split across matters manually

**Bare last name shared by multiple open matters — confirm with a human, don't guess:**
a printer account with no first name at all (e.g. `DONOVAN`) is only safe to resolve via
`MANUAL_MATTER_MAP` when exactly one open matter has that last name. Real example hit
2026-08-19: `DONOVAN` alone was ambiguous between two real open matters (`DONOVAN,
BROOKE` and `DONOVAN, MEGAN`) — resolved by asking Ted directly which one actually
printed, rather than picking either automatically. `"DONOVAN": 1786827108` in the map
now reflects that answer (Megan), not a guess.

---


# PaperCut Shared Account Sync

**Script:** `src/generate_papercut_accounts.py`, dashboard page at `/papercut`
(`src/web/routes_papercut.py`) — built 2026-08-19.

**Purpose:** Generate a PaperCut-compatible TSV from Clio open matters so PaperCut's
shared accounts stay automatically aligned with active Clio matters. Eliminates
manual name-matching and the mismatch problem in printer_expenses.py — the actual
switch of `printer_expenses.py` to PIN/Code-based matching is a **separate, not-yet-
done step** (see "Not yet done" below); this subproject only produces the file PaperCut
needs to have that PIN available in the first place.

**Flow:**
1. Pull all open matters from Clio API (`matter_matching.fetch_open_matters()`, same
   shared lookup Printer Expenses/Bradford/Court Calendar Sync use)
2. Write TSV in PaperCut batch import format, one row per open matter, straight to a
   share on the PaperCut server itself — `PAPERCUT_ACCOUNTS_PATH` in `.env` is
   `\\192.168.2.49\cliosync\papercut_accounts.tsv` (confirmed 2026-08-19, see "Network
   share setup" below)
3. PaperCut's own Shared Account Sync feature (Text file source) is configured to read
   that file as a **local path on its own disk**, `C:\...\cliosync\papercut_accounts.tsv`
   (whatever local folder that share exposes) — nothing in this repo calls a PaperCut
   API; PaperCut just polls its own filesystem on schedule (hourly/nightly)

**Overwritten in place every run, deliberately NOT date-stamped** — unlike this
project's other `output/` files. PaperCut is configured to read one fixed path on a
schedule, so the file at that path always needs to be the current state, not a dated
snapshot a human picks. Scope is open matters only (same convention as every other
subproject) — every row is therefore `Enabled=Y`; there's no disabled-row case yet
(closed-matter account cleanup is the separate, unbuilt "Matter close cleanup" CAP
candidate module, not this script's job).

**PaperCut TSV columns (tab-delimited, no header row):**
```
A  Parent Account Name   → matter display_number  e.g. "ALCANTAR, JUAN"
B  Sub-account Name      → blank (top-level accounts)
C  Enabled               → Y (open matters only)
D  Account PIN/Code      → Clio matter's own numeric `id`
E  Credit Balance        → blank
F  Restricted Status     → blank
G  Users                 → blank (use Groups instead)
H  Groups                → [All Users] (Ted, 2026-08-19 — matter access already works
                            this way day to day; no need for a narrower staff group)
I  Invoice Option        → ALWAYS_INVOICE
J  Comment Option        → blank (COMMENT_OPTIONAL default)
K  Notes                 → blank
```

**PIN/Code decision — Clio's `id`, not `custom_number`:** the original planning note
here just said "Clio matter Unique ID," which is ambiguous — this codebase already uses
"MUID" elsewhere (`matter_matching.py`) to mean `custom_number`, Clio's own optional,
user-defined per-matter reference field. Built against the internal numeric `id`
instead: it's guaranteed present and unique on every matter (`custom_number` is blank
on some), and it's already the exact value this codebase's other scripts pass as
`matter.id` in Clio API payloads (e.g. `printer_expenses.py`'s ExpenseEntry POST) — so
switching `printer_expenses.py` to PIN/Code matching later can use the PIN straight
through with no extra Clio lookup to translate it back to a matter id. Confirmed live
2026-08-19 against all 219 real open matters — every row got a non-blank PIN.

**Dashboard:** `/papercut` is read-only status (last generated time, account count)
backed by `papercut_sync_runs` in `data/clio_dashboard.db`, same shape as RingCentral
Directory Sync's `/ringcentral` — a "Generate now" button re-runs the pipeline
synchronously, no confirm step (never writes to Clio or PaperCut, so nothing
destructive to gate behind a second click).

**Network share setup (confirmed working 2026-08-19):** rather than have PaperCut's
service account read a share hosted on *this* machine (would need PaperCut's own
service to carry working network credentials for a cross-machine read), the direction
is reversed — this machine pushes the TSV onto a share hosted **on the PaperCut server
itself** (192.168.2.49), so PaperCut ends up reading a plain local file, and only the
write side needs cross-machine auth:
- A dedicated local Windows account `cliosync` was created on 192.168.2.49, scoped to
  one folder only, with **Change** (share permissions) + **Modify** (NTFS permissions)
  — both layers are required; SMB enforces whichever is stricter, and Change-without-
  Modify was the actual first failure mode hit here (auth succeeded, read worked,
  write came back Access Denied until Modify was added on the NTFS Security tab).
- The share is named `cliosync` (not `CAP` as first assumed while planning this) —
  worth remembering if this ever needs re-diagnosing, since `\\192.168.2.49\CAP`
  doesn't exist and fails with a generic "network name cannot be found" that looks
  like an auth problem, not a wrong-share-name problem.
- This machine authenticates via a cached Windows credential (`cmdkey /add:192.168.2.49
  /user:cliosync /pass:...`), not a mapped drive letter — a mapped drive wouldn't
  reliably survive under a non-interactive Scheduled Task later, a cached credential
  does. **Gotcha hit live:** once *any* stored credential exists for a target, Windows
  tries it first and does not silently fall back to the anonymous/guest access that
  had been working before — so a wrong password made things fail *harder* than before
  any credential existed at all, not more gracefully. A stale/wrong stored credential
  also has to be deleted by its **Target** value (`cmdkey /delete:192.168.2.49`), not
  by username — `cmdkey /delete:cliosync` fails silently with "Element not found."

**Cutover plan (as of 2026-08-19, not yet executed) — Ted's call, do this at the end
of the month:**
1. Run a **full-month** (Aug 1–31) `printer_expenses.py` live billing pass first,
   covering everything up through the day before the switch — not a partial-month
   report, and not skipped, since PaperCut's account `Balance` field turned out NOT to
   be a safe substitute for this (see "Balance is not unbilled-charges tracking"
   below). Confirm this covers the full month so nothing between Aug 1 and whenever
   the last billing run happened gets missed or double-billed.
2. Only then run **`papercut_balance_migration.py`** (not the plain
   `generate_papercut_accounts.py` output) as the Shared Account Sync source for the
   one cutover run, so PaperCut's existing account `Balance` figures carry over
   instead of every account starting over at a fresh, unrelated number under its new
   Clio-matched name.
3. Every sync run *after* that first cutover can go back to the plain
   `generate_papercut_accounts.py` output — by then names are aligned and there's
   nothing left to preserve balance-wise that a normal name-matched update wouldn't
   already handle.

**`papercut_balance_migration.py` — ONE-TIME migration helper, built 2026-08-19, not
part of the regular pipeline:** exists because two real problems surfaced while
planning the cutover, both financially real, not hypothetical:
- PaperCut's Shared Account Sync matches existing accounts **by name**. This firm's
  hand-created PaperCut account names don't all match Clio's `display_number` exactly
  (same divergence `printer_expenses.py`'s `MANUAL_MATTER_MAP` already works around in
  the other direction) — a name that doesn't match exactly would sync as a brand-new
  account rather than update the existing one, stranding its balance under the old
  name.
- PaperCut's own documentation is genuinely contradictory (checked live against two
  manual mirrors) about whether a **blank** Balance field on a matched existing account
  resets it to 0 or leaves it alone. Rather than gamble, this script always writes the
  account's real current balance explicitly, sidestepping the ambiguity entirely.

Usage: `uv run src/papercut_balance_migration.py --existing data/shared_account_list.csv`
(that existing-account export is PaperCut's own "Shared account list" export, copied
from the `cliosync` share — not generated by this repo). Writes
`output/papercut_accounts_migration.tsv` (same shape as the normal output, Credit
Balance populated from the matched existing balance) and
`output/papercut_balance_mismatches.csv` (every existing account that couldn't be
matched, classified so a human doesn't have to eyeball hundreds of rows):
- **Likely match by last name on a still-OPEN matter** — primary signal is
  `matter_matching.index_by_last_name_all()` (same approach Bradford/Legs/Court
  Calendar already use for bare-last-name identifiers), not whole-string difflib —
  a real-data check found whole-string difflib mostly false-positives on a shared
  common first name (two unrelated "JAMES" or "MICHAEL" clients), not genuine name
  similarity. difflib is only a last-resort fallback (cutoff 0.8) when no last-name
  index match exists at all.
- **Ambiguous** — 2+ open matters share that last name; lists every candidate, no
  auto-pick (same "confirm with a human" rule as `printer_expenses.py`'s own
  `MANUAL_MATTER_MAP` guidance above).
- **Matches a closed/non-open Clio matter** — expected, no action needed.
- **No matching Clio matter under any status** — real-data check 2026-08-19 found 199
  of 415 existing accounts in this bucket, totaling ~$5,121 — but confirmed with Ted
  this is expected and **not worth chasing down**: most of these predate the firm's
  Clio migration entirely and were never going to have a Clio counterpart. Don't
  re-investigate this bucket as if it were a bug — it's a real, understood, and
  accepted gap between "every PaperCut account that has ever existed" and "every
  client Clio has ever known about."

**Balance is not unbilled-charges tracking — a real finding, not an assumption:**
compared PaperCut's live `Balance` figures against a real Aug 1–19 usage report for
several accounts (e.g. `ALCANTAR, JUAN`: Balance -$70.80 vs. that period's usage cost
only -$42.10). Balance is a much larger, ever-growing cumulative total that appears to
include usage from **before** the report's own start date — meaning it has likely never
been reset even after past months were billed to Clio via `printer_expenses.py`. Two
consequences: (1) it already includes whatever is in any given period's usage report,
so adding a usage report's costs on top of it double-counts; (2) **nothing in this
codebase reads or resets PaperCut's Balance field to drive Clio billing** —
`printer_expenses.py` bills strictly off whatever fresh usage-report export it's given,
completely independent of PaperCut's own running balance. Balance-preservation during
the account migration (above) is therefore about not arbitrarily resetting PaperCut's
own internal historical number, not about "the money still owed" — that figure lives
only in the monthly usage-report → `printer_expenses.py` → Clio pipeline.

**Not yet done:**
- `printer_expenses.py` still matches by fuzzy display-name, not by PIN/Code — this
  was always meant to be the payoff of this subproject (see its "Manual overrides"
  section), not automatic just because the TSV now exists. Switching it over is a
  follow-up, not done here.
- No Windows Scheduled Task registered for this yet (same manual, confirmed-with-user
  pattern as RingCentral's `sync-ringcentral.bat` — see "Daily automation" under
  RingCentral Directory Sync). Whatever account eventually runs that task needs its
  own `cmdkey` credential for 192.168.2.49 — the one set up 2026-08-19 is tied to
  whichever Windows user ran it interactively.
- PaperCut's own Shared Account Sync (Text file source) hasn't actually been pointed
  at the file yet and no sync has been confirmed to run — the write side (this repo →
  the share) is confirmed working; the read side (PaperCut → its own disk, on
  schedule) is still a manual PaperCut-admin config step, deliberately deferred until
  the end-of-month cutover plan above.
- The end-of-month cutover itself (full-month billing pass, then the balance-migration
  sync) hasn't happened yet as of this writing — see "Cutover plan" above.
- One small leftover exception from the 2026-08-19 dry-run test: `TO, CHAU` (1 page,
  $0.10) doesn't match any open matter under any spelling found — confirmed with Ted
  to leave as an unresolved exception for now rather than guess.

**PaperCut sync docs:** Accounts > Shared Account Sync > Text file source.
File location must be accessible from the PaperCut server (mapped drive or UNC path;
here, a local path since the sync reads its own machine's disk — see above).
Sync runs Hourly or Overnight (nightly ~12:55am).

---

