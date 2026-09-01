> Auto-loaded by Claude Code when working in `src/equalizer/`. Root map: `../../CLAUDE.md`.

# Equalizer

**Modules:** `src/equalizer/`, dashboard pages at `/equalizer`
(`src/web/routes_equalizer.py`, `src/web/templates/equalizer.html`,
`equalizer_matter.html`, `equalizer_worksheet.html`, `src/web/static/equalizer.js`)

**Purpose:** A Python/Clio port of the discontinued "Propertizer" desktop tool
(2026-08-12, Ted) — a spreadsheet-style worksheet for dividing marital assets
and debts between two parties, calculating the equalization payment needed
to balance the division at 50/50 (e.g. one party keeps the house and writes
the other a check for the difference). Same column layout as the legacy
tool: Joint (FMV/Debt/Equity), Before-Tax per party, Tax Basis, After-Tax per
party, G/L — plus row-selection + H/W/`=` toolbar buttons that assign a
row's equity fully to Party A, fully to Party B, or split 50/50.

**Party naming — Husband/Wife by default, first names for same-sex couples:**
every worksheet has `naming_mode` (`husband_wife` default, or `first_names`)
and `party_a_label`/`party_b_label`, edited via the editor's Settings panel.
Switching to `first_names` mode reveals editable label fields plus an
"Autofill from Clio" button (`equalizer/clio_parties.py`) that pulls the
matter's client contact and its Opposing Party relationship contact (same
OC/OP `/relationships.json` lookup pattern `outlook_calendar/relationships.py`
established for the Outlook migration, scoped to one matter) — a best-effort
prefill, never blocking: a contact with no `first_name` on file, or no
Opposing Party relationship at all, just leaves the field blank for a human
to type in. `party_a_role`/`party_b_role` (default Petitioner/Respondent) are
a separate pair of fields from the naming labels — same-sex couples still
have a Petitioner and Respondent regardless of which naming mode is active.
**Gotcha fixed 2026-08-13:** switching the mode dropdown to `first_names`
used to leave the two label fields showing the literal words "Husband" and
"Wife" (carried straight over from the DB defaults) — indistinguishable
from an actual name, so a save with the dropdown flipped but the fields
untouched silently persisted "Husband"/"Wife" as if they were real first
names. Now, switching mode away from `husband_wife` for the first time
since the Settings panel opened clears both fields and immediately calls
the same autofill Clio lookup; Save is also blocked (inline warning, not a
silent no-op) if either field is empty in `first_names` mode.

**PDF includes tax rates (added 2026-08-13, always-shown as of 2026-08-14):**
`equalizer/pdf.py`'s main table has a **Rate** column (None/Ordinary/LT
Gain/ST Gain) per row, matching the on-screen grid — previously only G/L
(Y/blank) was shown, not which rate a row actually used. A "Tax Rates
Applied" section (Federal/State/LT/ST as percentages, one column per party)
follows the main table. Originally gated to only appear when at least one
row had G/L checked with a non-`None` rate — revisited 2026-08-14 after
reviewing a real Propertizer PDF output, which shows this table
**unconditionally**, even on a worksheet with no G/L rows at all; this now
matches that. Live-tested via `pdfplumber` text extraction — the After-Tax
math checked out by hand too ($800,000 Before-Tax − ($600,000 unrealized
gain × 15% LT rate) = $710,000).

