> Not auto-loaded — `client_assignment.py` is a flat file in `src/`, no subfolder to
> hang a per-directory CLAUDE.md off of. Open this yourself before touching it.
> Root map: `../CLAUDE.md`.

# Client Assignment

**Modules:** `src/client_assignment.py`, dashboard pages at `/assignments`,
`/assignments/report`, `/assignments/caseload` (`src/web/routes_client_assignment.py`,
`src/web/templates/client_assignment*.html`) — built 2026-09-02/03 (Ted).

**Purpose:** Find open matters missing a Responsible Attorney, Originating Attorney,
and/or Responsible Staff assignment, and assign one from a fixed dropdown — the only
subproject in this repo that writes directly to core Matter fields rather than a
custom field, a bill, or a note.

**These three fields are nested User relationships, not plain fields** — same gotcha
`court_calendar/matter_fields.py` already documented for reads (a flat `fields=` list
returns nothing for them; need `responsible_attorney{id,name}` etc.). Writing follows
the same shape: `PATCH /matters/{id}.json` with
`{"data": {"responsible_attorney": {"id": <user_id>}}}`. Confirmed live 2026-09-02
against the designated test matter (DOE, JANE, see root CLAUDE.md) for all three
fields.

**A field can be set or reassigned, but never cleared back to blank through the
API.** Confirmed live: `{"originating_attorney": null}` and `{"originating_attorney":
{}}` both return **200 with the field unchanged** (Clio silently ignores the no-op —
this is a real trap, not just an untested path: it looks like success), and
`{"originating_attorney": {"id": null}}` returns a 422. Clio's own OpenAPI spec
confirms this is by design — `responsible_attorney`/`originating_attorney`/
`responsible_staff` each say **"The keyword `null` is not valid for this field"**.
Once assigned through this tool (or Clio's own UI), only Clio's own UI can blank a
field again. This is fine for the tool's actual purpose (filling gaps), but means
`update_matter_field()` must only ever be called with a real, valid user id — never
attempt a "clear" write, it will appear to succeed and do nothing.

**Fixed attorney/paralegal roster, not "everyone with subscription_type X"**
(`client_assignment.ATTORNEY_NAMES` / `PARALEGAL_NAMES`) — Clio's `NonAttorney`
subscription bucket also includes non-paralegal staff (confirmed live: Dalinah
Espinoza, Heather Brown, and Ted Walker are all `NonAttorney` but none belong in the
Responsible Staff dropdown). The roster is three attorneys (Heidi Collier, Dahann
Bowers, Pamela Bradford) and three paralegals (Misty Sherman, Patricia Payne, Sandy
Cressey), resolved live by name against `clio_users.get_staff_directory()` — not
hardcoded ids, same reasoning `clio_users.py` itself was built for.
`get_assignable_users()` fails loud (raises) if a named person isn't found, or if an
"attorney" name isn't actually marked Attorney in Clio — a misspelled name here would
otherwise silently vanish from the dropdown instead of erroring.

**Scope — open matters only, any-field-missing** (both decided 2026-09-02, Ted):
matches the convention every other monitor in this repo uses (Trust Monitor, Staff
Unbilled, etc.), and a matter shows up on `/assignments` if it's missing even one of
the three fields — DOE, JANE is a real example (had Responsible Attorney set but not
the other two when this was built). Sorted alphabetically by matter (last name) —
changed 2026-09-16 (Ted) from an earlier most-incomplete-first grouping, which
scattered a client's own matters across different missing-count groups (e.g. Ann
Colton's two open matters, one missing 1 field and one missing 2, landing in separate
groups instead of next to each other).

**Server-side validation on every save** (`POST /assignments/set`) — the picked
`user_id` must actually belong to that field's roster (attorney roster for
`responsible_attorney`/`originating_attorney`, paralegal roster for
`responsible_staff`), checked again on the server even though the dropdown itself
only ever offers the right roster — confirmed live that submitting an attorney's id
for `responsible_staff` is rejected with a 400 rather than silently accepted.

