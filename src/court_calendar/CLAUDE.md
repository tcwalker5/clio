> Auto-loaded by Claude Code when working in `src/court_calendar/`. Root map: `../../CLAUDE.md`.

# Court Calendar Sync

**Modules:** `src/court_calendar/` (used through the web dashboard, `/calendar`)

**Purpose:** Verify every SD Superior Court hearing has a matching Clio calendar entry —
a Python/Clio port of the standalone `calendar-check` project, which compared against
Outlook via Microsoft Graph. Comparison itself is read-only; **corrected 2026-08-01** —
a prior version of this doc claimed the whole subproject never writes to Clio, but
there is now one explicit, single-matter write path (Court Case Number, see below),
added along with case-number reconciliation and never updated here at the time.

**Auth — deliberately open, unlike every other subproject (2026-08-04):** every route
under `/calendar` in `routes_calendar.py`, including the client court-date list/report
and the Court Case Number write, has no `require_auth` dependency — no dashboard
passphrase needed. This was an explicit decision so the court calendar can be used by
anyone on the LAN/Tailscale, while the dashboard passphrase still gates every other
subproject (Bradford, Printer, Legs, RingCentral, Trust); logging in still unlocks those
in the same browser session (see "Auth" in `src/web/CLAUDE.md`).

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

**Combined appearances (fixed 2026-08-18):** `matcher.py`'s `compare_events()` sizes each
matter/day's group of court-calendar hearings against how many Clio entries actually exist
for that matter/day before matching. When there are fewer Clio entries than hearings — the
common cause is a combined appearance (real example: matter Bassett, case `26FL001421N`, a
DVRO hearing and an FRC heard together at the same date/time/dept, entered in Clio as ONE
calendar entry, a normal staff convention, not a gap) — every hearing in that group shares
whichever entry best fits by purpose text, rather than the first-processed hearing claiming
it exclusively. A hearing only gets flagged "Purpose mismatch" if *none* of the group's
purposes appear in the entry at all; it's not required to be individually named alongside
the others. Before this fix, the first hearing falsely showed "Purpose mismatch" (the entry
"belonged" to the other one) and the second falsely showed "Missing / No calendar event",
even though Clio had nothing actually wrong. When a matter/day genuinely has as many (or
more) Clio entries as hearings, the original one-entry-per-hearing exclusive-claim matching
still applies unchanged. Regression coverage: `tests/test_court_calendar_matcher.py`.

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
Purpose mismatch" — never a blank "Missing" badge with no explanation. Full detail
(`matcher.py`'s `changes` list, one entry per reason) shows as small muted lines under the
reason itself — brought back 2026-08-18 at Ted's request, echoing a hint the legacy
Outlook-based `calendar-check` tool used to show ("Court says X and Outlook says Y").
Previously this only showed on hover (a `title=` tooltip), easy to miss at a glance; a first
pass showed it as one visible but overly long joined line, revised same-day into `changes`
being a list of small dicts instead of pre-joined sentences — `{"court": ..., "clio": ...}`
for a field-level comparison (rendered as two short lines, "Court: X" / "Clio: Y") or
`{"detail": ...}` for a freeform explanation with no natural Court/Clio split (e.g.
"Ambiguous matter", rendered as one line) — so each row reads as reason + up to two short
lines rather than one long sentence.

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

