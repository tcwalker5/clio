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
the other two when this was built). Sorted most-incomplete-first, then alphabetically,
so the matters needing the most attention surface at the top.

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
