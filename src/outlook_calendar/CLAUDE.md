> Auto-loaded by Claude Code when working in `src/outlook_calendar/`. Root map: `../../CLAUDE.md`.
> Covers the flat orchestration scripts too: `outlook_auth.py`, `outlook_migration.py`,
> `outlook_migration_tag.py`, `outlook_exceptions_availability.py`,
> `outlook_recurring_availability.py` (all live directly in `src/`, not this folder —
> Claude Code won't auto-load this file for them, so if you're asked to touch one of those
> and this file isn't already in context, open it yourself first).

# Outlook Calendar Migration

**Modules:** `src/outlook_auth.py`, `src/outlook_calendar/`, `src/outlook_migration.py`

**Purpose:** Migrate Heidi Collier's Outlook calendar into Clio — the calendar of
record since June 1, 2026 (`data/clio-matters.csv` and the Clio account both start
there). Started as a one-time backfill; now run on an ongoing basis (re-run
periodically with an updated `--to-date`, including future-scheduled events) since
Outlook is still where hearings/calls get entered day to day.

**How it writes to Clio:** it doesn't, directly. This script only *reads* Outlook
(Microsoft Graph) and Clio (matters + existing calendar entries, to skip anything
already migrated). All writing happens when the generated CSV is imported through
Clio's own UI (Settings > Data Import > Calendar Events) — chosen over the API
specifically because Clio's import can be undone from there if something's off; a
bulk API-created batch can't be undone that easily.

**Auth:** reuses `calendar-check`'s existing Azure app registration and its exact
registered redirect URI (`http://localhost:3020/api/auth/callback`) — no Azure Portal
changes needed, just a one-time browser consent:
```powershell
uv run src/outlook_auth.py             # first time (browser flow)
uv run src/outlook_auth.py --refresh   # token expired
```
Scope is `Calendars.Read` only — nothing here writes back to Outlook.

**Matching — two event types, two paths (`outlook_calendar/event_parser.py`):**
- **Hearings** (FRC, RFO, MSC, etc.): reuses `court_calendar/normalizer.py`'s
  `extract_party_name()`/`party_names_match()` (already built for Outlook-subject-style
  text) and the existing `purpose_mappings` table, same as the court calendar sync.
- **Calls** (`TCON`/`OCON`): a deliberately separate, simpler path per project
  decision — no purpose_mappings involved. Recognizes `TCON`, `OCON`, `TCN` (typo
  variant), and `T/C`/`T-C`/`T.C.` in two subject conventions: `"NAME TCON"` (party
  immediately precedes the marker) and `"Last, First- [staff] t/c w/CL"` (party
  precedes the first dash). **Bare `"OC"` is deliberately NOT treated as a call
  marker** — real-data check found it almost always means "Opposing Counsel"
  (e.g. `"SATTERLY OC's Responsive Dec due"`), not "office conference"; only the
  unambiguous forms above are recognized. Hearings are tried first; calls are a
  fallback only when no hearing-style match is found.
- Both paths resolve to a Clio matter via `matter_matching.index_by_last_name()` — the
  same shared lookup Printer Expenses/Bradford Invoice Import/Court Calendar Sync use.
  Unlike the court calendar sync, there's no case number here to disambiguate a client
  with two open matters — those go straight
  to the exceptions file.
- **Known limitation:** the underlying substring party-matching (shared with the rest
  of the project) can mismatch when one client's exact last name is a literal substring
  of another client's compound name — e.g. an event for "NIALEA ORTEGA" incorrectly
  matched client "ORTEGA, LAURA L." instead of "ORTEGA-GUACHENA, NIALEA". Rare, but a
  reason to actually read the CSV before importing, not just skim it.

**Skips Dahann's and Pam's events entirely**, via Outlook category color (not text
parsing) — `SKIP_CATEGORIES` at the top of `outlook_migration.py` maps `"Purple
category"` -> Dahann and `"Green category"` -> Pam, confirmed against real subject
text. Extend that dict if another color convention shows up.

**OP/CL — RFOs only.** Whether an event concerns the Opposing Party or our Client
can't be reliably derived from the Outlook data, and only matters for RFOs (it
indicates who's asking for the order) — every RFO title has a literal `OP/CL`
placeholder for a paralegal to resolve by hand (search/replace in Excel); every
other purpose omits that token entirely rather than carrying a meaningless one.

**Event title format:** `{client last[, First Initial]} {purpose} [{OP/CL}] {Time}`, e.g.
`WELLS RFO OP/CL 9:00 AM` (RFO) vs `LARSEN FRC 9:00 AM` (no OP/CL). Missing purpose
shows as `?` rather than being silently dropped. Client last name comes from the
matched Clio matter's own `display_number` (not whatever text was parsed off the
Outlook subject — that can attach the wrong first name to a shared last name, e.g.
a "BONNIE LARSEN" call actually matching client LARSEN, NOEL). Widened to `Last, F`
(first initial) only when another *open* matter shares that last name — otherwise
the bare last name is enough and matches the existing hearing-title convention.

