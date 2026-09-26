> Not auto-loaded — `ringcentral_directory.py` is a flat file in `src/`, no subfolder
> to hang a per-directory CLAUDE.md off of. Open this yourself before touching it.
> Root map: `../CLAUDE.md`.

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

**Multiple numbers on one contact (fixed 2026-08-25):** a single Clio contact can
carry more than one `phone_numbers` entry — real examples on this account: Kevin
Metros (two distinct Mobile numbers), Leyla Kabban, Letitia Perez, Robert Tinajero
(each Work + Mobile). Previously only the `default_number` (or first, if none is
marked default) ever made it into the directory — anyone calling from their
non-default number went unresolved by RingCentral's caller-ID lookup, with no error
or log line indicating a number had been dropped. `fetch_contacts()` now collects
every *distinct* normalized number per contact (`DirectoryContact.extra_phones`,
duplicate values under different Clio labels — e.g. one real contact had the same
number listed as both "Other" and "Mobile" — collapse to one, same as before).
`_place_extra_phones()` fills the row's still-empty Home/Business/Mobile columns
from those extras, mapped by Clio's own phone `name` label
(`PHONE_LABEL_TO_COLUMN`: Mobile/Cell → Mobile Number, Work/Business/Office →
Business Number, Home → Home Number); an unrecognized label or an already-filled
column falls through `PHONE_EXTRA_FALLBACK_ORDER` instead of being dropped
silently. Fax/pager-labeled numbers are excluded from extras entirely — not
something a caller-ID directory should be dialing.

