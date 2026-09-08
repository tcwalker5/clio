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
happens to share its name. What that section covers on top of it: keeping the
dashboard itself always running (Windows Task Scheduler, not a custom service) and a
per-feature pattern for unattended/scheduled subproject runs — see that section for
the 2026-09-03 architecture decision.

**Purpose:** Browser UI for the whole repo — a home page (channel grid) linking to
every subproject's dashboard page. **Corrected 2026-09-03** — this line had drifted
stale, naming only the first few subprojects built (Printer Expenses, Bradford
Invoice Import, Legs Expenses, Court Calendar Sync, RingCentral Directory Sync, Trust
Monitor) and silently omitting everything added since (Collections, Staff Unbilled
Report, PaperCut Accounts, Equalizer, Moore/Marsden, Client Assignment). Left as a
general description rather than re-enumerated, specifically so it can't drift stale
the same way again — see root `CLAUDE.md`'s Project Structure tree or
`dashboard.html`'s channel grid for the current, authoritative list of what's wrapped.
Wraps each script's existing `run_pipeline()` function; does not duplicate
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


# CAP — Collier Automation Platform (dashboard always-on + per-feature scheduling)

**Resolved 2026-07-30:** the web dashboard (`src/web/`, rebranded 2026-07-29 — see "Web
Dashboard" above) is **step one of this vision, not a separate thing.** It already
delivers the core goal below — one branded UI wrapping every subproject's
`run_pipeline()`, instead of scattered one-off scripts — for on-demand/manual use.

**Architecture decided 2026-09-03 (Ted)** for the remaining unattended/automated piece
— explicitly **not** the single custom "Windows Service + Scheduler with plug-in
modules and one internal API client" originally sketched here (kept below, struck
through in spirit, as design history — see "Rejected direction"). The requirement
driving this decision: the mechanism must not need editing every time a new subproject
ships, since this repo adds new subprojects often (six shipped in the single week this
was decided). A shared scheduler process or a shared internal API client is exactly the
kind of central thing a fast-moving repo like this one tends to break while adding
feature N+1. Two independent, native-Windows mechanisms instead:

