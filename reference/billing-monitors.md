> Not auto-loaded — `trust_monitor.py`, `collections_monitor.py` (+
> `collections_flarpl.py`, `collections_payment_plan.py`), and
> `staff_unbilled_monitor.py` are flat files in `src/`, no subfolder to hang a
> per-directory CLAUDE.md off of. Open this yourself before touching any of them.
> Root map: `../CLAUDE.md`.

# Trust Monitor & Replenishment Requests

**Modules:** `src/trust_monitor.py`, dashboard page at `/trust`
(`src/web/routes_trust.py`, `src/web/templates/trust.html`)

**Purpose:** Two independent things on one page.

1. **WIP-vs-trust monitor** (informational) — flags open matters where the
   "cushion" (trust balance minus WIP) has dropped below $2,500, an early
   warning that unbilled work is outpacing trust before it's even billed.
2. **Trust replenishment request review** (the actual point of the tool) —
   lets billing staff bulk-review and send trust top-up requests to Clio as
   unapproved drafts (`approved: false`), with per-matter pause and target
   override, so nothing goes out without a human clicking Send.

**WIP is not `BillableMatter.unbilled_amount` alone.** That field only
counts activity never added to *any* bill — it silently excludes activity
already sitting on a draft or awaiting-approval bill. Confirmed live
(matter WELLS, ANDREW): the field showed $150 vs. his real $11,275.82 WIP.
Correct WIP = `unbilled_amount` + the total of that matter's bills in state
`draft`/`awaiting_approval`. An "outstanding" figure (already invoiced,
unpaid — sum of `Bill.balance` for bills in state `awaiting_payment`) is
still fetched internally, but only to decide whether a matter with zero
trust/WIP/outstanding activity can be skipped entirely — **it is no longer
displayed on this page.** It used to sit in a column next to the cushion
math (labeled "Outstanding"), which read as if it were part of the same
calculation when it never was. Split out 2026-08-11 into its own page — see
"Collections" below — since a retainer shortfall and an overdue bill are
different problems with different remedies (a TrustRequest can't legally
carry the client's card processing fee; a direct bill payment can).

**Trust balance comes from `Matter.account_balances`** (`type == "Trust"`),
not `BillableMatter.amount_in_trust` — the latter only returns a record for
matters with nonzero *never-billed* activity, too narrow to use as the
matter universe (confirmed live: 60 of 186 real matters with pending bill
activity were entirely absent from it). `account_balances` needs the
**Accounting** permission checked in the Developer Portal (came back
`{"redacted": true}` before that, same signature as Bradford's `custom_rate`
gotcha) plus the full `clio_auth.py` browser re-auth, not `--refresh`.

**Requested amount is deliberately trust-balance-only, not WIP-based** —
this took two wrong turns to land on (see project memory
`project_trust_monitoring.md` if working on this again). The rule: a matter
becomes a request candidate once its raw trust balance drops below
`ACTION_GATE` ($2,000, fixed); the requested amount tops it back up to
`TRUST_MINIMUM` ($2,500 default, overridable per matter), rounded up to the
next $10. WIP is intentionally excluded from this calculation — billing
stays 100% manual (Clio's own UI, monthly on the 1st + occasional ad hoc,
with trust-application-on-approval and duplicate-billing prevention already
built into that process) rather than this tool trying to pre-fund unbilled
work via a trust request ahead of billing. **Outstanding balances are never
folded into a trust request either** — trust deposits can't legally absorb
card processing fees (would effectively skim client trust funds), but a
direct bill payment can pass the surcharge to the client, so collecting an
overdue bill needs its own separate flow (not built) rather than being
mixed into a trust top-up.

**Request lifecycle**, backed by two tables in `data/clio_dashboard.db`
(`trust_matter_settings`: per-matter target override + pause, persists
indefinitely until unpaused or the matter closes; `trust_requests`:
lifecycle log, since Clio's API has no GET/list endpoint for TrustRequest at
all — this table is the *only* record of what's already been requested). A
candidate with no pending request is new; one with a pending request whose
recorded trust balance still matches current trust is "already requested"
(client hasn't paid yet); a real difference marks the old request stale and
generates a fresh one.

**Credit card fee exposure:** the review table shows a live-updating summary
(count, total requested, estimated card processing fee risk at 2.95% —
`trust_monitor.CARD_FEE_RATE`) for whatever rows are currently checked —
purely informational, recalculated client-side, not applied to any request
or persisted anywhere. Exists because trust deposits can't legally pass the
card surcharge to the client the way a direct bill payment can, so the firm
absorbs it if a client pays a trust request by card — worth seeing before a
large batch send, not after.

**Live sending is BLOCKED and on hold as of 2026-08-12 — every real attempt
so far has failed with a 400.** `approved: false` draft-not-notify behavior
is therefore still entirely unconfirmed; no TrustRequest has ever
successfully reached Clio through this code.

**First attempt (2026-08-04)**, OCHOA, EVA (matter 1786839078): root cause
found — `create_trust_request()` sent every amount as a Python float
everywhere, but Clio's own OpenAPI spec (`/trust_requests.json` POST) types
the **nested** `data.matter[].trust_amount` as `integer`/int32 while the
**top-level** `data.trust_amount` is `number`/double — the code was handing
Clio `2600.0` where it declared an integer field. Fixed by casting only the
per-matter amount to `int` (amounts are always whole tens already, via
`_round_up_to_10`, so no precision is lost). Also fixed in the same pass:
`create_trust_request()` previously logged nothing at all on the
request/response, which is why the dashboard error only ever showed `"400"`
with no body text — it now logs the full payload and Clio's response
body/status before raising.

**Second attempt (2026-08-12)**, WELLS, ANDREW (matter 1786847613, requested
$7,560, the int-cast fix from above already in place): still a 400, on every
retry (five real attempts logged, including after a full OAuth re-auth
mid-session — see below). This time `resp.text` came back completely empty
(`Content-Length: 0`), not the `{"error": {...}}` shape Clio normally
returns — confirmed live that a deliberately malformed payload (missing
`data` entirely) *does* get a proper JSON error body, so the endpoint can
render errors; something about a well-formed TrustRequest specifically
short-circuits before that. A shape-valid payload with a bogus/nonexistent
`client_id`/`matter.id` produces the exact same empty 400 as the real
WELLS request, which is why the leading theory is a permission/authorization
gate firing before Clio even checks whether the referenced records exist —
not a data problem with this specific matter (independently confirmed live:
matter Open, `client.id` matches, client is a Person with a valid email).

**Permissions ruled out so far:** Billing was already Read/Write. Accounting
was Read-only (granted 2026-07-30 for reading `account_balances`) and was
changed to Read/Write on 2026-08-12, followed by a full `clio_auth.py`
re-auth (not `--refresh`) — confirmed the token actually rotated
(`.env` mtime moved to match), and the 400 was unchanged across multiple
retries afterward. **Payment distributions** (Clio's own description:
"Payment info on bills, trust payments, credit memos, allocations" — the
closest textual match to TrustRequest of anything in the permission list)
had not yet been tried as of when this was put on hold — that's the next
thing to flip, if picked back up.

**Escalated to Clio API support** (`api@clio.com`) on 2026-08-12 with the
full diagnostic writeup above, since public Clio docs (docs.developers.clio.com)
don't document a permission-to-endpoint mapping and the support-article
pages require a login this project can't get past. **Work is paused pending
their reply** — don't attempt further live sends without first checking
whether that reply narrowed things down, to avoid burning more guesses on
permission checkboxes blind.

**Deferred, not built:** email notification for the WIP early-warning (no
email infrastructure exists anywhere in this repo yet — dashboard-only for
now). Interim/automated bill creation was considered and explicitly
rejected — billing stays manual.

## Workflow
```powershell
# Read-only report (writes output/trust_monitor_YYYY-MM-DD.csv + a log)
uv run src/trust_monitor.py
```
The request-review workflow (pause, target override, send) is dashboard-only
via `/trust` — no CLI equivalent, since it's inherently interactive.

---


# Collections

**Modules:** `src/collections_monitor.py`, dashboard page at `/collections`
(`src/web/routes_collections.py`, `src/web/templates/collections.html`)

**Purpose:** Answers "who owes money for work already billed?" — the
counterpart to Trust Monitor's "who's low on retainer for future work?"
Split into its own page 2026-08-11 (Ted): the two are different problems
with different remedies. A trust replenishment request tops up money held
for *future, unearned* work, and can't legally carry the client's card
processing fee. Collecting an overdue bill is a payment for work *already
done*, and the firm can pass that surcharge to the client. `/trust` used to
show both on one table (an "Outstanding" column bolted onto the WIP/cushion
formula) — that read as if the two numbers were related, when they never
were.

**Data:** every Clio `Bill` in `state = awaiting_payment`
(`collections_monitor.fetch_unpaid_bills()`, one `UnpaidBill` per bill —
this part is unchanged). `days_overdue` is computed client-side from
`due_at` vs. today; a bill not yet past its due date shows as "Current,"
not flagged.

**Grouped by matter on `/collections` (restructured 2026-08-25, Ted):**
`collections_monitor.build_matter_summaries()` groups those per-bill rows
into one `MatterBillSummary` per matter — **Total Balance** (sum of that
matter's unpaid bills), **Oldest Issued** (the earliest `issued_at` among
them), and a past-due badge/**Days Overdue** figure taken from whichever of
the matter's bills is individually most overdue (not necessarily the
oldest-issued one — a later bill can still be the most overdue if it had a
shorter payment term). A bill with no matter linked can't be grouped with
anything and becomes its own single-bill summary rather than being dropped.
The summary row has a **▶/▼ expand toggle** (`toggleDetail()` in
`collections.html` — same `<tr>`-pair-plus-JS-toggle pattern
`toggleStaffDetail()` established for Staff Unbilled, reused rather than
inventing a second one) revealing that matter's individual bills
underneath, each with its own Bill #/Issued/Due/Days Overdue/Balance/status
badge. The three sortable columns (Matter/Oldest Issued/Total Balance) move
each summary+detail `<tr>` pair together via their shared `data-detail-id`,
so an expanded matter's detail never gets separated from its own summary
row — sort choice is persisted in `sessionStorage` and reapplied on
`setAction()`'s reload (added 2026-08-25, same day: switching a matter's
Handling reloads the whole page — needed for a fresh live FLARPL/Payment
Plan read — which used to silently reset whatever sort was picked). The
overdue-only filter checkbox now filters at the matter level (any bill
overdue → matter shows) and hides/shows the paired detail row alongside its
summary row.

**Visibility-only, no send action** — same incremental path Trust Monitor
itself started on (report first, action later). An actual "request
payment" action — a payable link the client can pay by card — would need
the **Clio Payments** permission, not currently granted to this app's
Developer Portal registration (see the App Permissions table in `reference/clio-api.md`).
Nothing here writes to Clio at all.

**Handling decisions (added 2026-08-18, Ted):** a **Handling** column on `/collections`
lets staff record how each matter's collections situation is being handled, from a fixed
dropdown (`collections_monitor.COLLECTIONS_ACTIONS`): Keep billing, Escalate to attorney,
Escalate to Heidi, Send to collections agency, Uncollectible and Withdraw — a closed list
rather than freeform text, so the review report reads consistently across every matter,
same reasoning as this project's other explicit-mapping constants. Persisted in its own
`collections_actions` table (own schema fragment, see `web/db.py`'s `_apply_fragment`),
keyed by **matter, not bill** — a matter with more than one unpaid bill still gets ONE
decision, since "how are we collecting this" is a client-level call, not a per-invoice
one (confirmed live: AMOS, CHRISTINE's 3 separate unpaid bills all correctly show the
same decision). Saved via `onchange` on the dropdown (`POST /collections/set-action`,
`collections_monitor.set_action()`) — no separate save button, same instant-persist
pattern as Equalizer's inline editing. Purely local dashboard state; never sent to Clio.
**Lives on the summary row only as of the 2026-08-25 matter-grouping restructure above**
— before that, the same dropdown/value was rendered redundantly on every one of a
matter's bill rows (harmless since they were always kept in sync, but Ted asked for it
to only appear once, on the summary line, once bills stopped being flat per-row anyway).

**Print report for review** (`GET /collections/action-report`) — one summary line per
matter (Matter/Oldest Issued/Total Balance/Handling/Confirmed) with that matter's
individual bills listed in a nested table underneath, always expanded (no JS
toggle — restructured 2026-08-25 alongside the main page's matter-grouping, but print
has no interaction model so everything just prints visible), **Print** button and print
stylesheet, sorted alphabetically by matter `display_number` rather than by
overdue/balance like the main table (Clio's own "Last, First" convention already sorts
by last name, so no separate name-parsing is needed), and **deliberately leaves the
Client column off** — per Ted, every matter today has exactly one client (a 1:1 match),
so the matter name alone is enough and sorting by it is more intuitive than by client.

**Matter row vs. bill sub-table are visually differentiated (added 2026-08-25, Ted):**
matter rows are bold with a top border separating each matter's group; the bill
sub-table underneath is indented and muted gray — both on screen (this page is reviewed
before it's printed) and in the print stylesheet, `tr.matter-row`/`tr.bill-subrow`.
Before this they read identically, with no visual separation between one matter's block
and the next.

**Redesigned 2026-09-08 (Ted: too many horizontal lines, hard to differentiate an
expanded matter's bills):** the old version gave every cell in both the outer table
and every matter's own nested bill table a full-width `border-bottom` (the global
`th, td` rule in `style.css`) — with several matters each showing multiple bills, that
read as a dense grid rather than distinct groups. Now the *only* rule line is each
matter's top border (unchanged from 2026-08-25 above); everything inside a block is
grouped by faint zebra shading (`table.detail-table tbody tr:nth-child(even)`) instead
of ruled lines, both on screen and on paper. Two more changes landed the same day:
- **Matter name and Bill # are now live links into Clio** — the matter name links to
  `{{ clio_base_url }}/nc/#/matters/{matter_id}` (same URL shape Court Calendar Sync's
  client-list report already uses), and each bill number links to
  `{{ clio_base_url }}/nc/#/bills/{bill_id}`, both `target="_blank"`. Only rendered
  when a link target actually exists (`s.matter_id and s.display_number` — a bill with
  no matter still falls back to plain client-name text, same as before); print CSS
  renders these as plain black text, no underline, since they're not clickable on paper.
- **A ▶/▼ expand/collapse arrow was added per matter** (`toggleReportDetail()`,
  same pattern as `/collections`' own `toggleDetail()`), replacing the old
  always-expanded-on-print-only design — collapsing lets staff skim Handling/Balance
  on screen without wading through every bill. Collapsing is screen-only: printing
  force-shows every matter's bills regardless of collapsed state
  (`@media print { tr.bill-subrow.hidden { display: table-row; } }`), so a matter
  collapsed for on-screen review never silently drops off the actual printed page.

**Trust requests split into their own block, links fixed for the no-matter case
(2026-09-09, Ted):** three related fixes to the print report specifically —
- **Trust requests are now a separate alphabetical block below the real invoices**,
  headed by a plain "Trust Requests — not subject to collections" label row (spacing
  only, no rule line, matching the rest of this redesign). Previously every summary —
  invoices and trust requests alike — was sorted together by `display_number`, which
  is blank for a trust request with no matter (the "new client retainer" case — see
  `UnpaidBill.trust_label`), so those rows sorted to the very top out of alphabetical
  order relative to everything else. Ted: confusing to anyone unfamiliar with the page.
  `routes_collections.py`'s `action_report` now builds `invoice_summaries` and
  `trust_summaries` separately (each sorted by the new `display_name`, see next point),
  and the template's row-rendering logic is a Jinja macro (`summary_rows`) called once
  per block instead of duplicated.
- **A trust request with no matter (new client retainer) now sorts and displays as
  "Last, First"**, matching every matter-backed row's own `display_number` convention,
  instead of Clio's raw `client.name` field ("LISA BRANSON", space-order — not a format
  choice, just what Clio calls its `name` field for an individual). New
  `MatterBillSummary.display_name` property + a module-level `_last_first()` helper in
  `collections_monitor.py` do the reformatting; best-effort (splits on the last space),
  not a full name parser — company names and already-comma'd names pass through
  unchanged.
- **A trust request with no matter is now a clickable link too** — it previously
  rendered as plain, unlinked text since there was no matter to link to at all. Fixed
  by linking to the Clio **contact** (`/nc/#/contacts/{client_id}`) instead of a matter
  in that case, since the client always exists even when the matter doesn't yet.
  Requires `MatterBillSummary.client_id`, newly carried through from the bill's own
  `client_id` (`build_matter_summaries` already had it on `UnpaidBill`, just wasn't
  propagated up to the per-matter summary before this).

  **Root cause found (Ted, same day):** the Branson trust request had been issued at the
  CLIENT level (no matter — "Client level trust request", the same "new client retainer"
  case above), but the actual payment got recorded against the MATTER's own trust
  ledger once one existed for her, instead of being applied to the client-level request
  bill. Confirmed live: her matter's `account_balances` Trust entry was $7,935.00 —
  matching the "unpaid" request's balance exactly. Clio has no mechanism that
  reconciles the two automatically; the request bill just stays `awaiting_payment`
  forever unless someone notices and fixes it by hand (which Ted did, in Clio, once
  this was found).

  **New live check added the same day — `MatterBillSummary.trust_level_mismatch`:**
  Ted asked for a way to flag this before it happens again, since a client-level trust
  request existing at all means a matter often gets created for that client shortly
  after (going from prospective to actual client) — exactly when this mixup can occur.
  For every unpaid client-level trust request, `collections_monitor.fetch_matter_trust_balance()`
  makes one targeted `matters.json?client_id=X&status=open,pending,closed` call (same
  `account_balances{type=Trust}` field `trust_monitor.py` already relies on, any status
  since the money could've been recorded before the matter closed) and sums that
  client's Trust balance across all their matters. `trust_level_mismatch` is true when
  that sum is nonzero — surfaced as a red "⚠ Check trust level" badge next to the
  existing "Trust request" badge on both `/collections` and the print report. Deliberately
  a **soft signal, not a conclusion** — confirmed live the same day against Jennifer Roof's
  existing $5,000 client-level trust request (the original bill that surfaced the whole
  three-category quirk, 2026-09-02 below): her matter holds $152.86 in trust, which
  flags the same badge but is obviously not the same $5,000 — could be unrelated trust
  dust, not proof of the Branson-style mixup. The badge's tooltip states the actual
  matter trust balance so staff can judge for themselves rather than trusting the flag
  blindly. Scoped tightly (one call per affected client, not a firm-wide matters sweep)
  — with only 3-4 client-level trust requests open at a time in practice, this adds a
  handful of calls to `/collections`' existing live fetch, not a new class of cost.

**Independent Invoices/Trust Requests checkboxes (2026-09-09):** two checkboxes next
to Print, both checked by default — any combination is valid (both on for the full
report, one on to print just that section, both off to show neither). Implemented as
a `data-section="invoice"/"trust"` attribute per row plus a dedicated `section-hidden`
CSS class (`applySectionFilter()` in the template), kept deliberately separate from
the existing per-matter expand/collapse `hidden` class so the two toggles can't
interfere with each other. Unlike a collapsed matter (which the `@media print` rule
always force-expands — see the redesign note above), an unchecked section stays
hidden in print too, since that's the entire point of the checkboxes.

**CSV export, added 2026-09-09 (Ted: wanted CSV export for any page with a printable
list):** a "Download CSV" button next to Print. Deliberately a **separate CSV from
`/collections/download`'s own** (that one is bill-level with no Handling/Confirmed
columns — wouldn't actually match this page). New
`collections_monitor.write_action_report_csv()` mirrors this page's table exactly:
same `Section` values (Invoice/Trust Request) and order as the two on-screen blocks
above, same Handling/Confirmed columns. The one difference forced by CSV's own
shape: the page's single "Total Balance" cell stacks up to three $ figures
(earned/replenishment/new-trust) — the CSV splits those into three real columns
instead, since a spreadsheet cell should hold one value. A `trust_level_mismatch`
flag (see above) has no dedicated page *column* to mirror — it's a badge on the
Matter cell — so it's appended as text to that same cell in the CSV instead (e.g.
`ROOF, JENNIFER — CHECK TRUST LEVEL (matter holds $152.86)`), keeping the CSV a
faithful text rendering of what's on screen rather than inventing a new column the
page doesn't have. Same "written to `output/` on page load, served by its own
`/download` route" pattern as every other CSV in this app.

**Three bill categories, not one flat "unpaid bill" list (added 2026-09-02, Ted):**
a real Clio quirk surfaced while investigating a bill with no matter shown (JENNIFER
ROOF, bill #30285, $5,000) — it had no matter linked, but Clio's `Bill.kind` field
(`revenue_kind` vs `trust_kind`) and `type` (`MatterBill` vs `ClientBill`) reveal it's
not billed work at all, it's a trust request. Confirmed live across all 233 current
unpaid bills: 219 are `revenue_kind` (billed work, always `MatterBill`), 14 are
`trust_kind` — 10 tied to a matter (`MatterBill`, a trust replenishment on an existing
matter) and 4 with no matter (`ClientBill`, a brand new client's initial retainer).
`UnpaidBill.category` (`"earned"` / `"replenishment"` / `"new_trust"`) and
`UnpaidBill.overdue` (always `False` for a trust-kind bill, regardless of `due_at`) are
computed **per bill, not per matter** — a matter can carry both an earned invoice and a
trust top-up bill at once (confirmed live: SUAREZ/RANDS/DONOVAN/STLUKA all had exactly
this mix), so grouping by matter alone couldn't separate them.

Nothing is dropped from the table — same show-everything-and-badge convention as the
rest of this doc — but the three categories are kept out of each other's totals:
- **Earned** (billed work with a balance due) — the actual collections AR, this page's
  "earned & owed" headline figure and the only thing that drives `overdue`/the past-due
  badge/the overdue-only filter.
- **Replenishment** — a trust top-up request on an *existing* matter. Care about it, but
  it isn't earned yet, so it's excluded from the earned total and never shown as past due.
- **New client retainer** — a brand new client's initial deposit (no matter yet). Not
  subject to collections at all, per Ted.

The header's summary bar (`/collections`) is now exactly three dollar figures (earned &
owed / replenishment / new client retainers) — the older matter-count/past-due-count
stats were removed 2026-09-02 at Ted's request ("we do not need a summary"). A
matter/client row whose bills are entirely one non-earned category gets a "Trust
request" badge instead of a past-due/current pill (tooltip distinguishes the two
subtypes); a mixed matter keeps its normal overdue badge (driven by the earned portion)
and splits its Balance column into an earned figure plus muted "+ $X replenishment" /
"+ $X new client retainer" lines rather than one blended total. Same three-way split
applies to the print report's Total Balance column and the CSV's per-bill "Type" column.

**Long Handling text was getting silently cut off on paper (fixed 2026-08-25) —**
`table.nowrap`'s `white-space: nowrap` plus a printed page's fixed width doesn't wrap
overflow text, it just clips it with no visible sign anything was truncated: "Claim as
uncollectable and withdraw" printed as "Claim as uncollectable and w". Fixed two ways —
that option was shortened to **"Uncollectible and Withdraw"** (`COLLECTIONS_ACTIONS`,
with a SCHEMA rename migration covering both of its prior names for existing rows,
same pattern as the FLARPL/Payment plan rename migrations elsewhere in this file), and
`#report-table`'s Handling/Confirmed columns now get `white-space: normal` specifically
(scoped by id so the bill sub-table's own short columns stay `nowrap`) so any other
long value (`"Payment from sale of home"`, `"Send to collections agency"`) wraps
instead of clipping if this ever comes up again.

**FLARPL and Payment plan — Handling records intent only; confirmation is never set
from this dashboard (added 2026-08-18, corrected 2026-08-19):** the **Handling** dropdown
records the firm's own INTENTION (e.g. "we're going to pursue a FLARPL" / "we're setting
up a payment plan") — neither value means the thing has actually happened yet. A first
version of this feature added a **Status** checkbox next to Handling that staff could
tick directly from the dashboard; corrected same day (Ted) — recording a FLARPL is an
external act (filed with the county), not something that should be settable by clicking
a checkbox here. Current design:

- **FLARPL** (Family Law Attorney's Real Property Lien — a lien on real property to be
  sold later, securing fees) — its **Confirmed** column shows Clio's own **real matter
  custom field** ("FLARPL Recorded", id `19226673`, `field_type: checkbox`, confirmed
  live 2026-08-18), **read-only** (`collections_flarpl.py` — no write function exists in
  this codebase anymore; staff flip it in Clio itself once the county confirms
  recording, and this dashboard just reflects that back). Only batch-fetched for matters
  currently showing "FLARPL" (`collections_flarpl.fetch_recorded_by_matter()`, one
  `ids[]` call), not for every unpaid-bill matter. Live-tested 2026-08-18 (before the
  write path was removed) against the designated test matter (DOE, JANE): set true,
  confirmed true via a direct Clio read, reverted to false, confirmed reverted — see
  "Designated test matter" above for why that matter specifically.
- **Payment plan** — at first (2026-08-19) had no matching Clio custom field (confirmed
  live, searched "Payment Plan", "Payment", "Installment", "Plan", "Schedule" —
  nothing), so it had **no Confirmed-column indicator at all** rather than a
  dashboard-only flag standing in for one; a first version gave it a locally-writable
  "Active" checkbox (a `collections_actions` DB column, also named `payment_plan_active`
  — coincidentally the same name the field below reuses, but a different, unrelated
  thing), removed same day as the FLARPL fix for exactly the same reason: a checkbox
  with no external truth behind it is the pattern being avoided, not a narrower
  exception to it.

  **Ted added a real "Payment Plan" matter custom field 2026-08-25** (id `19347918`,
  `field_type: checkbox`) — Clio has no API for payment plans themselves, but this
  field is now the same kind of confirmation source FLARPL already has. Wired up the
  same way: `collections_payment_plan.py` (read-only, no write function, same
  reasoning as `collections_flarpl.py`), `UnpaidBill.payment_plan_active`, batch-fetched
  only for matters currently showing "Payment plan". Live-tested 2026-08-25 against
  the designated test matter (DOE, JANE): set true, confirmed true via a direct Clio
  read, reverted to false, confirmed reverted. Hit the same "already exists" gotcha
  Moore/Marsden's date fields hit (see that section) — DOE, JANE already had an
  auto-created empty `CustomFieldValue` record for this field once the first PATCH
  ran, so the revert PATCH had to target that record's own id
  (`{"id": "checkbox-1122682188", "value": false}`) rather than re-using the
  `custom_field: {id}` create-shape a second time.

## Workflow
```powershell
# Read-only report (writes output/collections_monitor_YYYY-MM-DD.csv + a log)
uv run src/collections_monitor.py
```
Or just visit `/collections` — same live pipeline, rendered as a table.

---


# Staff Unbilled Report

**Modules:** `src/staff_unbilled_monitor.py`, dashboard page at
`/staff-unbilled` (`src/web/routes_staff_unbilled.py`,
`src/web/templates/staff_unbilled.html`) — added 2026-08-17 (Ted). The
dashboard page is one summary row per staff member (at-risk matter count,
total unbilled, total shortfall, total owed on their own matters) that
expands on click into the underlying per-matter rows — matter name,
client, unbilled activity, matter WIP, trust balance, shortfall, owed —
via a plain `<tr>`-pair-plus-JS-toggle (`toggleStaffDetail()` in the
template; no accordion/expand pattern existed elsewhere in this codebase
to reuse, so this is the first one). `staff_unbilled_monitor.
build_user_summaries(rows)` does the grouping (one `UserSummary` per staff
member, `rows` sorted by descending unbilled) and is shared by both the
CLI's console summary and the dashboard route — not duplicated logic.

**Purpose:** Answers "who is billing time that's at risk of not getting
collected?" — a per-staff-member/per-matter cut Clio's own reporting UI
doesn't offer directly. Started 2026-08-17 as a flat "who's working on
matters that owe money" list; same day, Ted asked for the nuance that
actually makes it useful — filter out anyone whose client still has
retainer funds available, leaving only the staff/matter combinations
genuinely at risk of going uncollected.

**At-risk filter — the whole point of this report, not a display option:**
a staff/matter row only appears if that matter's Clio **trust balance is
less than its real WIP** (`trust_monitor.fetch_billable_matters_unbilled()`
never-billed amount + `trust_monitor.BILL_WIP_STATES`
draft/awaiting-approval bill totals — the exact same WIP definition
`trust_monitor.py` already established and audited, reused via
`trust_monitor.build_trust_statuses()` rather than recomputed). This is
deliberately **buffer-free** — a sharper, different condition from
`/trust`'s own $2,500-cushion "flagged" early warning. Confirmed live
2026-08-17 on a real matter (BERNARD, LIESL): trust $2,364.70 against WIP
$2,365.00 — a $0.30 shortfall — correctly appears here despite being nowhere
near $2,500 under water, proving no minimum-buffer threshold leaked in.
Matters where trust still covers WIP are filtered out **entirely**, not
just badged — that was the explicit ask ("filter out people who have
retainer funds available"), a deliberate deviation from this project's
usual show-everything-and-badge-the-risk convention
(`trust_monitor.py`/`collections_monitor.py` both list every matter/bill
and flag the risky ones instead of dropping the safe ones). Matters
`trust_monitor.build_trust_statuses()` itself excludes (the firm-overhead
client, or a genuinely zero trust/WIP/outstanding matter) are skipped here
too, logged as their own count — neither is a real client to be "at risk
of not collecting" from.

**Data — figures per row:**
- **Unbilled Activity** (per-staff) — the *staff member's own* unbilled
  work on that matter (`GET /activities.json?status=unbilled`, summed by
  `user{id,name}` + `matter{id}`). Clio's `status` enum keeps `unbilled`
  and `non_billable` as distinct values, so this is billable-only with no
  extra client-side filtering needed. Confirmed live 2026-08-17: summing
  this figure across all staff for a given matter reproduces
  `trust_monitor.py`'s own `unbilled_amount` for that matter exactly (two
  real matters checked, KAHELE and HAMMER, byte-for-byte) — the same
  underlying Clio definition, just re-sliced by user instead of only by
  matter.
- **Matter WIP / Trust Balance / Shortfall** (matter-level) — WIP and
  trust balance are `trust_monitor.py`'s own figures, reused directly;
  Shortfall = `max(0, Matter WIP − Trust Balance)`, i.e. how much of the
  matter's current WIP the retainer *doesn't* cover (always > 0 here since
  only at-risk rows survive the filter).
- **Owed (Outstanding Bills)** (matter-level) — reuses
  `trust_monitor.fetch_bills_by_matter()` directly (not reimplemented)
  called with `state=awaiting_payment`, `amount_field=balance` — the exact
  same figure `collections_monitor.py` reports per bill. Confirmed live
  matches (POJUNIS, JOSEPH: $1,026.30 in both reports).

**Matter WIP, Trust Balance, Shortfall, and Owed are matter-level, not
per-user splits** — none of them are tied to one specific staff member's
work, so if two staff both have unbilled activity on the same at-risk
matter, both rows show the *same* values for all four. Summing any of
those columns across every row double-counts; only sum "Unbilled Activity"
across rows for a genuine per-staff total.

**Scope:** open matters only, same convention as every other subproject
(Trust Monitor, Printer Expenses, etc.). An unbilled activity attached to
a non-open matter is skipped and counted in the log rather than silently
dropped.

## Workflow
```powershell
# Read-only report (writes output/staff_unbilled_YYYY-MM-DD.csv + a log)
uv run src/staff_unbilled_monitor.py
```
Or just visit `/staff-unbilled` — same live pipeline, rendered as an
expandable table; "Download CSV" there serves the same-day CSV the CLI (or
the page's own live run) already wrote to `output/`.

---

