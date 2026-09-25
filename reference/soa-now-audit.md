> Not auto-loaded — `soa_now_audit.py` is a flat file in `src/`, no subfolder to hang
> a per-directory CLAUDE.md off of. Open this yourself before touching it.
> Root map: `../CLAUDE.md`.

# SoA/NoW Close Audit

**Modules:** `src/soa_now_audit.py`, dashboard page at `/soa-now-audit`
(`src/web/routes_soa_now_audit.py`, `src/web/templates/soa_now_audit.html`) — built
2026-09-22/23 (Ted), rewritten from a hardcoded list to a live scan 2026-09-24.

**Purpose:** scan every open or pending matter for a filed Substitution of Attorney or
Notice of Withdrawal — a real signal a matter is probably done and was never closed —
and let staff close the ones that qualify. Reuses `client_assignment.py`'s
document-matching/classification logic rather than duplicating it (see that module's
own doc for how `SOA_NOW_PATTERN`, folder classification, and the naming-convention
refinement were derived and live-validated: `_get_with_retry()`, `FILED_NAME_PATTERN`/
`CONFORMED_NAME_PATTERN`/`OPPOSING_NAME_PATTERN`/`CORRESPONDENCE_NAME_PATTERN`,
`_classify_folder_path()`, `_refine_classification()`, `evaluate_close_readiness()`,
`close_matter()`).

**Deliberately standalone from Client Assignment (Ted, 2026-09-22): "This page should
be exclusive to that and not concern about case assignments."** No Responsible
Attorney/Originating Attorney/Responsible Staff concept anywhere on this page — it
only cares about SoA/NoW evidence and closing. **Close Case is never gated on
anything this page finds** — confirmed explicitly (Ted, 2026-09-23): "some cases
won't require a NoW or SoA. So being able to close without this is OK." This is a
checklist for staff to work through by hand, not an automated gate like
`/assignments`' own Close button.

## Rewritten 2026-09-24: hardcoded list → live scan of every open matter

**Original version (2026-09-22/23) checked a fixed ~55-matter list** — the ~52 names
from a bulk report built the same week for `client_assignment.py`'s Close button
(matters Client Assignment's "missing Responsible Staff" heuristic had surfaced, plus
3 added by hand later: Cannizaro, Cole, Comer). Ted flagged this as a likely-future
direction ("this might be converted to audit our SoA and NoW periodically") but
explicitly deferred it at the time — "for now, just the hard coded list."