1. **Dashboard uptime — Windows Task Scheduler, not a custom service.**
   `start-dashboard-service.bat` (no `pause`, unlike `start-dashboard.bat` — Task
   Scheduler has no console to send a keypress to, so a `pause` here would hang the
   task forever after any crash instead of exiting with a real code) +
   `start-dashboard-service.vbs` (hides the console window; unlike
   `start-dashboard-silent.vbs`'s fire-and-forget `WshShell.Run(..., False)` used for
   the desktop shortcut, this one runs with `True` — it must block until uvicorn
   exits, or Task Scheduler would mark the task "finished" the instant it launched the
   batch file while uvicorn kept running detached underneath it, breaking both
   "restart on failure" and "don't start a duplicate instance"). Registered as a
   Scheduled Task, trigger **At log on** (not At startup — avoids ever storing a
   Windows account password in Task Scheduler; if this ever needs to survive a reboot
   with nobody logged in, that's the tradeoff to revisit), Settings: restart on
   failure (a few attempts, short interval), don't start a new instance if one's
   already running. This intentionally is *not* full service supervision (no
   services.msc entry, no true process-health monitoring if uvicorn hangs without
   exiting) — accepted tradeoff for zero new dependencies and reusing a pattern this
   repo already trusts (see #2). **Registered and live-tested 2026-09-03** — the
   `Register-ScheduledTask` principal needed the fully-qualified `COMPUTERNAME\TEDMINI`
   form for `-UserId`; a bare `"TEDMINI"` fails registration with "The parameter is
   incorrect." `ExecutionTimeLimit` must be explicitly zeroed out
   (`New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero)`) — Task
   Scheduler's own default kills any task still running after 72 hours, which would
   silently take the whole dashboard down after 3 days of uptime otherwise. **Deploying
   a code change to the running service is still a manual step** — Python files aren't
   hot-reloaded (only Jinja2 templates are, unchanged from before), and this was a
   deliberate choice over a file-watcher auto-restart: a watcher would bounce the live
   LAN service mid-edit during a multi-file change, serving a half-finished state to
   whoever's using it at that moment. After a deploy, restart with **`restart-dashboard.bat`**
   (repo root — there's no native `Restart-ScheduledTask` cmdlet, this wraps
   `Stop-ScheduledTask` + a 3s pause + `Start-ScheduledTask` in one command; the pause
   is deliberate, see the incident right below).

   **Real incident 2026-09-04 — a restart left `/date-calculator` 404ing against a
   stale process.** Most likely proximate cause: only `Start-ScheduledTask` was run,
   without `Stop-ScheduledTask` first — Ted asked for a true "restart" command right
   after, which is exactly what `restart-dashboard.bat` above now is, precisely so
   this can't happen from muscle memory again. Independently, this also surfaced a
   real bug worth fixing regardless of what caused this specific incident:
   `start-dashboard-service.vbs` called `WshShell.Run(...)` but never did anything
   with its return value — VBScript only propagates a launched process's exit code if
   something explicitly acts on it (`WScript.Quit(exitCode)`); without that,
   `wscript.exe` always exits `0` no matter what the batch/uvicorn underneath it
   actually did. Confirmed live via the log: uvicorn failed to bind port 8421
   (`WinError 10048`, exit code 3, because the old process was still holding it), but
   `Get-ScheduledTaskInfo` reported `LastTaskResult: 0` (success) anyway — meaning
   Task Scheduler's own "restart on failure" setting would never fire on a *genuine*
   crash either, silently, since its first signal (a non-zero exit code) never
   reached it. Fixed by adding
   `WScript.Quit(WshShell.Run(...))` so the real exit code reaches Task Scheduler.
   **This means the crash-restart safety net was silently non-functional from
   registration (2026-09-03) until this fix (2026-09-04)** — worth remembering if a
   past "it restarted fine" observation from that window gets second-guessed.

2. **Scheduled/unattended jobs — per-feature Windows Scheduled Tasks, not a shared
   scheduler.** Generalizes RingCentral's own pattern below, which predates this
   decision and was the direct precedent for it: one small `.bat` (`uv run
   src/<feature>.py`, nothing else) + one Task Scheduler entry, per subproject that
   needs unattended runs. A new feature's schedule is entirely self-contained — adding
   one never touches another feature's `.bat`, task, or any shared "scheduler" code,
   and one job misbehaving can't take another down. The registration itself (the
   `schtasks`/`Register-ScheduledTask` call) is a one-time, per-machine setup step —
   **not tracked in this repo**, same as `cap.lan`'s DNS entry — because it's
   machine/network configuration, not application code; only the `.bat` each task
   runs lives in git.

**Rejected direction (2026-07-27 through 2026-09-02, superseded above):** a single
Windows Service + Scheduler with each integration as a plug-in module, all talking to
Clio through one internal API client (diagram and reasoning kept below as design
history). Revisit only if the per-feature-tasks approach demonstrably breaks down at
higher volume (e.g. dozens of scheduled jobs making per-machine task sprawl genuinely
hard to audit) — not before.
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

**Modules to fold in — status as of 2026-09-04:**
- PaperCut account synchronization — built (see PaperCut Shared Account Sync in
  `reference/printer-papercut.md`), dashboard page only, no unattended schedule yet
- RingCentral contact synchronization — built, has a dashboard page (`/ringcentral`)
  *and* its own standalone daily Windows Scheduled Task (`sync-ringcentral.bat`) — the
  precedent that became the decided pattern above (#2)
- Matter-based print cost exports — built as Printer Expenses, dashboard page only,
  no scheduling yet
- Client Assignment — built (see `reference/client-assignment.md`), dashboard page
  only, not a candidate for unattended scheduling (it's an interactive assign-from-
  dropdown tool, not a sync)
- Court Calendar Sync — dashboard page only (on-demand); the scheduled-morning-run-
  with-emailed-report upgrade is still not built (still blocked on no email
  infrastructure existing anywhere in this repo — see Trust Monitor's own note on this)
- Date Calculator — built (see `reference/date-calculator.md`), dashboard page +
  a daily background sync (`sync-length-of-marriage.bat`) that (re)writes a matter's
  "Length of Marriage" field wherever both Date of Marriage and Date of Separation
  are set — the second real per-feature Scheduled Task after RingCentral's, and the
  first candidate that was actually considered for a Clio webhook instead (rejected:
  webhooks need a public HTTPS endpoint, which this LAN-only dashboard doesn't have —
  see that reference doc for the full reasoning)

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

**Decided 2026-09-03** (see above): neither a rewrite nor a unified scheduler — each
subproject keeps its own CLI entry point unchanged, and any that need unattended runs
get their own `.bat` + Scheduled Task, orchestrated by nothing shared. Bradford Invoice
Import and Legs Expenses stay as their own document-import tools rather than CAP
modules — they parse a contractor's PDF invoice, which isn't the "keep an external
system synced with Clio" pattern the rest of this platform is built around.

---


## Per-user Clio attribution — open decision (2026-09-08, on hold)

**The problem:** every Document, Note, and write this platform makes shows up in Clio
as authored by whoever authorized the one shared `CLIO_ACCESS_TOKEN` — currently Ted
Walker (confirmed live via `/users/who_am_i.json`, id `359072911`), regardless of which
staff member actually used the dashboard. Surfaced 2026-09-08 via Equalizer's Save-to-
Clio (a PDF and a Note, both attributed to Ted no matter who saved them), but it's true
of every write across every subproject, not just Equalizer/Moore-Marsden.

**Confirmed not fixable per-request:** a Document's `creator` field (`ClioCreator`, a
full user reference) and a Note's `author` field (`User`) both exist on read, but
**neither is a writable property** in `POST /documents.json` or `POST /notes.json`
(checked directly against `reference/openapi.json`'s request-body schemas, not
assumed) — they're server-derived from whatever identity the API call authenticates
as. There is no override.

**Rejected 2026-09-08 (Ted): appending the real user's name as visible text** (e.g.
"(saved by Misty Sherman)" in the document name or Note body, while the real Clio
`creator`/`author` metadata still shows Ted) — explicitly not an acceptable compromise.
**Ted's stated position: either ignore this feature entirely, or do the full per-user
integration** — no scoped/partial version (e.g. per-user auth just for Equalizer/
Moore-Marsden's Document+Note saves, leaving everything else on the shared token) is
on the table. Decision between those two not yet made — parked here for a future
session rather than re-scoped smaller.

**What full per-user integration actually requires** (scoped 2026-09-08, not yet
built):
1. **A real web-based OAuth callback.** `clio_auth.py`'s existing flow opens a browser
   and runs a *local* HTTP server on `127.0.0.1` to catch the redirect — that only
   works for a CLI script on one machine. A shared dashboard needs a server-side
   callback route (e.g. `http://cap.lan:8421/oauth/callback`) so each person's own
   browser redirects back to the dashboard itself. Same Clio app registration/
   client_id, no new Developer Portal app needed — just a different redirect URI and
   a route to receive it.
2. **Per-user token storage.** A new table in `data/clio_dashboard.db` (own schema
   fragment, matching this project's established per-module pattern) holding each
   authorized person's access/refresh token + expiry, keyed by Clio user id — not a
   single `.env` pair anymore.
3. **Per-user token refresh.** Access tokens expire in about an hour. Today's refresh
   is a manual `clio_auth.py --refresh` editing one `.env` pair; for N people this has
   to happen automatically, per row, with no human running a CLI command.
4. **CAP's login model has to change.** `web/auth.py` today is a bare boolean — one
   shared passphrase, no identity attached at all. Real attribution means CAP has to
   know *who* is logged in, which points at replacing the shared passphrase with
   "Sign in with Clio" itself (Clio already is the identity system that matters here).
   This is a genuinely different login experience for the whole office, not just a
   backend change. Court Calendar Sync's pages (deliberately open, no login, so
   anyone on the LAN can use them) would need to stay carved out separately, still
   reading under a fixed/shared identity.
5. **Every subproject's `build_session()` reads one global env var today.** Making
   this per-user means threading "whose token" through essentially the whole
   codebase — `trust_monitor.py`, `collections_monitor.py`, `client_assignment.py`,
   `date_calculator.py`, `equalizer/`, `moore_marsden/`, all of it — and every route
   handler needs to resolve "the current logged-in user" and pass their session
   through. This is the single largest piece of the work by line count.
6. **Real permission-compatibility risk, unverified:** Ted's token currently works for
   Accounting-scoped calls (Trust Monitor) because he's presumably an account
   owner/admin in Clio. A paralegal's or associate's own personal Clio permissions
   might not include Accounting or Billing write — their personal token could be
   rejected on operations Ted's sails through today. Needs a per-person Clio
   permission audit before trusting this platform-wide, not assumed to just work.

**Not decided:** whether to build this (full scope, per point 1-6 above, no smaller
version per Ted's stated either/or) or drop the ask entirely. Revisit when picked back
up — start from this section rather than re-deriving the schema findings above.

---

