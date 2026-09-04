> Not auto-loaded — `date_calculator.py` is a flat file in `src/`, no subfolder to
> hang a per-directory CLAUDE.md off of. Open this yourself before touching it.
> Root map: `../CLAUDE.md`.

# Date Calculator

**Modules:** `src/date_calculator.py`, dashboard page at `/date-calculator`
(`src/web/routes_date_calculator.py`, `src/web/templates/date_calculator.html`) —
built 2026-09-04 (Ted).

**Purpose:** A generic two-date duration calculator (years/months/days, plus total
days) — Ted's original ask was as simple as "12/22/1967 and today." Extended into a
family-law-specific mode: length of marriage (Date of Marriage → Date of Separation),
including California's **Family Code § 4336 "long-term marriage" presumption** (a
marriage of 10 years or more, from marriage to separation, shifts the burden on
spousal support duration) — flagged automatically whenever the computed span reaches
10 years.

**Reuses Moore/Marsden's existing Date of Marriage/Date of Separation fields and read
logic** (`moore_marsden.clio_matter_dates`, field names imported directly rather than
re-typed) — real Clio matter custom fields (ids `18509746`/`18509761`, `date` type),
not something this tool invents. **Deliberately read-only on those two fields** (Ted,
2026-09-04) — entering/correcting Date of Marriage/Separation permanently still only
happens via Moore/Marsden's own Settings, so there's exactly one write path into those
two fields, not two independently-maintained ones. Typing dates directly into this
calculator (e.g. for a matter where neither is set in Clio yet, or for a fully generic
calculation with no matter at all) only affects what's shown here — nothing is saved
to Date of Marriage/Separation from this tool.

**Selecting a matter loads it immediately — no separate "Load Matter" button**
(changed 2026-09-04, Ted: "that is more consistent with a dropdown"). Every other
page sharing `matter_search.js` (Equalizer, Legs, Moore/Marsden, Printer) keeps its
own explicit submit button — this page is the one exception, via a new opt-in
`autoSubmit` parameter (`initMatterSearch(matters, true)`, default `false` everywhere
else) rather than changing the shared behavior for all five pages. Deliberately
scoped this way, not by accident: **auto-firing on selection is only safe because
loading a matter here is read-only** (it just fetches Date of Marriage/Separation to
display) — the actual write (Save "Length of Marriage" to Clio) stays its own
separate, explicit button click, unaffected by this change. Don't set `autoSubmit`
on a matter-search form whose submit itself writes to Clio.

**Duration math** (`calculate_duration()`) is a plain calendar-accurate
years/months/days breakdown — the standard borrow-from-the-previous-month technique
(the same one `python-dateutil`'s `relativedelta` uses internally). Not pulled in as a
dependency since this project has no `dateutil` anywhere else and the algorithm is a
dozen lines; verified against the 1967→2026 example by hand (58 years, 8 months, 13
days) and against a live matter (VALENTINE, LAURA: 2004-09-04 → 2025-10-01 = 21 years,
27 days) before shipping. The exact same algorithm is duplicated in the page's
client-side JS for instant, no-round-trip display as you type — deliberately: this is
pure math with no Clio dependency, so a server round-trip per keystroke would be pure
latency for no benefit. **The server never trusts the client's computed value** — `POST
/date-calculator/save` recomputes from the raw start/end dates itself before writing
anything to Clio, same "don't trust client-submitted derived values" discipline as
any other write path in this project.

**New Clio custom field this tool owns: "Length of Marriage"** (text, parent type
Matter) — e.g. `"8 years, 4 months"`, or `"21 years — Long-Term Marriage"` once the
10-year threshold is crossed. Ted created this field manually in Clio's Custom Fields
settings (same "human creates the field, code discovers it live by name" convention
as every other custom field in this repo — Court Case Number, FLARPL Recorded,
Payment Plan, Date of Marriage/Separation). **The summary saved to this field
deliberately omits days** (years/months only) — day-level precision isn't meaningful
for a field meant to be scanned at a glance; the interactive page still shows full
years/months/days/total-days precision, just doesn't persist all of it.

**Same existing-CustomFieldValue-id gotcha `clio_matter_dates.py` already
documented** for Date of Marriage/Separation applies here too (some fields get an
auto-created empty `CustomFieldValue` record on every matter; POSTing a fresh one via
`custom_field:{id}` against a matter that already has one 422s) —
`update_length_of_marriage()` checks for an existing value id first (or accepts one
the caller already has from a bulk fetch) and only falls back to the create-shape when
genuinely none exists yet.

## Two ways the field gets populated

1. **Interactively** — `/date-calculator`, search for a matter (open/pending, same
   scope Moore/Marsden's own matter search uses), which auto-fills Date of
   Marriage/Separation if already set; click **Save "Length of Marriage" to Clio**.
2. **In the background** — `uv run src/date_calculator.py` (`sync_length_of_marriage()`)
   scans every open/pending matter in **one bulk fetch** (`custom_field_values`
   included directly in the `matters.json` call, same pattern
   `court_calendar/matter_fields.py` established — not a live fetch per matter, which
   would be 2×N extra API calls for a 225-matter firm), and for any matter with
   **both** dates set, writes the field only if the computed value differs from
   what's already stored — same "only touch what changed" discipline as
   `ringcentral_directory.py`'s own sync, so a daily run against unchanged matters is
   a silent no-op, not redundant writes/log noise.

**Why polling, not a Clio webhook:** Clio's API does support webhooks
(`/webhooks.json`, confirmed in `reference/openapi.json`) — but they require a public
`https://` URL for Clio's servers to POST to, and this dashboard is deliberately
LAN-only (`cap.lan`, no public exposure — see root `CLAUDE.md`). A daily Scheduled
Task is the fit here, same as every other unattended job in this repo (see
`src/web/CLAUDE.md`'s CAP section) — not real-time, but zero new infrastructure.

**If the "Length of Marriage" field doesn't exist yet in Clio,** both the interactive
Save button and the background sync fail loud with the same clear message ("create it
in Clio's Custom Fields settings first") rather than a raw error — the sync job fails
on its very first eligible matter (nothing partially updates first, since no matter
can have an existing `CustomFieldValue` for a field that doesn't exist at all yet).

## Workflow
```powershell
# Background sync — writes/updates Length of Marriage wherever both dates are set
# and the computed value has changed
uv run src/date_calculator.py
```
Or just visit `/date-calculator` — same duration math, either standalone or attached
to a matter.

**Daily automation:** `sync-length-of-marriage.bat` (repo root) wraps the command
above, intended to run once a day via a Windows Scheduled Task on the same machine
that runs the dashboard — second real example of the per-feature-task pattern
`src/web/CLAUDE.md`'s CAP section settled on 2026-09-03, after RingCentral's own sync:
```powershell
schtasks /create /tn "Clio Length of Marriage Sync" /tr "C:\Users\TEDMINI\projects\clio\sync-length-of-marriage.bat" /sc daily /st 07:15
```
Registering this is a deliberate, one-time manual step (persistent OS-level
automation is confirmed with the user before being created, not silently set up) —
not yet registered as of 2026-09-04, pending the "Length of Marriage" field actually
being created in Clio first.

---