**Instant-persist dropdowns, no confirm button** — same pattern as Collections'
Handling dropdown and Equalizer's inline editing: `onchange` posts immediately, the
row's own `<select>` elements are re-read client-side afterward to recompute that
row's "Complete"/"N missing" badge (no server round-trip needed for that, since every
field that can go missing already lives in the row's own selects).

**Print report** (`/assignments/report?view=missing|all`) — two buttons on the main
page, same "live pipeline rendered as a printable table" pattern as Collections'
action-report: `view=missing` lists only matters still missing a field, `view=all`
lists every open matter with its current assignments (`—` for anything still blank).
**No Client column** (removed 2026-09-08, Ted) — same reasoning as Collections' own
print report: every matter today has exactly one client (matter name = client name),
so listing both is redundant.

**Print cutoff, fixed 2026-09-08** — real bug, live-confirmed: with 5 columns (before
the Client column was dropped) `table.nowrap`'s `white-space:nowrap` made the table
wider than a printed page, and unlike a screen view (which just gets a scrollbar), a
printed page has no way to reveal the overflow — it silently clips, with no visible
sign anything was cut off. Exact same failure mode Collections' print report hit and
fixed once already (see `billing-monitors.md`'s "Long Handling text" note). Dropping
the Client column got it close (measured ~731px of intrinsic content width against a
~720px printable page — right at the edge, printer-margin-dependent), but the durable
fix is letting the Matter column specifically wrap on print
(`#report-table th:first-child, #report-table td:first-child { white-space: normal }`
inside `@media print`) since matter names vary the most in length ("ALDERMAN, KEVIN &
MARYANNE" vs. "COX, JOEY") while the other three columns draw from a small fixed
roster of short names. Verified live by simulating the print rule's effect and
re-measuring: intrinsic width dropped to fit within a 720px page with room to spare.

**Column order, changed 2026-09-21 (Ted)** — Originating Attorney now comes before
Responsible Attorney (then Responsible Staff), on the main table, the print report,
and the CSV export — was Responsible Attorney/Originating Attorney/Responsible
Staff before. `client_assignment.ASSIGNMENT_FIELDS` was reordered to match (only
cosmetic elsewhere — it just drives the "missing: X, Y" gap order in the CLI output
and iteration order, nothing that depends on a specific sequence).

**CSV export, added 2026-09-09 (Ted: wanted CSV export for any page with a
printable list)** — a "Download CSV" button next to Print, same 4 columns as the
table (Matter/Originating Attorney/Responsible Attorney/Responsible Staff),
respecting whichever `view` is currently on screen. `client_assignment.py`'s new
`write_report_csv()` writes to `output/client_assignment_report_{view}_{date}.csv`
on every page load (same "written as a side effect of rendering, served back by a
separate `/download` route" pattern Trust Monitor and Staff Unbilled already use),
keyed by `view` so switching between missing/all doesn't require a fresh page load
before downloading the other one. **Case Load below was explicitly excluded** from
this same request — it's a pie chart, not a list, so a CSV export wasn't judged
worth building for it.

**Excluded test/internal matters, added 2026-09-24 (Ted: "internal accounts and test
accounts")** — `EXCLUDED_MATTER_NAMES` (`{"DOE, JANE", "NON-BILLABLE, ADMIN"}`) is
checked in `fetch_matters_for_assignment()` itself, before anything downstream sees
the list — the single shared loader every `/assignments` route (`routes_
client_assignment.py`'s `_load()`) calls, so the exclusion applies everywhere at
once: the main "missing an assignment" table, the print report, the CSV export, and
Case Load's counts. DOE, JANE is this whole project's own designated
live-Clio-testing matter (root `CLAUDE.md`'s "Designated test matter" section) —
real writes get made to it routinely for testing across every subproject, which was
showing up here as "missing an assignment" like any other client matter. NON-
BILLABLE, ADMIN is an internal bucket, not a client matter — same one already
excluded from `soa_now_audit.py`'s own SoA/NoW checks for the same reason. Matched
against `display_number`, uppercased for the comparison (not relying on Clio always
returning it uppercase, just not assuming otherwise). Confirmed live: 176 open
matters came back with both names confirmed absent from the result.

## Close matter

**"Close" button per row, added 2026-09-21 (Ted)** — a fast way to cull the
`/assignments` list without leaving the page: `POST /assignments/close` PATCHes
`{"data": {"status": "closed"}}` (Clio's status enum is `open`/`closed`/`pending`,
confirmed against `reference/openapi.json`'s PATCH schema for `/matters/{id}.json`).
One-way from this tool, same posture as the assignment fields themselves — there's
no "reopen" button here; that's a Clio-UI action.

**Hard-gated on a live SoA/NoW filename check (Ted: "the only time a matter can be
closed [is when] a SoA or NoW [is] located in ... Pleadings")** — `GET
/assignments/close_check` (`client_assignment.find_soa_now_documents()` +
`evaluate_close_readiness()`).

**Rewritten 2026-09-22 — the original exact-folder-name version was live-broken on
real matters, caught while building a bulk report Ted asked for across ~50 matters
flagged to close.** The first cut matched only exact top-level folder names
("Correspondence", "Pleadings", "Conformed Copies"). Live-checking real matters
found this wrong on two axes: (1) the real folder is usually named **"OUR
PLEADINGS"**, not "Pleadings" (sometimes just "PLEADINGS", sometimes both, plus a
typo'd "CORRESONDENCE" seen live) — exact-name matching missed it on most matters,
meaning the gate was effectively always reporting "nothing found" and blocking
closes it should have allowed; (2) folders nest arbitrarily deep and inconsistently
per matter (GARCIA, LARISSA has a second, independent "PLEADINGS" folder nested
three levels down under a "DCSS" sub-folder, and "CONFORMED COPIES" nested *inside*
"OUR PLEADINGS" rather than beside it) — a top-level-only search missed real filed
copies sitting deeper. Most importantly, real matters also have an **opposing-party
folder** — "THEIR PLEADINGS AND CORRESPONDENCE", "THEIR DOCUMENTS", "OP RFO 2023"
("OP" = Opposing Party, same abbreviation `equalizer/clio_parties.py`'s OC/OP lookup
uses) — that a naive substring match on "pleadings" would have wrongly counted as
our own filed copy; confirmed live on COURTLAND, KELLY, whose only SoA/NoW match in
the whole matter was the *other side's* filed Substitution of Attorney, sitting in
"THEIR PLEADINGS AND CORRESPONDENCE - Copy".

Fixed by fetching the matter's **entire folder tree** in one call
(`_fetch_matter_folder_tree()` — `GET /folders.json?matter_id=X` with no
`parent_id`/`scope` returns every folder for the matter flat, with parent links,
confirmed live it goes arbitrarily deep in one page) and **every document** in the
matter the same way, then classifying each SOA_NOW_PATTERN match by walking its full
folder ancestry (`_classify_folder_path()`) against three prefix patterns, checked
in this order:
- `OPPOSING_NAME_PATTERN` (`^their\b|^op\b|^opposing\b`) → classification
  `"opposing"`, checked first and short-circuits the others — a filed-looking name
  nested under an opposing-party folder never counts as ours, at any depth.
- `FILED_NAME_PATTERN` (`^(our\s+)?pleadings\b|^conformed\s+copies\b`) →
  `"filed"` — matches "PLEADINGS", "OUR PLEADINGS", "CONFORMED COPIES" at any
  ancestor depth, deliberately anchored at the start of the name so "THEIR
  PLEADINGS..." can never match this pattern regardless of the opposing check above.
- `CORRESPONDENCE_NAME_PATTERN` (`^corr`) → `"correspondence"` — loose on purpose
  (also catches the "CORRESONDENCE" typo seen live); only used for the "drafted, not
  filed" message, never gate-determining, so the looseness carries little risk.
- Anything else that matched SOA_NOW_PATTERN but none of the above → `"other"` —
  still surfaced to the human (e.g. a document sitting loose in the matter root), just
  doesn't satisfy the gate on its own.

**Filename itself is a second signal, layered on top of folder classification —
added 2026-09-22 (Ted gave the firm's file-naming convention and asked to rerun the
bulk report to check for differences).** The firm's own standard,
`YY.MM.DD Party.DocType.Description.Status` (e.g. `25.07.15 CL.SOA.Conf.pdf`), when a
filename follows it, states both whose document it is (`CL`/`OP`/`OC`/`CT`/`AT`/`TP`)
and its filing status (`Exec`=signed only, `Conf`=confirmed/filed, `Rec`=received
only, `Draft`) independent of which folder it's sitting in — and folder placement
alone can be wrong. Live-confirmed on HOANG, JENNIFER: `26.01.06 OP.SOA.Exec.pdf`
sits in a plain "PLEADINGS" folder (which folder classification alone calls
"filed"), but its own filename says it's the **opposing party's** copy and only
**signed, not filed** — this matter doesn't split "PLEADINGS" into separate
our-side/their-side subfolders, so folder location alone can't tell them apart.
`_parse_naming_convention_tokens()` splits a filename on whitespace/`.`/`_`/`-` and
looks for a whole token matching a party or status abbreviation;
`_refine_classification()` combines that with the folder classification, and can
only ever *downgrade* a "filed" folder classification, never upgrade a non-filed
one (an absent or unrecognized token means "the filename doesn't say," not
"confirmed ours and filed"):
- An explicit `OP` party token → always `"opposing"`, full stop, regardless of
  folder or status — confirmed live this catches a real case folder-only
  classification got wrong (KENDRO, JILL's `26.04.03 OP.SOA.Conf.pdf` sits in
  "PLEADINGS" and even says `Conf`, but it's the *opposing party's* confirmed
  filing, not the client's).
- An explicit `Exec`/`Rec`/`Draft` status where the folder said "filed" →
  downgrades to a new classification, `"unfiled"` — the document is explicitly
  declaring itself not filed yet, no matter which folder it's in.
- Anything else → the folder classification stands unchanged.

Rerunning the full ~50-matter bulk report after this change found **zero matters
where the overall closeable/not-closeable verdict flipped** — every matter that had
a genuinely-filed `CL...Conf` match also kept at least one after the more precise
check — but did correct several individual *findings* that were previously
mislabeled "filed" (the HOANG and KENDRO cases above, plus a similar case on REED,
BRITTNEY and VISWANATHAN, VIDYA), meaning the reasoning shown to staff is now more
trustworthy even though this round of results happened not to change any actual
close/no-close decision.

**429 retry added to these bulk-read calls, 2026-09-22** — the folder-tree and
document fetches (`_fetch_matter_folder_tree()`, `find_soa_now_documents()`,
`matter_has_any_documents()`) had no rate-limit retry at all, unlike
`update_matter_field()`'s PATCH — a real gap against this project's own stated
safety rule ("Retry on rate limit (429)"), only surfaced because the bulk report
below makes enough back-to-back calls in one run to actually hit Clio's limit
(confirmed live: several matters errored with `429 RateLimited` partway through a
~50-matter run). Fixed with a shared `_get_with_retry()` helper (same
`RETRY_DELAYS = [5, 15, 30]` backoff as the PATCH path) used by all three.

`find_soa_now_documents()` now returns `{"name", "path", "folder_id", "classification",
"folder_classification", "party_token", "status_token"}` per match — `path` is the
real folder breadcrumb (e.g. `"DCSS > PLEADINGS"`) built from the ancestry walk, this
is also what answers "show me the path" for the bulk report below; `folder_id`
(added 2026-09-22 for `soa_now_audit.py`, see `reference/soa-now-audit.md`) is the
document's immediate containing folder id, for building a direct
`document_management?folder_id=...` link into Clio;
`folder_classification`/`party_token`/`status_token` are kept alongside the final
`classification` so a human (or a report) can see *why* it landed where it did, not
just the end result. `evaluate_close_readiness()` still reduces this to `can_close`
(true only if any match classified `"filed"`), `filed`, and `unfiled` (everything
else, including the new `"unfiled"` classification). Matching itself
(`SOA_NOW_PATTERN`) is unchanged from the 2026-09-21 widening: the
bare abbreviation ("SOA", "NOW", `\b`-wrapped so they don't match inside ordinary
words like "know" or "renowned"), the shorthand phrase ("Sub of Atty" / "Sub. of
Atty."), or the full term ("Substitution of Attorney" / "Notice of Withdrawal").

Three outcomes, only one of which allows the close to proceed without the override:
- **A "filed" classification exists** → closeable. The modal lists the filed
  document(s) and their path, and a "Close Matter" button appears — the only path
  that reaches `POST /assignments/close` without `override=true`.
- **Something matched, but nothing classified "filed"** → blocked. The modal shows
  each match's classification and path (e.g. "opposing", "correspondence") so staff
  can see why it didn't count.
- **Nothing matched at all** → blocked with a plain "not found" message. If the
  matter also has **zero documents in Clio at all**
  (`client_assignment.matter_has_any_documents()`, only checked in this
  empty-findings case to save the extra API call), the modal adds a note that the
  matter's files may still be on the legacy Y: drive — confirmed as a real, common
  pattern across the bulk report (26 of 52 matters checked had zero Clio documents).

**Still just a filename search, not proof of anything actually filed with the
court** — a "filed" classification doesn't guarantee the document is genuinely a
filed SoA/NoW, and the reverse doesn't guarantee nothing was filed under a folder or
filename this pattern doesn't recognize. **Re-checked server-side in
`/assignments/close` itself** (same "validate again on the server, don't just trust
the client" posture as `/assignments/set`'s roster check) whenever `override` isn't
set — a request that reaches that route without a live "filed" match and without
`override=true` gets rejected with a 400, not just gated by the modal's own JS.

**"filed" vs. "prepared" split, added 2026-09-24** (Ted, reviewing `/soa-now-audit`'s
green badge on WELLS, BRITTNEY's 4 matches — all bare "OUR PLEADINGS", no Conformed
Copies, no `.Conf` token: "these are not filed with the court"). Before this, a bare
match in "Pleadings"/"Our Pleadings" and a match in "Conformed Copies" were both just
`"filed"` — but a document sitting loose in Our Pleadings only proves we
drafted/lodged it, not that the court actually has it; Conformed Copies specifically
holds the court-stamped copy, which is real evidence. Now the folder walk
distinguishes them: `CONFORMED_NAME_PATTERN` (`\bconformed\b`, matched anywhere in
the folder name via `.search()`, not prefix-anchored via `.match()` — widened
2026-09-24, see below) → `"filed"`; a bare
`FILED_NAME_PATTERN` match (now just `^(our\s+)?pleadings\b`, Conformed Copies split
out of it) → the new `"prepared"` classification — found, drafted, not gate-passing
on its own. The naming-convention refinement (`_refine_classification()`) now works
both directions on this tier: an explicit `.Conf` status token upgrades "prepared" to
"filed" (a court-stamped copy doesn't stop being one just because it's loose in Our
Pleadings instead of a Conformed Copies subfolder), while Exec/Rec/Draft still
downgrades either to `"unfiled"`, same as before.

**Real impact, checked live 2026-09-24 against the same ~50-matter list this whole
feature was built against:** the 7 already-closed matters were unaffected (each had
at least one genuine Conformed-Copies-or-`.Conf` match surviving the stricter rule),
but of the 3 matters still open at the time, all 3 — PAUP, LAURA; VISWANATHAN, VIDYA
(closed by the time of this check, but would have flipped too); WELLS, BRITTNEY —
lost their "filed" status: none of their matches were Conformed Copies or carried an
explicit `.Conf` token. The Y: drive side of `/soa-now-audit`'s check (see
`reference/soa-now-audit.md`) took an even bigger hit: of the original 10 matters
showing "Filed on Y: drive," only 2 (HUTMACHER, SARAH; KOBS, MAUREEN) had a genuine
Conformed Copies match on Y: — the other 8 were all bare "OUR PLEADINGS" and dropped
to "prepared."

**Plain-English "filed" word added as a second upgrade signal, same day** (Ted:
"filed forms could also have the word filed in the name") — `FILED_WORD_PATTERN`
(`\bfiled\b`, case-insensitive) is deliberately separate from the formal
Party.DocType.Status convention (it's not a coded token, just someone writing what
happened) but treated as equally strong evidence in `_refine_classification()`:
with no formal status token present, a "prepared" match whose filename contains the
standalone word "filed" upgrades to "filed" too. Only applies when there's no formal
status token to defer to instead — an explicit Exec/Rec/Draft still wins. Real
examples that motivated this, all from the Y: drive side of the 8 matters that had
just dropped to "prepared": "NOW filed 4.18.23.pdf" (HUERTA), "NOW filed
11.18.22.pdf" (LIEURANCE, RUSSETH), "NOW filed 12.1.23.pdf" (MICHEL), "NOW FILED
12.22.22.pdf" (OBRIEN) — 5 of the 8 recovered "filed" status this way; LAWLER,
MIKELS, and SMITH VANESSA ANN genuinely have no such wording in any match and
correctly stayed "prepared." WELLS, BRITTNEY (the match that started this whole
correction) has no "filed" word in any of its 4 filenames either and correctly
stays excluded — confirmed live this doesn't regress the original fix.

**`CONFORMED_NAME_PATTERN` widened from prefix-anchored to anywhere-in-name,
2026-09-24** — found via a one-off exploratory scan (see "Reverse scan" below), not
a bug report: the original `^conformed\b` (checked via `.match()`, so only a folder
whose name *starts* with "conformed") missed CARTER, DARLENE's genuine filed copy,
sitting in a folder named "OUR PLEADINGS > COURT CONFORMED COPIES" — "conformed"
isn't at the start there. That one still classified "filed" correctly, but only by
luck — its filename also happened to contain the word "filed," triggering the
separate word-signal upgrade instead of the folder match. Widened to `\bconformed\b`
via `.search()` (matches anywhere in the name) — confirmed live this now classifies
CARTER, DARLENE's match as "filed" via the folder itself
(`folder_classification: "filed"`), not just via the word-signal rescue. Judged safe
to widen (unlike the prefix-anchored `FILED_NAME_PATTERN`/`CORRESPONDENCE_NAME_PATTERN`/
`OPPOSING_NAME_PATTERN`, deliberately left anchored so a folder like "THEIR PLEADINGS
AND CORRESPONDENCE" doesn't get "filed"/"correspondence" credit just for containing
those words mid-name) — "conformed" is specific enough a legal term that matching it
anywhere in a folder name carries negligible false-positive risk.

**Reverse scan, 2026-09-24 (Ted: "reverse the script and find open matters that
should be closed... let's do this on a scratchpad... I want to see if it's worth
it")** — a one-off, uncommitted scratch script (not a shipped tool) that ran the
same classification across **every currently open matter** (175, after excluding
DOE JANE/NON-BILLABLE) instead of a hand-picked list, looking for any that already
have a genuinely filed SoA/NoW despite still being open. Result: **27 of 175** did —
and **26 of those 27 were never surfaced by Client Assignment's own "missing
Responsible Staff" heuristic at all** (only 1 overlapped with the existing
`soa_now_audit.py` hand-picked list). Spot-checked 6 at random, all held up as real
evidence, including one three folder levels deep under a sub-case folder that the
full-tree walk correctly found. This is the finding that justifies building
`soa_now_audit.py`'s deferred "live query instead of a hardcoded list" direction
(see that file's own module docstring) — "missing staff" and "has a filed SoA/NoW"
are almost entirely different matter populations, so a real periodic audit tool
needs to scan broadly, not extend the hand-picked list forever. Not yet built as of
this writing — the scan lives only in a scratchpad, kept here as the record of why
it's worth doing.

**One modal for the whole flow, not `alert()`/`confirm()`, added 2026-09-21** —
originally built on plain browser dialogs, revised the same day so the status message
could carry a real link: `#close-modal-overlay` (in `client_assignment.html`) shows the
check-in-progress state, then the outcome message, and always an **"Open this matter's
Documents in Clio"** link (`{{ clio_base_url }}/nc/#/matters/{matter_id}/document_management`
— confirmed live 2026-09-21 by clicking into the Documents tab on the designated test
matter, DOE JANE, and reading the resulting URL; this is the correct deep link to a
matter's document listing, opens directly with no intermediate click needed) so staff
can go verify in Clio without leaving the check. `confirm()` is still used, but only as
a final "are you sure" gate on the two buttons that actually close something (Close
Matter, Close Anyway), not to display the status itself.

**"Close Anyway" override, added 2026-09-21 (Ted)** — a button inside the same modal
(shown only once the check comes back blocked) that skips the filed-folder check
entirely (`POST /assignments/close` with `override=true`). Exists because the filename
scan is real but not exhaustive — a genuine filing can still be missed (different
wording than `SOA_NOW_PATTERN` covers, a scanned image with no searchable filename,
filed somewhere the scan doesn't look, or the file is only on the legacy Y: drive) and
staff need a way through that isn't "rename a file in Clio to satisfy a regex." Still
gated by its own `confirm()` (states plainly that it's skipping the check), and every
override is logged at `logging.WARNING` (`"Matter {id} closed via override —
SoA/NoW-in-Pleadings check was bypassed"`) rather than the ordinary-close `INFO` level,
specifically so a bypass stands out in the log file rather than blending into routine
closes — matches this project's Development Philosophy #1 (auditability): the check
being skippable doesn't mean the skip itself goes unrecorded.

## Case Load

**`/assignments/caseload`** (button next to the two print buttons, 2026-09-03) — case
count per person, as a pie chart + legend, for **Responsible Attorney** and
**Responsible Staff** separately. **Originating Attorney is deliberately excluded**
(Ted) — confirmed live it's almost entirely Heidi Collier in practice (163 of 226 open
matters, vs. 3 for Dahann Bowers and 59 unset), so a chart of it wouldn't tell staff
anything they don't already know.

**Hand-rolled inline SVG, no chart library** (`routes_client_assignment.py`'s
`build_pie_chart()`) — this app has no Node/npm toolchain and no chart library
anywhere else in the repo, so slice geometry (arc `M`/`L`/`A` path data) is computed
in plain Python and rendered as static `<path>` elements; a single 100%-share slice
is special-cased into two semicircle arcs since a full 360° sweep degenerates to a
zero-length arc otherwise.

**Colors, per the project's dataviz skill:** the first three slots of its validated
default categorical palette (`#2a78d6` blue / `#eb6834` orange / `#1baf7a` aqua),
assigned in fixed roster order — validated via the skill's palette validator against
this app's white panel surface (all CVD/lightness/chroma checks pass; aqua's contrast
against white lands in the WARN band, mitigated by the legend's always-visible
count/percent labels, which the skill treats as sufficient "relief" for that warning).
**"Unassigned" gets the palette's muted gray (`#898781`) instead of continuing the
categorical sequence** — deliberately: it isn't a person, so it reads as a coverage
gap (the same "badge the gap" convention as Collections' trust-request badges), not a
fourth team member. Every count is direct-labeled in the legend (exact count + percent
per person, including a roster member with zero matters), so the pie is never the only
source of the numbers, in-line with the skill's guidance for a part-to-whole chart
compared at a glance.

## Workflow
```powershell
# Read-only report to stdout (missing assignments only) + a log file under logs/ — no CSV output
uv run src/client_assignment.py
```
Or just visit `/assignments` — same live pipeline, rendered as an editable table.
