> Auto-loaded by Claude Code when working in `src/web/`. Root map: `../../CLAUDE.md`.
> Note: this isn't an isolated subsystem — `routes_*.py` are thin wrappers over the other
> modules (equalizer, moore_marsden, trust, collections, etc.). If you're editing a
> `routes_X.py` file, also read that module's own doc (see root map) — the actual logic
> lives there, not here.

# Web Dashboard

**App:** `src/web/app.py` (FastAPI + Jinja2 + vanilla JS — no Node/npm toolchain)

**Branding:** rebranded 2026-07-29 from "Clio Dashboard" to **Collier Automation
Platform (CAP)** — dark ink/brass visual identity, channel-grid home page (each
subproject shown as a "channel" with its data flow, e.g. `PDF -> Clio`). This is
step one of the bigger "CAP" vision described later in this doc (see "CAP — Collier
Automation Platform") — this dashboard *is* the platform, not a separate thing that
happens to share its name; what that section still describes as unbuilt is the
automated Windows Service + Scheduler layer on top of it.

**Purpose:** Browser UI for the whole repo — a home page linking to drag-and-drop
versions of Printer Expenses, Bradford Invoice Import, and Legs Expenses, plus Court
Calendar Sync, RingCentral Directory Sync, and Trust Monitor & Replenishment
Requests. Wraps each script's existing `run_pipeline()` function; does not duplicate
matching/posting logic.

**Run:**
```powershell
uv run uvicorn web.app:app --app-dir src --host 0.0.0.0 --port 8421
# or just double-click start-dashboard.bat (syncs deps, then starts)
```
`--app-dir src` puts `src/` on `sys.path`, matching how the standalone scripts resolve
flat imports (`import matter_matching`, etc.) — same convention, one process.

**Concurrency:** `start-dashboard.bat` launches uvicorn with no `--workers` — one process,
one event loop, serving every LAN user. Fixed 2026-08-03: the drag-and-drop
preview/confirm routes (Legs, Printer, Bradford) and the live-pipeline routes (Trust,
RingCentral) originally called their subproject's `run_pipeline()` directly inside an
`async def` handler — a fully synchronous, blocking call (Legs' page-by-page Tesseract
OCR and everyone else's live `requests`-based Clio fetches aren't asyncio-aware) sitting
on the one event loop thread. Confirmed live: with Legs OCR running, a *second browser
tab* loading an unrelated page (e.g. `/calendar`) hung until the OCR finished — one
person's upload froze the dashboard for the whole office, not just their own request.
Every such call is now wrapped in `fastapi.concurrency.run_in_threadpool(...)` so it
runs on a worker thread instead of the event loop. `routes_trust.py`'s `_load_home` had
to become `async def` for this, since it's the shared re-render path after every
mutating action. Deliberately NOT wrapped: `routes_trust.py`'s `_send_one()` (the
Clio-POST that actually creates a TrustRequest) — it shares a `sqlite3.Connection`
created on the request thread, and `web/db.py`'s `get_connection()` doesn't set
`check_same_thread=False`, so handing that connection to a threadpool worker would raise
`ProgrammingError` rather than fix anything. Its blocking window is small (a handful of
selected matters per send, not a few-hundred-matter fetch) so it was left as future work
rather than papered over. Adding uvicorn `--workers` instead of threadpooling was
considered and rejected — the dashboard's dry-run-preview -> confirm handoff
(`preview_store.PREVIEWS`) is an in-memory dict, not shared across separate worker
processes, so multiple workers would break "Confirm & Post" whenever the confirm
request landed on a different worker than the preview did.

**Auth:** Single shared passphrase (`CLIO_DASHBOARD_PASSPHRASE` in `.env`), not per-staff
login — the Clio side already uses one shared app registration. Session is a signed cookie
(`CLIO_DASHBOARD_SECRET`). This is LAN-only gatekeeping, not intended as a public-internet
login system. Auth is applied per-route (`Depends(require_auth)` in each `routes_*.py`),
not as blanket middleware — **2026-08-04: every route under `/calendar` (Court Calendar
Sync — see that section — including its client-list report and the Court Case Number
write) deliberately has no `require_auth` at all**, by explicit decision, so the court
calendar is usable by anyone on the LAN/Tailscale without logging in. Every other
subproject (Bradford, Printer, Legs, RingCentral, Trust) still requires the passphrase;
logging in via `/login` sets one session cookie for the whole app, so entering the
passphrase from anywhere unlocks those too in the same session. `base.html`'s topbar
reflects this: authenticated sessions get the full nav, unauthenticated visitors (outside
`/login`) get a minimal one with just "Court Calendar" and "Log in".