**Tax rates and the After-Tax formula:** a Tax Rates panel holds four rates
per party — Federal, State, Long-Term Capital Gain, Short-Term Capital Gain
(entered as decimals, e.g. `0.24`) — since post-divorce tax brackets can
differ between the two parties. **New worksheets now default to the legacy
Propertizer tool's own rates** (Federal 25%, State 9.3%, LT gain 15%, ST
gain 25%, same for both parties — confirmed 2026-08-14 against a real
Propertizer PDF output; `equalizer/store.py`'s `DEFAULT_*` constants). This
only applies going forward — existing worksheets keep whatever rates they
already had (still `0` by default before this change) rather than being
silently rewritten; the DB column `DEFAULT` was updated too but only
actually governs a database created fresh with today's schema, not one
where the columns already existed, which is why `store.create_worksheet()`
sets them explicitly on every insert rather than relying on the column
default alone. Each row picks which rate applies via a **Rate** dropdown
(None / Ordinary / LT Gain / ST Gain — Ordinary combines Federal + State),
independent of the **G/L** checkbox, which gates whether any tax adjustment
applies to that row at all. Tax Basis is a real dollar figure per row (not
just the legacy tool's literal "FMV" label) — no Tax Basis entered means
"sold at FMV, no gain," not zero-by-omission. Checking G/L on a row while
every configured rate is still `0` auto-opens the Tax Rates panel with a
hint naming that row — otherwise checking G/L silently does nothing
visible, since the formula just multiplies by a zero rate; now mostly
relevant to worksheets created before the new non-zero defaults, or a
worksheet where someone deliberately zeroed a rate back out. Formula
(`equalizer/calc.py`, confirmed with Ted 2026-08-12):
```
unrealized gain = FMV − Tax Basis
After-Tax = Before-Tax − (gain × party's Before-Tax share of equity × rate)
```
The apportionment-by-share (a party only carries the tax hit on the share
they're actually receiving) is this code's own interpretation of Ted's
plain-language formula, not something specified at that level of detail —
worth confirming against a real worksheet before relying on it for an actual
filing. A manually-typed After-Tax value always overrides the computed
figure (same "never silently overwrite" rule as everywhere else in this
project) — clearing the field back to empty reverts to auto-computed.

**Equalization payment is fixed at 50/50 and shown TWO ways — Before-Tax
and After-Tax — not just one** (added 2026-08-14, after reviewing a real
Propertizer PDF output that the earlier screenshot alone couldn't settle):
the original screenshot only ever had G/L unchecked everywhere, so its
Before-Tax and After-Tax figures were identical and there was no way to
tell whether Propertizer computed one or two numbers. A real Propertizer
PDF output resolved it — its Summary page shows the equalizing payment
under **both** the Before-Tax and After-Tax column groups, always, even
when they match. `calc.compute_totals()` now returns both pairs
(`equalization_amount`/`payer` from Before-Tax totals,
`equalization_amount_after_tax`/`payer_after_tax` from After-Tax totals),
and both the PDF and the on-screen banner show two lines rather than one.
They're identical whenever nothing on the worksheet uses G/L (the common
case) but genuinely diverge once a row has a real taxable gain — confirmed
with a live test (a $600k unrealized gain row split the two figures by
$45k). Equity itself (`FMV − Debt`) is never stored, only computed at read
time, so it can't drift from its inputs (same reasoning as Trust Monitor's
cushion/shortfall properties).

**Persistence — live editing in SQLite, PDF archived to Clio on Save, never
locked (renamed from "Finalize" 2026-08-14):** `equalizer_worksheets`/
`equalizer_items` in `data/clio_dashboard.db` hold the working state
(matches every other subproject's pattern — fast, resumable, no Clio
round-trip per cell edit). This is the one page in the dashboard that
deviates from the usual full-page-reload-per-action shape: item CRUD and
the H/W/`=` buttons are small JSON endpoints (`equalizer.js` calls them
directly, autosaving as staff edit) rather than a submit-and-reload form —
Settings and Tax Rates saves are the exception, since a naming-mode change
touches column headers, every modal, and the PDF at once; those PATCH then
reload the page rather than patching each place in JS.

**"Finalize" was renamed to "Save to Clio" and stopped being a one-way
lock** (Ted, 2026-08-14: "nothing is ever final" in this line of work — a
family law settlement can change even after paperwork is in). A worksheet
stays fully editable after being saved, and Save to Clio can be clicked
again any time — it pushes a new Document **version** under the same
`clio_document_id` (`parent: {id: clio_document_id, type: "Document"}`
instead of `parent: {id: folder_id, type: "Folder"}`) rather than a
duplicate file, confirmed live to return the same document id with
`version_number` incremented, i.e. genuine Clio-native version history. The
matter Note (below) only posts on the very first save — the link it
contains stays correct across later edits, so re-posting one per save would
just clutter the matter's Notes list. `status` (`draft` vs `saved`) is now
purely informational — it does not gate editing. Deleting a worksheet is
still blocked, but the check moved from `status != 'draft'` to
`clio_document_id IS NOT NULL`, since a saved-and-then-further-edited
worksheet still has to stay non-deletable (it has a real Document + Note in
Clio) even though its status doesn't mean "locked" anymore.

- **Preview** (`GET /equalizer/{id}/preview.pdf`) regenerates the PDF live
  from current draft data and never touches Clio — same dry-run-preview
  role Bradford/Legs/Printer's preview step plays, available any time,
  opens inline for view/print via the browser's own PDF viewer.
- **Save to Clio** (`POST /equalizer/{id}/save`) is the one action that
  writes outside `data/clio_dashboard.db`: generates the PDF and uploads it
  via `equalizer/clio_documents.py` to the matter's existing **Evidence**
  folder as `equalizer-{date}.pdf` (first save) or as a new version of the
  same document (later saves). The Evidence folder is assumed to already
  exist on every matter (firm standard folder template, Ted 2026-08-12) —
  this fails loud rather than creating one in the wrong place if it's
  missing.

**Recall — matter search, not a flat list of everything ever saved:** with
hundreds of clients, a single growing table of every worksheet (draft and
saved) would become unusable. `/equalizer`'s own browsable table only ever
shows `status = 'draft'` worksheets — a short, genuinely actionable list of
what's still unsaved. Finding anything else (a saved worksheet, or checking
whether a matter already has one before starting a new one) goes through
the same matter search box, pointed at `GET /equalizer/lookup` instead of
directly creating a worksheet: a matter with no worksheets yet goes
straight to a fresh one (no extra click for the common case); a matter that
already has one or more (any status) shows `equalizer_matter.html` — that
matter's worksheet history plus a "Start a New Worksheet" button, so
recalling one is a search away rather than scrolling a global list, and
starting a duplicate isn't silent.

