> Not auto-loaded — `bradford_invoice.py` and `legs_expenses.py` are flat files in
> `src/`, no subfolder to hang a per-directory CLAUDE.md off of. Open this yourself
> before touching either script. Root map: `../CLAUDE.md`.

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