**Storage:** SQLite at `data/clio_dashboard.db` — court events, purpose mappings,
staff cache, RingCentral sync run history, trust request settings/lifecycle. No
external database service. Gitignored via the blanket `data/` rule — see the
"Gap closed" note under Project Structure.

**Drag-and-drop apps (Bradford Invoice, Printer Expenses, Legs Expenses):** upload a file -> dry-run
preview (payloads + exceptions, same categories as the CLI) -> "Confirm & Post" button ->
live run. Mirrors the CLI's `--dry-run` workflow; the confirm step is the only path that
posts to Clio.

**Remote access off the office LAN:** install [Tailscale](https://tailscale.com) on the
machine running the dashboard and on any device that needs to reach it — no port
forwarding or public-facing server required. The dashboard itself only ever binds to the
LAN/Tailscale interface, never a public one.

**On-LAN access:** `CAP Dashboard.url` (repo root, added 2026-07-28) is an internet
shortcut pointed at `http://cap.lan:8421/` — meant to be copied to individual staff
desktops or emailed as an attachment so non-technical staff double-click instead of
typing an IP and port. `cap.lan` is a local DNS/hosts-file name resolved on the office
LAN (not something this repo configures or documents further — it's network-side setup,
same category as the Windows Scheduled Task registration for RingCentral sync).

**New `.env` keys:**
```
CLIO_DASHBOARD_SECRET       # session cookie signing key
CLIO_DASHBOARD_PASSPHRASE   # shared login passphrase
```

---


# CAP — Collier Automation Platform (dashboard is step one; Service + Scheduler not started)

**Resolved 2026-07-30:** the web dashboard (`src/web/`, rebranded 2026-07-29 — see "Web
Dashboard" above) is **step one of this vision, not a separate thing.** It already
delivers the core goal below — one branded UI wrapping every subproject's
`run_pipeline()`, instead of scattered one-off scripts — for on-demand/manual use. What's
still not built is the automated half described in this section: a Windows Service +
Scheduler layer for unattended/scheduled runs (e.g. Court Calendar Sync's still-planned
morning run) that don't require a human to open the dashboard.

Idea from 2026-07-27, expanded 2026-07-28: rather than keep adding one-off scripts for
each new Clio-adjacent integration, consolidate them into one maintainable platform —
**CAP (Collier Automation Platform)**, chosen over narrower names like "Clio Automation
Service" because the intent is for this to eventually be the integration layer for the
whole practice, not just a Clio-facing tool. One platform to maintain, not many one-offs.

**Architecture direction for the remaining (unattended/scheduled) piece:** a Windows
Service + Scheduler, with each integration as a plug-in module rather than its own
standalone script — sitting alongside the dashboard (not replacing it) for the runs
that shouldn't need a human at the keyboard:
```
                Clio
                  │
        ┌─────────┼─────────┐
        │         │         │
    Contacts   Matters   Activities
        │         │         │
        ▼         ▼         ▼
              CAP (Windows Service + Scheduler)
 ┌────────┬──────────┬─────────┬──────────┬───────────┐
 │PaperCut│RingCentral│Outlook │Accounting│Reporting  │
 └────────┴──────────┴─────────┴──────────┴───────────┘
```
Every module talks to Clio through **one internal API client**, not directly — a future
Clio API change (or a repeat of this repo's own custom-field/nested-selector gotchas)
gets fixed in one place instead of N scripts each needing the same fix separately.

**Modules to fold in — status as of 2026-07-30:**
- PaperCut account synchronization — still just an idea, not started (see PaperCut
  Shared Account Sync in `reference/printer-papercut.md`)
- RingCentral contact synchronization — built, has a dashboard page (`/ringcentral`)
  *and* its own standalone daily Windows Scheduled Task (`sync-ringcentral.bat`), so
  it already achieves "unattended scheduled run" per-subproject, just not through a
  unified CAP service — worth noting as a pattern (one `.bat` + `schtasks` per
  subproject) that could cover a lot of this section without a full service ever
  getting built
- Matter-based print cost exports — built as Printer Expenses, dashboard page only,
  no scheduling yet
- Court Calendar Sync — dashboard page only (on-demand); the scheduled-morning-run-
  with-emailed-report upgrade is still not built

**Explicitly not needed:** pushing Clio contacts out to individual staff phones
(iPhone/Android). RingCentral already resolves caller name via CallerID off the
existing Directory Sync — a separate device-level contact push would be solving an
already-solved problem.

**Candidate modules — captured for future evaluation, none of this is scoped or
committed work yet:**
- **Matter close cleanup** — nightly job to close/archive PaperCut accounts, document
  folders, and shared drives for matters that just closed in Clio, instead of leaving
  stale access around indefinitely
- **Automatic print-cost disbursements** — Printer Expenses today is a monthly manual
  drag-and-drop import; this would post the Clio ExpenseEntry automatically as PaperCut
  records usage, no monthly file needed
- **Billing intelligence** — nightly cross-check of calendar entries, phone calls, and
  emails against entered time, flagging likely missed billable events (directly
  supports the firm's stated collections priority)
- **A/R automation** — aging invoice reminders escalating from client email/text ->
  attorney notification at 60 days -> collections queue at 90
- **Conflict-check assist** — on new-contact creation, search existing clients, related
  contacts, and opposing parties for a possible conflict
- **Document intelligence** — auto-route new documents to the right matter subfolder
  by type (motions, declarations, financials, etc.)
- **Office dashboard** — a shared-screen view of daily firm-wide stats: open matters,
  today's hearings, new consultations, outstanding A/R, pages printed, hours entered
  vs. missing
- **Employee productivity summaries** — daily per-attorney/staff rollup of billable vs.
  admin time, emails, calls, documents, appointments, estimated utilization
- **Reception call-lookup / screen pop** — on an inbound RingCentral call, look up the
  caller in Clio and surface matter info (client: balance due, trust, WIP, next court
  date; opposing counsel/party: which matter they're opposing) to reception before they
  answer (distinct from CallerID name display, which is already solved). Feasibility
  discussed 2026-08-01, not scoped or started — see below.

  **Trigger:** RingCentral's directory *export* is push-only (see RingCentral Directory
  Sync in `reference/ringcentral.md`), but that's a different API surface than call events — RingCentral's
  Notification/Subscription API supports real-time webhooks on Telephony Session events
  (ringing, with caller ANI), which is what a screen pop would listen on. Not yet
  verified against this account's RingCentral plan/app permissions — first thing to
  confirm before building.

  **Lookup:** needs its own phone→matter reverse index built straight from Clio contact
  data, *not* reused from `ringcentral_directory.py`'s output CSV — that output collapses
  contacts sharing a phone number into one merged directory row (see "Phone dedup" under
  RingCentral Directory Sync), which loses the individual matter link a screen pop needs.

  **Matter data:** trust/WIP math already exists in `trust_monitor.py` (WIP =
  `unbilled_amount` + draft/awaiting_approval bill totals, not `unbilled_amount` alone —
  see Trust Monitor & Replenishment Requests); next court date is a
  `calendar_entries.json?matter_id=...` query, same as Court Calendar Sync uses.

  **Main open risk — latency, not data availability:** a phone rings for maybe 15-20
  seconds. Live-summing WIP across bill states on every ring is cutting that close.
  Leaning toward a cached snapshot table (refreshed every few minutes, keyed by phone
  number, same shape as `trust_requests`) that the screen pop reads instantly, rather
  than hitting Clio live per call — revisit this tradeoff when actually scoping it.

  **Also undecided:** how the pop is actually displayed at reception's desk (always-on-
  top window vs. a dashboard browser tab pushed to via WebSocket/SSE — the dashboard is
  currently request/response only, no server push exists yet).
- **Matter timeline** — unify calls, emails, documents, billing entries, hearings, and
  notes for a matter into one searchable timeline
- **AI matter assistant** — auto-generated per-matter summary: last hearing, upcoming
  deadlines, outstanding discovery, balance due, last client contact

**Not decided yet:** whether this becomes a new top-level module wrapping the existing
scripts' logic, a rewrite, or a scheduler that just orchestrates the existing CLI entry
points unchanged — revisit when this is actually picked up. Bradford Invoice Import and
Legs Expenses stay as their own document-import tools rather than CAP modules — they
parse a contractor's PDF invoice, which isn't the "keep an external system synced with
Clio" pattern the rest of this platform is built around.

---