**Staff-chosen filename + "Save As" scenario cloning (added 2026-08-14):**
Ted's stated need — staff may run several scenarios for the same matter in
one day (e.g. "house to Husband" vs. "house sold and split") — needed two
things, both live-tested:

- **Save to Clio prompts for a filename every click**, not just the first
  save — `equalizer.js`'s click handler uses the browser's native
  `prompt()`, defaulting to whatever was last saved
  (`worksheet.clio_document_name`) or a fresh `equalizer-{date}` if never
  saved, so an ordinary re-save is just Enter. Cancelling the prompt aborts
  the save — this replaces the plain `confirm()` the button used to have,
  since typing/confirming a name already serves as the "are you sure" gate.
  The typed name is sanitized server-side (`routes_equalizer.py`'s
  `_sanitize_filename()`, allowlist not denylist) before use — it becomes a
  URL path segment in Clio's presigned S3 upload URL
  (`/uploads/document_version/file/{uuid}/{filename}`), so this doesn't
  rely on Clio's backend correctly encoding arbitrary staff input.
  `.pdf` gets appended if missing. The chosen name is stored
  (`equalizer_worksheets.clio_document_name`) and reused as the recall
  list's only way to tell multiple scenario worksheets for one matter apart
  — `equalizer_matter.html`'s table leads with a **Name** column now,
  showing `clio_document_name`, or `(saved before naming existed)` for
  worksheets saved before this feature (their name was never recorded, but
  they genuinely are saved — a bare "unsaved draft" fallback would have
  been actively wrong for those), or `(unsaved draft)` for a worksheet with
  no `clio_document_id` at all.
- **"Save As" clones settings + every row into a brand-new draft**
  (`equalizer/store.py`'s `duplicate_worksheet()`, `POST
  /equalizer/{id}/duplicate`) — for "copy most of the data but make a
  slight change" rather than retyping a whole scenario from scratch. The
  clone starts completely unlinked from Clio (`draft`, no
  `clio_document_id`/`clio_document_name`) — it's an independent worksheet
  that happens to start with the same numbers, not a version of the
  original; editing or deleting it never touches the source worksheet.
  Live-tested against a real saved worksheet (AMOS, CHRISTINE) via the
  actual route — confirmed the clone's 4 items matched exactly, confirmed
  the source worksheet's `updated_at` was byte-for-byte unchanged
  afterward, then deleted the clone (never saved to Clio, so plain local
  delete was safe).

**Matter Note on first save — links back to the live worksheet, confirmed
working 2026-08-14:** alongside the PDF upload, the first Save to Clio also
posts a Note to the Clio matter (`equalizer/clio_notes.py`, `POST
/notes.json`, `type: "Matter"`, rich-text `detail` with an `<a>` link to
`{CAP_BASE_URL}/equalizer/{id}`) so staff browsing the matter in Clio can
find and reopen the worksheet without going through the dashboard at all.
`CAP_BASE_URL` (optional `.env` key, defaults to `http://cap.lan:8421` —
the same on-LAN hostname `CAP Dashboard.url` already points at) exists
specifically so this link resolves for any staff member, not just whoever's
running the server (never `localhost`). This is a secondary, best-effort
step, not folded into the Documents-upload success/failure path — a Note
failure downgrades the save notice to "...but couldn't post the matter
note: {error}" rather than failing the whole save, since the PDF landing in
Evidence is the artifact that actually matters and has already succeeded by
that point. Covered by the **Matters** permission already granted to this
app (no new Developer Portal change needed). Live-tested 2026-08-14 against
matter AMOS, CHRISTINE — worked on the first attempt (content and link
verified correct), then the test Note was deleted via `DELETE
/notes/{id}.json` (also undocumented, also works, `204`).