**Department moved to the description line, not the title.** Department (courtroom)
doesn't apply to calls (TCON/OCON) at all, so a `?` placeholder in the title couldn't
distinguish "not parsed" from "not applicable." Instead, `build_export()` appends
`— {code}` to the description only when a department was actually found, normalized
to `LETTER-NN` (e.g. `N-19`, `D-10`) or a bare number for Central (`601`) via
`normalizer.normalize_dept()` — no "Dept" label, the code alone is self-explanatory
to staff. The raw `location` column is left as Outlook reported it (phone number for
calls, courtroom text for hearings) — unrelated to the normalized dept in the
description.

**OC/OP exception review (`outlook_calendar/relationships.py`):** matter_matching.py
only indexes Matters, so a TCON/OCON call with Opposing Counsel or an unrepresented
Opposing Party always lands in exceptions — Clio doesn't know that name as a *client*.
But Clio does already track both roles as Relationships (Contact <-> Matter, with a
free-text `description` like "Opposing Counsel"/"Opposing Party" — real taxonomy check
found ~200 of these at this firm, plus inconsistent variants like "Atty for Opposing
Party", matched with a regex, not an exact-string set). After writing the exceptions
file, the migration fetches `/relationships.json`, filters to OC/OP-flavored
descriptions, and cross-references each exception's party/subject text against those
contact names — one-directionally (contact name found IN the text, not the reverse):
the reverse direction produced two real false-positive classes in testing (short
leftover fragments from a failed party-name extraction trivially substring-matching
any long contact name; a bare client last name shared with the opposing party, e.g.
hearing "ROJAS FRC" falsely matching OP contact "ANEL ROJAS"). Runs across **all**
matter statuses, not just open — a closed matter can still have old Outlook calendar
history worth flagging (surfaced the firm's WELLS, BRITTNEY matter this way, invisible
to the regular open-matters-only client match). Report-only: nothing here is
auto-imported, and when a text matches more than one distinct (contact, matter) pair,
every candidate is listed in `note` rather than silently picking one.

**Calls report + interactive resolution (`outlook_calendar/calls_report.py`,
`interactive_resolve.py`, `call_overrides.py`):** every TCON/OCON call *and* DUE
deadline reminder (see below) the migration finds — regardless of outcome — also
gets written to its own review CSV (`_calls.csv`) so they can be visually compared
as one set instead of picked out by eye from the general exceptions file. Anything
still unresolved after the client-matter and OC/OP passes drops into an interactive
terminal prompt (runs automatically, not behind a flag): type part of a name to
search open matters, pick a number, or press Enter to skip. Events are grouped by
extracted party text first, so someone who called twice in the date range is only
asked about once. Every decision — matched *or* skipped — is persisted to
`data/outlook_call_overrides.csv` (gitignored, like the other `data/*.csv` inputs)
keyed by that party text **the instant it's made**, not batched until the whole
session finishes — Ctrl+C (or Ctrl+D/EOF) partway through only loses whatever
wasn't answered yet, not answers already given (verified: a KeyboardInterrupt or
EOFError mid-session is caught, doesn't crash the script, and lets the caller still
write every other output file for that run). Reloaded at the top of every future
run alongside `MANUAL_MATTER_MAP` — so a wider `--to-date` re-run (the normal
periodic workflow) doesn't re-prompt for someone already resolved, and a matched
party now also auto-resolves for *hearings*, not just calls, since it's merged into
the same lookup. Skipped entirely (with a warning, everything left unresolved) when
stdin isn't interactive — guarded by `isatty()` plus the same `EOFError` catch,
since `isatty()` alone wasn't reliable in every shell tested (returned `True` even
with stdin redirected from `/dev/null` in one test).

