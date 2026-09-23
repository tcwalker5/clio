> Not auto-loaded — `soa_now_audit.py` is a flat file in `src/`, no subfolder to hang
> a per-directory CLAUDE.md off of. Open this yourself before touching it.
> Root map: `../CLAUDE.md`.

# SoA/NoW Close Audit

**Modules:** `src/soa_now_audit.py`, dashboard page at `/soa-now-audit`
(`src/web/routes_soa_now_audit.py`, `src/web/templates/soa_now_audit.html`) — built
2026-09-22/23 (Ted).

**Purpose:** verify a filed Substitution of Attorney or Notice of Withdrawal exists
(Clio first, then the legacy Y: drive if Clio has nothing) for a fixed list of old
matters Client Assignment surfaced as missing a Responsible Staff assignment — Heidi
Collier's book, flagged 2026-09-22 with "all of these should be closed" — then close
them. Grew out of the Close Readiness bulk report built the same day for
`client_assignment.py`'s Close button (see `reference/client-assignment.md`'s "Close
matter" section for the underlying document-matching/classification logic this reuses
rather than duplicates: `SOA_NOW_PATTERN`, folder classification, naming-convention
token refinement).

**Deliberately standalone from Client Assignment (Ted, 2026-09-22): "This page should
be exclusive to that and not concern about case assignments."** No Responsible
Attorney/Originating Attorney/Responsible Staff concept anywhere on this page — it
only cares about SoA/NoW evidence and closing. **Close Case is never gated on
anything this page finds** — confirmed explicitly (Ted, 2026-09-23): "some cases
won't require a NoW or SoA. So being able to close without this is OK." This is a
checklist for staff to work through by hand, not an automated gate like
`/assignments`' own Close button.

