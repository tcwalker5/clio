> Auto-loaded by Claude Code when working in `src/moore_marsden/`. Root map: `../../CLAUDE.md`.

# Moore/Marsden Calculator

**Modules:** `src/moore_marsden/`, dashboard pages at `/moore-marsden`
(`src/web/routes_moore_marsden.py`, `src/web/templates/moore_marsden.html`,
`moore_marsden_matter.html`, `moore_marsden_worksheet.html`,
`src/web/static/moore_marsden.js`)

**Purpose:** Calculates the community's interest in a spouse's separate-
property real estate under California's Moore/Marsden doctrine (*In re
Marriage of Moore*, 28 Cal.3d 366 (1980); *In re Marriage of Marsden*, 130
Cal.App.3d 426 (1982)) — a Python/Clio port of a legacy Excel workbook,
built 2026-08-17 following the same worksheet/PDF/Save-to-Clio pattern
Equalizer established. Same dashboard-only shape as Equalizer: item CRUD is
small autosaving JSON endpoints, not a submit-and-reload form; Preview
regenerates the PDF live and never touches Clio; Save to Clio is repeatable,
not a one-way lock.

**The legacy report is not the single-period formula — it's a chained,
multi-refinance calculation**, one "Step" per refinance event, each
resetting the community-percentage calculation against a new basis. The
plain single-period textbook formula (community % = principal reduction ÷
original purchase price, applied once) is just the special case of this
worksheet with zero refinance rows.