Only applied to **single-contact rows** — a merged row (Phone dedup, above) already
represents more than one person, and there's no unambiguous owner to attribute a
second number to once that's true, so extras are skipped there rather than guessed
at. An extra number that collides with a number already placed anywhere else in the
directory (including an unresolved conflict's still-reserved primary number) is
dropped with a `logging.warning` listing the contact/number/reason, rather than
silently failing later at RingCentral's own upload-time rejection — same "fail
loud" rule as everywhere else in this project. `compute_snapshot_hash()` now hashes
the Home/Business columns too, so an extra-number change is actually detected as a
change worth re-uploading, not silently treated as a no-op. Live-tested 2026-08-25
against real data — all 4 known multi-number contacts came through with both
numbers in separate columns, zero drops.

**429 retry added 2026-09-23** after a real live failure: this account has enough
contacts that `fetch_contacts()` paging through all of them (no server-side "id in
(...)" filter exists to narrow the request — see that function's own docstring)
hit Clio's rate limit partway through (`Failed to fetch contacts (page 3): 429`),
with nothing here to retry it — a real gap against this project's own stated safety
rule ("Retry on rate limit (429)"), since none of this script's Clio reads had ever
needed it before. Fixed with the same `RETRY_DELAYS = [5, 15, 30]` backoff used
elsewhere in this project, applied via a local `_get_with_retry()` helper. The same
fix was applied at the same time to the two other Clio-paging functions this
pipeline also calls and which had the identical gap: `matter_matching.fetch_open_matters()`
(shared by printer_expenses.py, bradford_invoice.py, court_calendar/matcher.py, and
client_assignment.py too — fixing it here benefits all of them) and
`outlook_calendar/relationships.fetch_oc_op_contacts()`. Live-tested end to end after
the fix: a full run (185 matters, 240 OC/OP contacts, 12 contact pages, 339 resolved)
completed clean.

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

**Added/removed/modified diff, added 2026-09-25 (Ted: "I'd like to show what is being
added or removed... When I click sync now, only the changes should show. When it runs
automatically, the diffs should show by default").** Before this, `changed` was the
only signal — true/false, no detail on what actually differed. Landed as one design
that satisfies both halves of the ask at once: `/ringcentral` always renders the diff
of whatever the **most recent** run found, whether that run was a manual "Sync now"
click or the unattended daily Scheduled Task — so a manual click's own result is
exactly that diff, and an overnight automatic run's diff is already sitting there the
next time anyone opens the page, no extra click needed.

- `ringcentral_sync_runs` gained two columns (`web/db.py`'s `CORE_SCHEMA_COLUMNS`,
  same `_ensure_column` migration pattern as `staff_cache.is_attorney`):
  **`rows_json`** — this run's own directory row snapshot, kept *only* so the next
  run can diff against it. Deliberately not read back from the day's CSV file
  (`output/ringcentral_directory_{date}.csv`) — that file is named by date only, so a
  second run on the same day (an automatic 7am run followed by a manual "Sync now"
  later) would have already overwritten it by the time a diff needs the prior state.
  **`diff_json`** — the diff against the previous run's `rows_json`, precomputed at
  run time (`ringcentral_directory.diff_rows()`) rather than recomputed on every page
  view.
- Diffing key is each row's own `External ID` — the same reconciliation key
  RingCentral's own import matches on (see "Change detection" above), so the
  dashboard's diff mirrors what RingCentral will actually do with the new CSV rather
  than some other notion of row identity. A merged multi-contact row's External ID is
  already `|`-joined across every contact it represents; if that grouping itself
  changes (a third person joins a shared line), the old and new IDs simply won't
  match — correctly shows as one row removed and one added, since it genuinely is a
  different row identity from RingCentral's point of view, not an in-place edit.
- **Three buckets, not just added/removed** — `modified` (same External ID, any
  field differs — e.g. a phone number changed) was added alongside the two Ted named,
  since it's the natural third case of "what changed" and the existing `changed` flag
  already covered it in aggregate with no detail. `describe_row()` renders each entry
  as a `(name, phone)` pair for display — falls back to Company when First/Last Name
  are blank (a merged row), and to whichever phone column is actually filled.
- **Real edge case hit and fixed the same day:** this account's sync history predates
  these two columns, so the first run after shipping this had a previous run row with
  no `rows_json` to diff against. Treating that as "empty previous state" would have
  reported everything as freshly "added" while `changed` correctly said "No" (a real,
  confirmed-live contradiction: "306 added" alongside "changed: No", from a run where
  the directory genuinely hadn't changed at all — there was just no snapshot yet to
  compare it to). Fixed with a third diff state — `None`, meaning "no comparison basis
  exists this time," rendered as "No previous snapshot to compare against yet," not
  "empty" (`{added: [], removed: [], modified: []}`) and not "everything's new." Only
  a genuinely first-ever run (no previous row at all) diffs against a truly empty
  previous state. Self-heals from the next run onward once that run's own `rows_json`
  becomes the previous snapshot.
- Each run also logs every added/removed/modified contact by name/phone
  (`logs/ringcentral_directory_YYYYMMDD.log`), not just the aggregate counts already
  in the "Summary" line — same auditability posture as the rest of this project.
- Live-verified 2026-09-25 against the real account: first post-upgrade run correctly
  showed "no previous snapshot" (not a phantom 306-added) despite `changed: No`; a
  second run immediately after correctly diffed against the first run's real snapshot
  and showed no changes (accurate — nothing in Clio changed between the two runs).
  Tuple destructuring in the template (`{% set name, phone = describe_row(r) %}`) and
  all three badge types (Added/Removed/Modified) were also confirmed to render
  correctly with synthetic data before relying on it.

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
toggleable per the "App permissions" table in `reference/clio-api.md`.

---

## RingCentral REST API reference (general — not Clio-specific)

Captured 2026-09-03 for future RingCentral integration work of any kind, not just
this subproject. **Not exercised anywhere in this repo** — this repo makes zero
RingCentral API calls at all (see "RingCentral has no REST API for the company-wide
directory" above); everything below is from RingCentral's own published docs, not
confirmed live against this account. Verify live before relying on specifics if this
is ever actually used.

### Update User Contact(s) — personal contacts only, not the company directory
`PUT /restapi/v1.0/account/{accountId}/extension/{extensionId}/address-book/contact/{contactId}`
— full source: https://developers.ringcentral.com/api-reference/External-Contacts/updateContact

**This is the per-user "personal contacts" API confirmed absent for the shared
company directory above** — it updates one user's own address book entries, which is
a different resource than the shared directory office phones dial from. Relevant if a
future project needs to sync into an individual's personal RingCentral contacts
(e.g. a personal-assistant-style integration), not for anything company-directory-shaped.

- **Auth requirements:** `Contacts` app scope, `EditPersonalContacts` feature flag on
  the app. Usage plan group **Heavy** (RingCentral's stricter, lower-throughput rate
  tier — budget for this if calling it in a loop over many contacts).
- **Bulk syntax:** `contactId` accepts a comma-separated list to update several
  contacts' full resource in one call.
- **`accountId`/`extensionId` can both be `"~"`** to mean "the account/extension tied
  to the current auth session" — no separate lookup call needed if operating on the
  authenticated user's own contacts.
- **Full resource update (PUT, not PATCH)** — the body replaces the whole contact;
  fields you don't include aren't preserved implicitly (verify this against the docs'
  own PATCH-vs-PUT semantics before assuming partial-update behavior).
- **Fields:** name fields (`firstName`/`lastName`/`middleName`/`nickName`), `company`,
  `jobTitle`, up to 3 emails, `birthday`, `webPage`, `notes`, `ringtoneIndex` (max 64
  chars), `appInfo` (source tag, max 64 chars, useful for marking records written by
  an integration), and a wide set of phone numbers (`homePhone`/`homePhone2`,
  `businessPhone`/`businessPhone2`, `mobilePhone`, `businessFax`, `companyPhone`,
  `assistantPhone`, `carPhone`, `otherPhone`, `otherFax`, `callbackPhone`) **all in
  e.164 format (with the leading `+`)** — same convention this repo's own directory
  CSV already follows for the company-directory side. Three address blocks
  (`homeAddress`/`businessAddress`/`otherAddress`), each `{street, city, country,
  state, zip}`.
- **Response** mirrors the request plus `uri` (canonical resource URL), `id`, and
  `availability` (an enum meaningful only for Address Book Sync — e.g. a contact
  showing as `Deleted` — always `Alive` for a plain read/write here).
- **Phone-number-specific error codes** (relevant to any integration writing numbers
  through this API, given how much of this repo's own RingCentral code is phone-
  format handling — see "Phone dedup" above): `PAB-102` failed to parse a phone
  number, `PAB-104` numbers starting with `*` aren't supported, `PAB-105` extensions
  longer than 10 digits aren't supported. Generic ones: `CMN-100` missing required
  param, `CMN-101` invalid param value, `CMN-409` a string param exceeded its max
  length, `CMN-102` resource not found (404), `CMN-301` rate limit exceeded (429),
  `CMN-211` service overloaded (503) — same retry-on-429 posture this project already
  applies to Clio calls would apply here too.

---