**Clio-side deletes are soft — trashed documents are detected live, added
2026-08-14:** `DELETE /documents/{id}.json` in Clio doesn't actually delete
— confirmed live (found via `reference/openapi.json`, then verified against
a document Ted deleted himself): it sets `deleted_at` and the record stays
fetchable via GET (still `200`, not `404`) for **30 days** before Clio
purges it for good. Left unhandled, this is exactly how the dashboard's
local `clio_document_id` link goes stale silently — a real case that
happened: Ted deleted a worksheet's document directly in Clio, and the
dashboard kept showing "Saved to Clio" with no way to know otherwise. Fixed
by checking `deleted_at` live (`equalizer/clio_documents.py`'s
`is_document_trashed()` — an outright `404`, the post-30-day-purge case,
also counts as trashed) in two places:
- The worksheet editor page (`GET /equalizer/{id}`) and the matter-recall
  page (`equalizer_matter.html`) both check every linked
  `clio_document_id` live and show a **"Clio document deleted"** badge
  (red) instead of "Saved to Clio" when trashed, plus a warning banner on
  the editor page explaining what happened and what Save to Clio will do
  about it. Best-effort — a check failure (Clio temporarily unreachable)
  just skips the warning rather than breaking the page.
- `upload_pdf()` itself checks before deciding whether to version or
  create fresh: if `existing_document_id` is trashed, it falls back to
  creating a brand-new document in the Evidence folder (same path as a
  first-ever save) instead of trying to add a version to a document that's
  gone. The worksheet's `clio_document_id` gets updated to the new
  document automatically via the normal `mark_saved_to_clio()` call — no
  separate "recovery" code path, it's just what happens when the trash
  check comes back positive.

Live-tested full round trip on a real worksheet (DIBBLE, DIANNA): confirmed
the badge/banner correctly flip to "deleted" after trashing the linked
document, confirmed Save to Clio afterward creates a genuinely new document
(different id, `deleted_at: null`, fresh `version_number: 1`) rather than
erroring out. Caught and fixed a real wording bug in the process — the save
notice was labeling this case "(new version)" (based on "is this the first
save ever," which was `false`) instead of "(...saved as a new document)"
(based on whether the id Clio returned actually changed) — the two aren't
the same question once trash-recovery exists as a case.

**Document upload — confirmed working end-to-end 2026-08-14** (matter AMOS,
CHRISTINE), after two real bugs found via three failed live attempts plus
direct diagnostic testing:

1. `latest_document_version` came back as just `{id, version_number}` on
   create — same gotcha this project has hit before with
   `phone_numbers`/`custom_rate`: a nested field only returns a stub unless
   its subfields are explicitly requested. Fixed with an explicit
   `fields=id,latest_document_version{id,uuid,put_url,version_number}` on
   the create POST.
2. The PUT to the presigned `put_url` kept failing with S3's `403
   SignatureDoesNotMatch` until **both** `Content-Type: application/pdf`
   and `x-amz-server-side-encryption: AES256` were sent together — found by
   decoding the SignatureDoesNotMatch error's own `CanonicalRequest`, which
   named `X-Amz-SignedHeaders=content-type;host;x-amz-server-side-encryption`.
   Sending neither, or only one of the two, both fail. The presigned URL
   doesn't expose what value it was signed with anywhere — `AES256` is a
   confirmed-working value found by testing, not documented by Clio; worth
   re-checking if this ever starts failing again in case Clio's default
   bucket encryption setting changes.

Verified with the actual `upload_pdf()` function (not just the diagnostic
script that found the bugs) — created a real document with real PDF
content, confirmed `version_number: 1`, then deleted it. Every document
created during this debugging (three real failed save attempts plus
diagnostic/verification runs, 8 total) was cleaned up via `DELETE
/documents/{id}.json` (undocumented in the retired openapi.json spec but
works, `204`) — AMOS, CHRISTINE's Evidence folder has nothing Equalizer-
related left in it besides what a real save creates going forward.

## Workflow
Dashboard-only, no CLI equivalent (inherently an interactive spreadsheet
tool) — visit `/equalizer`, search for a matter (this either opens that
matter's existing worksheet(s) or starts a new one), add rows, assign with
H/W/`=` or type Before-Tax splits directly, optionally set Tax Rates and
per-row G/L/Tax Basis, Preview to check the PDF, then Save to Clio whenever
it's ready — and again anytime after, since editing continues.

---