**Hardcoded matter list, not a live query (Ted, 2026-09-22: "for now, just the hard
coded list")** — `MATTER_NAMES` is the same fixed ~52 names from the Close Readiness
report. Ted named this as a likely-future direction ("this might be converted to
audit our SoA and NoW periodically") but explicitly deferred it — not built as a live
all-open-matters query yet. If that's ever picked up, `MATTER_NAMES` needs to become
a real query and the whole "one-time list" framing in this doc revisited.

**`match_matters()` resolves against any matter status** (`open,pending,closed`), not
just open — so a matter closed since the audit was last run shows up with a "Closed"
badge instead of silently vanishing with no explanation. Name matching mirrors the
Close Readiness report's own logic: exact `display_number` match first, then
last-name-only fallback, with an explicit ambiguity check (multiple same-name matters
→ prefer the one that isn't closed if exactly one qualifies, otherwise report
`"ambiguous"` rather than guess).

## Y: drive fallback

**Only checked when Clio has zero SoA/NoW matches for a matter** (`run_audit()`) —
searches `Y:\Client Files\{letter}\{Last[, First]}` (Ted gave this exact path
convention 2026-09-22) using the same `SOA_NOW_PATTERN` filename search and folder
classification (`OPPOSING_NAME_PATTERN`/`FILED_NAME_PATTERN`/
`CORRESPONDENCE_NAME_PATTERN`) as the Clio side — **without** the naming-convention
party/status token refinement, since Ted was explicit these are legacy files that
predate that convention entirely ("All of these are legacy and will not use the new
naming convention").

**Same-last-name folders are genuinely ambiguous on Y: — refuses to guess
(`_find_y_drive_candidate()`).** Real folders found live: `OLSON`, `OLSON, C`,
`OLSON, LACEY`, `OLSON, LAURA` all exist as siblings — none of them "OLSON, SONJA."
Only resolves to a specific folder when (a) exactly one same-last-name folder exists
at all (even bare, no comma — confirmed live this matters: `FRANK, LINDA`/`KEYSER,
JESSICA`/`KOBS, MAUREEN` each have exactly one same-last-name folder with no first
name in it at all, and an earlier stricter version wrongly rejected all three), or
(b) more than one exists but exactly one's own first-name token (`_first_token_of_suffix()`)
exactly matches the target's first name. Anything else reports `"ambiguous"` (listing
every same-last-name folder found) rather than attributing a different client's
document to the wrong matter — this exact bug was caught live before shipping (a
naive substring check on `"VANESSA ANN"` failed to narrow `SMITH, VANESSA ANN` among
11 same-last-name `SMITH...` folders because the real folder only has "VANESSA", not
"VANESSA ANN").

**Links, not literal "open" — browser file:// blocking, found live 2026-09-23.**
First cut generated `file://Heidiofficenas/shared/Client%20Files/...` links (UNC-based
off `net use Y:`, confirmed live it resolves to `\\Heidiofficenas\shared`, so the
link works regardless of which drive letter a given staff machine happens to map the
share to). **Ted reported these didn't work for any matter, without exception** —
consistent with Chrome/Edge's security policy blocking top-level navigation to
`file://` links clicked from a plain `http://` page (this dashboard is
`http://cap.lan`, not HTTPS), not a per-path bug. Fixed by replacing the links with a
**"Copy path" button** (`copyPath()` in `soa_now_audit.html`) that copies the plain
Windows path to the clipboard via the legacy `document.execCommand("copy")` — not
`navigator.clipboard.writeText()`, which requires a secure context (HTTPS or
localhost) this LAN-only HTTP dashboard doesn't have and would be silently
unavailable. Shown for both the client's whole root folder (always, regardless of
whether a specific file was found — "if no documents found can we open up the root
file of the client on the Y drive") and each individual finding's own enclosing
folder (`"a link to open each enclosing folder for any document found"`).

## Close Case

`POST /soa-now-audit/close` calls `client_assignment.close_matter()` directly — same
underlying PATCH `{"data": {"status": "closed"}}`, no additional gate. **No
confirmation dialog** (removed 2026-09-23, Ted: "I know what I'm doing and that it
can be reopened in clio") — clicking closes immediately.

**Real bug, fixed 2026-09-23 — the button got stuck on "Closing..." even after a
genuinely successful close.** `closeCase()`'s post-success DOM update used
`button.closest("[data-matter-id]")` to find the card to update — but the button
itself also carries `data-matter-id` (needed to read which matter to close), and
`.closest()` checks the element itself first. So it resolved to the button, not the
outer `.panel` card; `querySelector` on a `<button>` (no `.badge`/div children) always
found nothing, so the success-path code silently did nothing and left the button
disabled on "Closing..." forever, even though the Clio close had already succeeded.
Fixed by scoping the selector to `.panel[data-matter-id]`.

## Mark SoA/NoW Filed

**Writes directly to Clio's own "NoW or SoA filed" matter custom field** (checkbox
type) — revised 2026-09-23 (Ted: "I just want the custom field to be marked") from an
original version that kept its own local `soa_now_audit_marks` SQLite table instead,
which nothing outside this dashboard could see (dropped same day, table doesn't exist
anymore). **This field already existed on real matters** (confirmed live,
`custom_fields.json`) — this tool didn't invent it; e.g. KOBS, MAUREEN already had a
pre-existing `checkbox-1127268708` CustomFieldValue record, value `false`.

**Same "check for an existing CustomFieldValue id first" pattern as
`moore_marsden/clio_matter_dates.py`'s `update_matter_dates()`** (see that module's
own docstring for the full gotcha writeup) — `set_soa_now_filed()` PATCHes the
existing record's own id when one exists (`{"id": existing_id, "value": marked}`),
falling back to `{"custom_field": {"id": ...}, "value": marked}` only if the matter
genuinely has no record yet. PATCHing with a bare `custom_field{id}` against a matter
that already has a record 422s with "custom field value ... already exists" — same
class of bug this project already hit and fixed once for the Date of
Marriage/Separation fields.

**Clio is the source of truth, no local copy** — `match_matters()` fetches
`custom_field_values{id,field_name,value}` alongside every matter in the same batched
query already used for name/status, and every page load reads the live value fresh.
This means the checkbox's checked state can never drift from what's actually in Clio,
at the cost of the two earlier local-DB marks (KENNEDY, KATHERINE and HUTMACHER,
SARAH) not carrying over — those need re-checking on the page once to land in Clio
for real.

**Real bug, fixed 2026-09-23 — a confirmed-persisted mark appeared to vanish/reset
once its matter was closed.** The template originally hid the entire controls block
(checkbox included) once `m.status == "closed"`, on the assumption nothing more was
needed once closed. Live-diagnosed: KOBS, MAUREEN's mark was genuinely saved
(confirmed by reading the local table directly at the time), but the matter had also
been closed, so the whole block — checkbox and all — disappeared from view, making it
look unchecked/lost even though the data was intact. Fixed by only conditionally
hiding the Close Case button on closed status; the "Mark SoA/NoW Filed" checkbox and
"Open matter" link now always render regardless of status. The matching JS fix:
`closeCase()`'s success path now removes only the Close Case button
(`button.remove()`), not the whole controls div, so the checkbox stays live and
clickable after a close.

**Still deferred (Ted, 2026-09-22): a richer version that also posts a Clio Note
citing the exact filename/path in the same click** — parked, not built. If picked
back up, start from this section rather than re-deriving the custom-field-vs-Note
tradeoff (custom field needs no admin setup since it already exists and is what Ted
asked for directly; a Note would additionally need the confirming staff member's name
typed in, since CAP has no per-user login).

## 429 retry

**Added 2026-09-23** to `client_assignment.py`'s `_get_with_retry()` (already covered
in `reference/client-assignment.md`) after a live ~50-matter bulk run hit real 429s
partway through — this page's own per-matter Clio calls (folder tree + documents,
`find_soa_now_documents()`) already inherit that fix since they're the same shared
functions.

## Workflow

Dashboard-only, no CLI equivalent — visit `/soa-now-audit`, expect roughly a minute
to load (a couple of Clio API calls per still-open matter, plus a Y: drive filesystem
walk for any with nothing in Clio — no caching yet, every page load re-runs the whole
check from scratch). For each matter: read the findings/paths shown (Clio folder
links via `document_management?folder_id=...`, Y: drive paths via the "Copy path"
button), open and read the actual document, then Mark SoA/NoW Filed and/or Close Case
as appropriate — neither blocks the other.