**Validated before building, not built speculatively** — Ted asked for a one-off
scratch scan first ("let's do this on a scratchpad before building another tool...
I want to see if it's worth it"): the same classification run across all 175
currently-open matters (not the hand-picked list) instead. Result: **27 already had
a genuinely filed SoA/NoW, and 26 of those 27 were never on the hand-picked list at
all** — Client Assignment's "missing Responsible Staff" heuristic only caught 1 of
them. "Missing staff" and "has a filed SoA/NoW" turned out to be largely different
matter populations, which is what justified the rewrite.

**Architecture decision — evolve this page in place, not a new app, a toggle, or a
merge into Client Assignment** (Ted asked specifically before building: "is it
easier to create a new app... or use a toggle... or combine them both into the same
sheet"). Once the hardcoded list is gone, there's only one mode left — scan open
matters, check for a filed SoA/NoW, let staff close what qualifies — so there was
nothing left to toggle between or keep as a separate concept. Client Assignment
answers a different question ("who's responsible") and was left untouched; putting a
Close Case button into that page's table would conflate two different jobs. Same
route, same template, same actions (Close Case, Mark SoA/NoW Filed) — only the data
source changed.

**No Y: drive fallback anymore (Ted, 2026-09-24): "nothing new should reference the
Y drive... a paralegal who has something assigned will find it in Clio, or we will
bring forward legacy data and use Clio from then on."** The original version fell
back to searching the legacy Y: drive (`Y:\Client Files\{letter}\{Last[, First]}`)
when Clio had nothing — reasonable for a fixed list of old matters that might never
have been migrated, but not a fit for an ongoing audit of the *current* caseload,
which is expected to live in Clio going forward. That whole subsystem (same-last-name
disambiguation, the folder classification mirror, the file:// → "Copy path" fix) was
removed along with the hardcoded list — see git history before this rewrite if it's
ever needed again, not reproduced here.

**Explicit "Run Audit" trigger + in-memory cache, not recomputed on every page
view** — a full scan is slow (a couple of Clio API calls per open matter, no
artificial delay between them — retries reactively on a 429 via the shared
`_get_with_retry()` pattern instead, per this project's own cross-project convention
now recorded in the `~/projects/` hub memory, never throttles preemptively).
`routes_soa_now_audit.py` keeps `_last_results`/`_last_run_at` as module-level
globals — same "in-memory handoff" pattern this app already uses for
`preview_store.py`'s dry-run previews. `GET /soa-now-audit` just renders whatever's
cached (or an empty "no audit run yet" state); `POST /soa-now-audit/run` runs the
scan (wrapped in `run_in_threadpool`, matching this app's convention for blocking
pipeline calls) and updates the cache. Lost on a dashboard restart — acceptable,
since the audit itself is read-only against Clio and re-running is just clicking the
button again; Close Case and Mark SoA/NoW Filed both write straight to Clio, not this
cache, so nothing real is lost. **Deliberately no progress bar** (Ted declined one
for now, 2026-09-24) — the button just shows "Running... this can take several
minutes" and the page reloads via `window.location.reload()` once the POST resolves.

**Live-verified end to end through the real route, not just the Python function** —
`POST /soa-now-audit/run` against the actual account: 175 matters scanned, 27
classified "filed," 40 "found, needs review" (27 + 40 = 67 total matches — identical
to the earlier scratch scan's count), confirming the productionized route behaves
exactly like the validated scratch script.

**Display filter, added 2026-09-24 (Ted: "There is no reason to flag the case or
display it if nothing is found. Also, if you see OP, their pleadings, but nothing
for us, then there is also no reason to display it. The only conditions to display
will be ones the reflect a sign/conformed/conf in the file or directory")** — before
this, the page listed every scanned matter regardless of what (if anything) was
found, including a "Nothing found" badge for matters with zero findings and matters
whose only findings were OP's own pleadings. `DISPLAY_SIGNAL_PATTERN`
(`\bsigned?\b|\bconformed\b|\bconf\b`, case-insensitive) plus `_has_display_signal()`
now gate what's worth showing a human: a matter only appears if at least one
**non-"opposing"** finding's name or path matches that pattern.

Deliberately a separate, narrower filter from the existing close-readiness logic in
`client_assignment.py`, not a change to it — `evaluate_close_readiness()`/`can_close`
are unchanged (other code, e.g. `/assignments`' own Close gate, depends on that
classification staying consistent) and still get computed and shown for every
displayed matter exactly as before. The two intentionally disagree in one
direction: a matter can be `can_close = True` (e.g. `FILED_WORD_PATTERN` matched a
plain-English "filed" with no "conformed"/"sign"/"conf" in the name) yet still fail
the display filter, since Ted named only sign/conformed/conf here, not "filed" —
such a matter simply won't appear on this page even though it's technically
close-ready by the older classification. That's expected, not a bug: this filter is
about what's worth a human's attention on this specific page, not a redefinition of
what counts as filed.

`run_audit()` itself stays unfiltered (returns every open/pending matter it
scanned, whatever it found or didn't) so a true "matters scanned" count survives —
`soa_now_audit.filter_for_display()` is a separate step `routes_soa_now_audit.py`
applies before caching, keeping `_last_scanned_count` (all of them) distinct from
`_last_results` (only what passed the filter). The template's summary bar shows
both ("175 matters scanned" / "30 flagged for review"), and the now-impossible
"Nothing found" badge case was removed from the template entirely rather than left
as dead code.

Live-verified 2026-09-24 against the real account: 175 scanned, 30 flagged for
display — down from the pre-filter 67 ("filed" + "review" combined) — confirming
the filter is doing real work, and every visible entry's findings are either "Filed
in Clio" or "Found, needs review" with no bare-opposing or empty-findings matter
slipping through.

**Run duration display, added 2026-09-24 (Ted: "what do you think about it saying
last scan took x amount of time").** `routes_soa_now_audit.py` now records a start
timestamp before `POST /soa-now-audit/run` kicks off the scan and stores the
elapsed seconds (`_last_run_duration_seconds`) alongside `_last_run_at` once it
finishes; `_format_duration()` renders it as `"4m 33s"`/`"45s"` next to the "Last
run" timestamp. Purely informational (Ted's own stated reasoning: lets staff
calibrate expectations before clicking Run Audit again, and gives an early-warning
signal if a normal run starts creeping longer over time — growing caseload, more
frequent 429 retries). Unit-verified the formatting helper directly (`3s`, `1m 5s`,
`4m 33s`, `59m 59s`) rather than re-running the full several-minute live scan a
second time just to see a string render correctly.

**Cancel/stop — discussed 2026-09-24, not built.** Ted asked whether a run could be
cancelled once started. Not possible today: `POST /soa-now-audit/run` blocks a
worker thread through the whole scan with no cancellation flag threaded through, so
closing the tab or aborting the fetch only stops the *client* from waiting — the
scan keeps running server-side regardless. Addable if picked up later: a global
"cancel requested" flag checked between matters in `run_audit()`'s loop (can't
interrupt mid-request, only between matters — worst case, a matter mid-429-retry
could take up to ~50s per call before the next check), a Cancel button posting to
set it, and the run returning whatever partial results it had. Estimated real-world
latency to actually stop: typically a few seconds (no rate limiting), worst case
roughly 1-2 minutes (mid-retry on both of a matter's calls) — parked here, not
scoped further, since Ted hasn't asked for it to be built yet.

**Test/internal exclusions carried over from Client Assignment** —
`fetch_open_matters_for_audit()` filters out `client_assignment.EXCLUDED_MATTER_NAMES`
(DOE, JANE and NON-BILLABLE, ADMIN) the same way `/assignments` does, so neither ever
shows up here as a candidate to close.

## Close Case

`POST /soa-now-audit/close` calls `client_assignment.close_matter()` directly — same
underlying PATCH `{"data": {"status": "closed"}}`, no additional gate. **No
confirmation dialog** (removed 2026-09-23, Ted: "I know what I'm doing and that it
can be reopened in clio") — clicking closes immediately. On success the whole card is
removed from the page (`panel.remove()`) — under the live-scan design a closed matter
no longer belongs on this page at all (it isn't open/pending anymore), unlike the
old hardcoded-list version, which kept showing a matter with a "Closed" badge so its
mark-status stayed visible. The route also drops the matter from the in-memory
`_last_results` cache, so a later reload (without a fresh Run Audit) doesn't show it
as still open either.

**Real bug, fixed 2026-09-23, still relevant** — `closeCase()`'s post-success DOM
update used `button.closest("[data-matter-id]")` to find the card to update, but the
button itself also carries `data-matter-id` (needed to read which matter to close),
and `.closest()` checks the element itself first — so it resolved to the button, not
the outer `.panel` card, and the success-path code silently did nothing, leaving the
button stuck on "Closing..." forever even though the close had already succeeded.
Fixed by scoping the selector to `.panel[data-matter-id]`.

## Mark SoA/NoW Filed

**Writes directly to Clio's own "NoW or SoA filed" matter custom field** (checkbox
type) — revised 2026-09-23 (Ted: "I just want the custom field to be marked") from an
original version that kept its own local `soa_now_audit_marks` SQLite table instead,
which nothing outside this dashboard could see (dropped same day). **This field
already existed on real matters** (confirmed live, `custom_fields.json`) — this tool
didn't invent it; e.g. KOBS, MAUREEN already had a pre-existing
`checkbox-1127268708` CustomFieldValue record, value `false`.

**Same "check for an existing CustomFieldValue id first" pattern as
`moore_marsden/clio_matter_dates.py`'s `update_matter_dates()`** (see that module's
own docstring for the full gotcha writeup) — `set_soa_now_filed()` PATCHes the
existing record's own id when one exists (`{"id": existing_id, "value": marked}`),
falling back to `{"custom_field": {"id": ...}, "value": marked}` only if the matter
genuinely has no record yet. PATCHing with a bare `custom_field{id}` against a matter
that already has a record 422s with "custom field value ... already exists" — same
class of bug this project already hit and fixed once for the Date of
Marriage/Separation fields.

**Clio is the source of truth, no local copy** — `fetch_open_matters_for_audit()`
fetches `custom_field_values{id,field_name,value}` alongside every matter in the same
batched query already used for name/status, so the checkbox's checked state can never
drift from what's actually in Clio. The two marks recorded under the earlier
local-table version (KENNEDY, KATHERINE and HUTMACHER, SARAH) didn't carry over —
already noted as a one-time loss when that version was replaced with the Clio-backed
one on 2026-09-23.

**The "mark disappears once a matter is closed" bug from the hardcoded-list version
is moot under the live-scan design** — that version kept closed matters visible with
a badge specifically so a saved mark stayed visible; the live-scan version simply
removes a matter from the page entirely once it's closed (see "Close Case" above), so
there's no state left to lose sight of.

**Still deferred (Ted, 2026-09-22): a richer version that also posts a Clio Note
citing the exact filename/path in the same click** — parked, not built. If picked
back up, start from this section rather than re-deriving the custom-field-vs-Note
tradeoff (custom field needs no admin setup since it already exists and is what Ted
asked for directly; a Note would additionally need the confirming staff member's name
typed in, since CAP has no per-user login).

## 429 retry

Inherited from `client_assignment.py`'s `_get_with_retry()` (added 2026-09-23,
covered in `reference/client-assignment.md`) — this page's per-matter Clio calls
(folder tree + documents, `find_soa_now_documents()`) are the same shared functions,
so they retry on a 429 with the same backoff automatically. No additional delay is
added anywhere in `run_audit()`'s loop — see this project's cross-project
`api-rate-limit-handling` hub memory for why (retry reactively, never throttle
preemptively).

## Workflow

Dashboard-only, no CLI equivalent. Visit `/soa-now-audit`, click **Run Audit** (or
**Re-run Audit** if a cached run already exists), and expect several minutes for a
full scan of the open caseload — the button disables itself and shows "Running..."
until it's done, then the page reloads with results. For each matter with a match:
read the finding's path and open its folder in Clio (`document_management?folder_id=...`)
to actually look at the document, then Mark SoA/NoW Filed and/or Close Case as
appropriate — neither blocks the other, and closing removes the matter from the page.