**DUE deadline reminders (purpose_code `"DUE"`, `event_parser.extract_due_event()`):**
a third fallback tier after hearings and calls — any subject containing "due" as a
whole word (e.g. `"WATERMAN OP'S RESPONSE TO DIVORCE DUE BY TODAY"`,
`"Metros docs due today"`). Party is read off the subject's leading word (there's no
purpose code to anchor on like hearings/calls have) — works for ~10 of 11 real
examples; the one miss was a name placed *after* "ON" instead of at the start
(`"RESPONSIVE DEC DUE ON PRECIADO"` extracted "RESPONSIVE") — that one correctly
falls through to the same manual-resolution prompt as any other unmatched event
rather than silently mismatching. **Always imported as an all-day event**
(`csv_export._is_all_day()`) regardless of what time Outlook recorded — a deadline
note has no meaningful clock time, and importing it at whatever time a reminder
happened to be set for would misrepresent it as the actual deadline time. Shares
the exact same review CSV, interactive prompt, and `call_overrides.py` persistence
as TCON/OCON calls (see above) — nothing deadline-specific there.

**Event type = Heidi:** Clio has a `calendar_entry_event_type` concept already in use
at this firm (types named "Heidi", "Dahann", "Pam", "Staff" — ids from
`/calendar_entry_event_types.json`, currently Heidi = `591618`). The CSV import has
no column for this, so it's a separate follow-up step, `src/outlook_migration_tag.py`
— run it *after* you've imported the CSV in Clio. It re-queries Clio for the same
matter+date and matches the summary text verbatim against your CSV to find the exact
entries it just created (not a "created recently" heuristic, which could tag someone
else's entry), then PATCHes each one's type to Heidi.

**Outputs:**
- `output/outlook_migration_{from}_{to}.csv` — ready to import into Clio as-is (after
  resolving `OP/CL` placeholders on RFOs)
- `output/outlook_migration_{from}_{to}_exceptions.csv` — unmatched/ambiguous events
- `output/outlook_migration_{from}_{to}_oc_op_candidates.csv` — exceptions that are
  likely calls with Opposing Counsel/Opposing Party rather than a missing client,
  matched against Clio's own Relationship contacts — review by hand, not auto-imported
- `output/outlook_migration_{from}_{to}_calls.csv` — every TCON/OCON call found, with
  its resolution (matched/OC/OP/skipped/unresolved) — for visual comparison
- `data/outlook_call_overrides.csv` — persisted manual call decisions (party -> matched
  matter or skipped), not date-stamped like the other outputs; grows across runs
- `output/outlook_migration_{from}_{to}_already_in_clio.csv` — matched events skipped
  because Clio already has an entry for that matter on that date (safe to re-run)
- `output/outlook_migration_{from}_{to}_tag_not_found.csv` — from the tagging step:
  CSV rows whose matching Clio entry couldn't be found (not actually imported yet, or
  the summary drifted on import — check by hand)
- `logs/outlook_migration_YYYYMMDD.log`, `logs/outlook_migration_tag_YYYYMMDD.log`

## Workflow
```powershell
# 1. Auth (if token expired)
uv run src/outlook_auth.py --refresh

# 2. Generate the CSV — always start from 2026-06-01 (Clio's start date);
#    push --to-date out far enough to catch future-scheduled events too
uv run src/outlook_migration.py --from-date 2026-06-01 --to-date 2028-06-01

# 3. Open the CSV, resolve every "OP/CL" placeholder (RFOs only), fix any names in
#    MANUAL_MATTER_MAP (top of outlook_migration.py) that showed up in the
#    exceptions file, then re-run if you changed the map. Read it, don't just
#    skim it — see the substring-matching limitation noted above.

# 4. Import output/outlook_migration_*.csv in Clio: Settings > Data Import > Calendar Events
#    (undo from there if something looks wrong)

# 5. Tag the entries you just imported with type = Heidi
uv run src/outlook_migration_tag.py --csv output/outlook_migration_2026-06-01_to_2028-06-01.csv

# 6. Import personal recurring series (dog pickup, workouts, birthdays, etc.) onto
#    Heidi's own calendar — always --dry-run first
uv run src/outlook_recurring_availability.py --from-date 2026-06-01 --to-date 2028-06-01 --dry-run
uv run src/outlook_recurring_availability.py --from-date 2026-06-01 --to-date 2028-06-01

# 7. Import everything left in the exceptions file onto Heidi's own calendar too —
#    same live-write safety rules, always --dry-run first
uv run src/outlook_exceptions_availability.py --from-date 2026-06-01 --to-date 2028-06-01 --dry-run
uv run src/outlook_exceptions_availability.py --from-date 2026-06-01 --to-date 2028-06-01
```

## Availability import — recurring series and exceptions (API writes)

