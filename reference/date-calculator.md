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

**Reuses Moore/Marsden's existing Date of Marriage/Date of Separation fields, read,
and write logic directly** (`moore_marsden.clio_matter_dates`, names AND functions
imported rather than re-typed/re-implemented) — real Clio matter custom fields (ids
`18509746`/`18509761`, `date` type), not something this tool invents. **Corrected
2026-09-04** — an earlier version was deliberately read-only on these two fields,
sending staff to Moore/Marsden's own Settings to actually set them. Ted reversed this
after using it: *"each tool should not depend on another tool or force the user to
navigate to another one."* One Save click on `/date-calculator` now writes Date of
Marriage, Date of Separation, and Length of Marriage together —
`routes_date_calculator.py`'s `/save` handler calls
`moore_marsden.clio_matter_dates.update_matter_dates()` directly, so there's still
only one *implementation* of the write (no duplicated logic, no drift risk), just two
UI entry points to trigger it now instead of one.

**Real bug fixed 2026-09-04 — Jinja renders Python `None` as the literal text
`"None"`.** The Start/End date `<input>` tags used `{{ matter.date_of_marriage if
matter else '' }}` — when `matter` is truthy but `date_of_marriage` is `None` (no
value set in Clio), that expression evaluates to `None`, not `''`, and Jinja stringifies
it to `"None"`. An `<input type="date" value="None">` fails the browser's date-format
validation and renders as blank — which looked like "the field didn't populate" (Ted's
report, live on DOE, JANE) but was actually a real bug, not a false alarm: it happened
to look like "did nothing" only because the invalid value coincidentally also renders
as empty. Fixed with `{{ (matter.date_of_marriage or '') if matter else '' }}` (and the
same for Date of Separation) — `or ''` catches `None` before Jinja ever stringifies it.

**Custom field values CAN be cleared to blank via the API — unlike Client
Assignment's built-in relationship fields.** Confirmed live 2026-09-04, reverting a
test write on DOE, JANE: `PATCH .../matters/{id}.json` with
`{"custom_field_values": [{"id": "<existing CustomFieldValue id>", "value": null}]}`
returns `200` and genuinely clears it (re-fetch confirmed `null`). This is a real,
useful difference from `client_assignment.py`'s `responsible_attorney`/
`originating_attorney`/`responsible_staff` — those are core Matter *relationship*
fields, not custom fields, and Clio's own spec explicitly disallows `null` for them
(confirmed via a 422 there). Don't assume that restriction carries over to any other
custom field this project touches (Date of Marriage/Separation, Length of Marriage,
FLARPL Recorded, Payment Plan, Court Case Number) — it appears to be specific to that
handful of built-in relationship fields, not custom fields generally.

**Selecting a matter loads it immediately — no separate "Load Matter" button**
(changed 2026-09-04, Ted: "that is more consistent with a dropdown"). Every other
page sharing `matter_search.js` (Equalizer, Legs, Moore/Marsden, Printer) keeps its
own explicit submit button — this page is the one exception, via a new opt-in
`autoSubmit` parameter (`initMatterSearch(matters, true)`, default `false` everywhere
else) rather than changing the shared behavior for all five pages. This distinction
still matters even now that this page writes DOM/DOS: **auto-firing on selection is
only safe because loading a matter is read-only** (it just fetches whatever's
currently in Clio to display) — the actual write (Save, which sets Date of
Marriage/Separation and Length of Marriage together) stays its own separate,
explicit button click, unaffected by this change. Don't set `autoSubmit` on a
matter-search form whose submit itself writes to Clio.

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
   scope Moore/Marsden's own matter search uses — selecting one loads it immediately,
   see below), which auto-fills Date of Marriage/Separation if already set; click
   **Save** to write Date of Marriage, Date of Separation, and Length of Marriage
   together. Date of Marriage/Separation is written unconditionally on Save; if that
   succeeds but the Length of Marriage write fails (e.g. the field genuinely doesn't
   exist), the response still reports success with a `warning` explaining the dates
   were saved but the summary field wasn't — never a flat failure that implies nothing
   happened when the more foundational write actually succeeded.
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

**The "Length of Marriage" field exists in Clio as of 2026-09-04** (Ted created it —
text, parent type Matter). Before it existed, the background sync failed loud on its
first eligible matter with a clear message ("create it in Clio's Custom Fields
settings first"), and the interactive Save still wrote Date of Marriage/Separation
successfully with a `warning` explaining the summary field specifically couldn't be
set yet (see the partial-success note above) — worth remembering if this field is
ever renamed/deleted and that failure mode needs recognizing again.

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