**Calculation (`moore_marsden/calc.py`), reverse-engineered from a real
legacy report and verified to the penny** against all of its real segments
(`tests/test_moore_marsden_calc.py`; labels in that fixture are generic
placeholders, not the source document's client name — see the "no client
names from redacted documents" rule below):

```
for each period (a refinance or the final valuation row), in order:
    basis = property value at the START of this period (the purchase price,
            or the prior period's ending/refi value)
    appreciation = end_value - appreciation_start
        (appreciation_start is normally == basis, except the very first
        period when the property was acquired before marriage — there, it's
        the value at date of marriage instead, so premarital appreciation
        stays separate property; the community-percentage denominator is
        still the original purchase price either way)
    segment_cpr = cumulative_community_interest_so_far + this_period's_own_principal_paydown
    community_pct = segment_cpr / basis
    community_appreciation = max(0, community_pct * appreciation)  # floors at
        # $0 on a depreciation period — separate property absorbs the loss,
        # not community; confirmed against the legacy report's own Step 1,
        # a depreciation period whose displayed community appreciation is $0
        # even though the real computed percentage is nonzero
    cumulative_community_interest_so_far = segment_cpr + community_appreciation

total_community_interest = cumulative_community_interest_so_far after the last period
each_spouse_share = total_community_interest / 2
```

Staff type each period's own **raw** principal paydown (pulled from
mortgage statements, per this doctrine's "Records you will need" list) —
`calc.py` carries the running cumulative total forward itself, rather than
requiring staff to hand-sum it before typing it in the way the legacy
Excel tool's preparer had to. This was confirmed by reverse-engineering the
legacy report's own numbers: its displayed "Community Principal Reductions"
figure at each step is already cumulative-through-that-step, not
period-only, which is a real usability gap in the original tool this port
fixes rather than replicates.

**Two things flagged for sign-off, not just silently assumed** (matching
this project's practice of naming an interpretation gap rather than
guessing past it — see Equalizer's after-tax apportionment note for the
same pattern): the "acquired before marriage" input branch is derived from
doctrine, not verified against a second real report (the one report this
was built from happens to be a purchase-during-marriage fact pattern, so
that branch never gets exercised by it); and the separate-property (SP)
side of the report is intentionally simplified to a single reconciling
**SP Total** (`property_value - cumulative_community_interest`, correct by
construction) rather than exactly replicating the legacy tool's SP-column
sub-line breakdown (down payment / principal reduction / appreciation /
loan balance), whose exact accounting convention couldn't be fully pinned
down from one example — the community-interest math itself is unambiguous
and verified.

**No client names from redacted source documents.** The legacy report used
to reverse-engineer this formula had its client-identifying header
redacted, but the redaction failed on one page — that name must never be
propagated into code, docs, memory, tests, or commit history. Test fixtures
and any future documentation referencing that report use generic labels
("the sample report," placeholder party names) instead.

**Data model** (`moore_marsden_worksheets`/`moore_marsden_segments`/
`moore_marsden_capital_improvements` in `data/clio_dashboard.db`) — one
worksheet per calculation, tied to a Clio matter; segments are ordered rows
chaining the calculation: exactly one `purchase` row (position 0), exactly
one `valuation` row (last), zero or more `refinance` rows between them.
`owner_spouse_label`/`non_owner_spouse_label` autofill from the matter's
client + Opposing Party contact (`clio_parties.py`, same OC/OP lookup
pattern as Equalizer's and Outlook Calendar Migration's). Equity/
percentage/appreciation are never stored — computed at read time from the
segment rows, so they can't drift from their inputs. The Purchase row's
"Purchase Price" field carries a tooltip spelling out the standard
Moore/Marsden denominator rule: contract sales price only, not total cash
needed to close — down payment/mortgage are already reflected in that price
(don't add again), and escrow/title/recording fees, loan points, and
prepaid interest/tax/insurance are transaction/financing/carrying costs
that don't belong in the denominator at all.

**Date of Marriage / Date of Separation are never stored locally — they're
real Clio matter custom fields** ("Date of Marriage" id `18509746`, "Date
of Separation" id `18509761`, both `date` type, confirmed live 2026-08-18),
read fresh from the matter on every page load/mutation
(`moore_marsden/clio_matter_dates.py`) and written straight through to Clio
whenever staff enter or correct one via Settings — never cached locally, so
there's nothing that can drift from the matter itself. `value_at_date_of_marriage`
(the dollar figure, not the date) stays a local worksheet field since it has
no Clio equivalent.

**Real Clio gotcha, different from Court Case Number's own precedent:**
writing these date fields can't always use the "pass `custom_field: {id}`,
Clio creates-or-updates" shape `court_calendar/clio_matter_update.py`'s
Court Case Number path relies on — DOE, JANE already had an empty
(`value: null`) `CustomFieldValue` *record* for Date of Separation (its own
id, like `"date-1372717531"`), and POSTing a fresh one via `custom_field:
{id}` against a matter that already has one 422s: `"custom field value for
custom field ... already exists"`, even though the read side shows `null`.
Text fields apparently don't get this auto-created empty placeholder on
every matter; these date fields do (likely intake-form configuration).
Fixed by checking for an existing value id first and updating *that*
record's own id (`{"id": "date-1372717531", "value": ...}`, no
`custom_field` key) when one exists, falling back to the `custom_field:
{id}` create-shape only when genuinely no record exists yet for that field
on that matter — worth remembering if another subproject ever writes a
different custom field and hits the same 422.

**Capital improvements are tracked separately from the segment chain**
(`moore_marsden_capital_improvements`, added 2026-08-18) — per *In re
Marriage of Allen*, 96 Cal.App.4th 497 (2002), a community-funded
improvement to separate property needs its own source/timing/treatment
analysis, not a blanket add to purchase price or an ordinary period's
principal reduction. Each line item (date, description, amount, `funded_by`
SP/CP, `treatment` reimbursement/pro-tanto) is entered as its own row,
separate from the purchase/refinance/valuation chain. Only `funded_by='cp'`
rows affect the community interest at all — an SP-funded improvement to the
owner spouse's own separate property creates no community interest and is
logged for the record only. `treatment` is a per-item staff judgment call
(Ted, 2026-08-18: "let staff choose per improvement" rather than the tool
assuming one theory), since the two are genuinely different math, not just
a display choice:
- **Reimbursement** — the dollar amount is added straight to the *final*
  total, computed after the whole segment chain — no appreciation share,
  since the community is just being repaid what it spent. Never folded into
  the chain itself; doing so would let it pick up a proportional share of
  *later* appreciation the next time `community_pct` is computed, which
  would silently make it behave like pro-tanto instead.
- **Pro-tanto** — folded into whichever segment period contains the
  improvement's `event_date`, exactly like that segment's own
  `cp_contribution` — so it shares proportionally in appreciation from that
  point forward, the same way ordinary principal reduction does.
  `calc._bucket_period_index()` places an improvement into a period by
  comparing its date against segment boundary dates (plain ISO string
  comparison); an improvement whose date can't be placed (missing, or
  before the purchase date) is excluded from the total rather than guessed
  at — `calc.unbucketed_improvements()` surfaces these so the UI/PDF can
  flag them explicitly (a red-flagged row + a warning banner) instead of
  letting the dollar amount silently vanish.

`compute_final()`'s `WorksheetTotals` exposes `segment_chain_total` and
`reimbursement_total` alongside the grand `total_community_interest`, so the
UI and PDF can always show the breakdown rather than one opaque number
whenever a reimbursement exists.

**A period whose date range straddles the matter's Date of Separation gets
a passive warning**, not an automatic split — `calc.py`'s `spans_separation`
flag (per non-purchase segment) just prompts staff to double check that the
principal-reduction figure they typed for that period only reflects
pre-separation community activity, since post-separation earnings are
generally separate property (Fam. Code §771). The tool doesn't attempt to
auto-split a segment at the separation date.

**Schema ownership** — `moore_marsden/store.py` owns its own `SCHEMA`/
`SCHEMA_COLUMNS`, applied by `web/db.py`'s `get_connection()` in its own
isolated try/except (see that file's `_apply_fragment()`). Building this
subproject prompted a real architecture change to `web/db.py`
(2026-08-17): every subproject's tables used to live in one shared `SCHEMA`
string, run through a single `executescript()` call — meaning a typo in
any one subproject's `CREATE TABLE` would throw and take down
`get_connection()` for the *entire* dashboard, not just the subproject that
broke. Equalizer's schema was split out to the same per-module-fragment
pattern at the same time, as the first proof that the mechanism
generalizes; the remaining pre-split tables (court events, purpose
mappings, staff cache, RingCentral sync history, trust requests) still live
in `web/db.py`'s own `CORE_SCHEMA`, applied through the identical
isolated-fragment mechanism as its own "core" entry — not yet broken out to
their owning modules, low urgency since they already get the same fault
isolation this way.

**Clio integration** — same pattern as Equalizer: `clio_documents.py`
(near-identical to Equalizer's, already fully generic — Document
create/PUT/PATCH upload flow, presigned-URL headers, trash detection) and
`clio_notes.py` (posts a matter Note on first save, linking back to
`{CAP_BASE_URL}/moore-marsden/{id}`) are separate per-subproject copies
rather than shared imports, matching this project's existing convention
that each subproject owns its own Clio-writing helpers.

## Workflow
Dashboard-only, no CLI equivalent — visit `/moore-marsden`, search for a
matter (opens its existing worksheet(s) or starts a new one), fill in the
Purchase row (contract price, not total cash to close), add Refinance rows
for each real refinance (or none, for the single-period case), fill in the
final Valuation row, add any Capital Improvements as their own line items,
confirm Date of Marriage/Separation in Settings (auto-filled from the
matter if already set there, otherwise typed in once and written back),
optionally mark "acquired before marriage," Preview to check the PDF, then
Save to Clio whenever it's ready — and again anytime after, since editing
continues.

---