Steps 6-7 above are the only scripts in this project that write to Clio directly
instead of generating a CSV for manual import — because Clio's CSV "Calendar
events" template has no way to target a specific calendar (no `calendar_owner`
column) or set recurrence (`recurrence_rule` only exists on the live API). Both
follow the project's standard live-write safety rules: `--dry-run` required, log
every request, retry on 429, continue on individual failures. Shared helpers
(Heidi's Calendar id, event type id, the retry/POST logic) live in
`outlook_calendar/clio_write.py`.

**`HEIDI_CALENDAR_ID = 8860113`** — Heidi's Clio *Calendar* id, **not** her
`USER_ID_HEIDI` env var. These are different Clio resources (see the Court
Calendar Sync note above: `calendar_owner`/`attendees` are Calendar/Attendee
records, no shared ID with `/users.json`). Found by inspecting an existing
Heidi-owned calendar entry's `calendar_owner.id` — if it ever needs
rediscovering, fetch any `calendar_entries.json` record you know is on her
calendar with `fields=calendar_owner{id,name}`.

**`outlook_recurring_availability.py`** — imports Heidi's PERSONAL recurring
Outlook series (dog pickup, workouts, Rotary, tax reminders, birthdays — not
client-related, but they do occupy real time on her calendar) as true recurring
`CalendarEntry` records. Real check of Heidi's calendar found 35 recurring
series, all personal — zero overlap with anything client-relevant — but the
script still runs `event_parser.looks_client_relevant()` as a guard and skips
(with a warning) anything that would look like a hearing/call/DUE, so a future
change in her calendar habits can't silently misfile a recurring client call
here instead of through the normal per-occurrence matching path.

Recurrence translation (`outlook_calendar/recurrence.py`) converts Microsoft
Graph's structured `recurrence.pattern` object into the RFC-5545 RRULE string
Clio's `recurrence_rule` field expects — confirmed against real data, not
guessed: a human created a real recurring entry in Clio's own UI and its
`recurrence_rule` was read back (`"FREQ=DAILY;WKST=SU"`), then a translated
`relativeMonthly` pattern ("third Thursday") was POSTed as a disposable test
entry, confirmed accepted, and deleted. Clio's own echo omits `INTERVAL=1` (the
default) — the translator matches that canonical form, which matters because
dedup compares a freshly generated RRULE string against what's already stored.
All 35 real series use `range.type: "noEnd"` (genuinely open-ended in Outlook)
— the translator only handles the pattern, not `UNTIL`/`COUNT`; extend it rather
than guess an untested format if a bounded series ever shows up. Dedup key is
`(subject, recurrence_rule)`, not subject alone — real data has two distinct
series both literally named "PAY PROPERTY TAXES BY THE 10TH" (different
installments, different schedules).

**`outlook_exceptions_availability.py`** — imports every remaining
`outlook_migration.py` exception (couldn't be matched to a client matter) as a
one-off `CalendarEntry` on Heidi's personal calendar. Deliberately
**unfiltered** by explicit decision (2026-07-15): this includes exceptions
where a party *was* extracted but didn't match a matter (might really be
unresolved client work) and genuinely private items — rather than guess which
is which, everything goes in as-is; a duplicate, matter-less copy on Heidi's
personal calendar is harmless if something's later resolved to a real matter.
Reuses `outlook_migration.gather_matched_events()` directly (the same function
`outlook_migration.py`'s own `main()` calls) instead of reimplementing the
fetch/match pipeline, so the two scripts can't define "exception" differently
and drift apart — this also means running this script triggers the same
interactive call-resolution prompts as a normal run for anything not yet
resolved (already-resolved parties from `data/outlook_call_overrides.csv` won't
re-prompt). Dedup key is `(subject, start_at)` — one-off entries, no
recurrence_rule to key on.

**`outlook_migration.py`'s own exceptions file excludes personal recurring
series** — `gather_matched_events()` fetches distinct recurring series
(`graph_client.fetch_recurring_series()`) and drops any occurrence belonging to
a series that isn't client-relevant, so a daily/weekly series' 100+ individually
-expanded occurrences don't flood the exceptions file; they're handled once
each by `outlook_recurring_availability.py` instead.

---


# Court Rules Automation (cancelled)

Was planned: auto-apply Clio Court Rules to matched court events (RFO, Trial, etc.) so
the deadline chain generates without a paralegal doing it by hand per matter. Blocked
on a `403 Forbidden` (Court Rules permission not granted to this app in the Clio
Developer Portal) and never unblocked. **Cancelled** — no longer planned.

---

