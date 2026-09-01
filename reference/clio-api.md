> Full Clio API permission table and the pagination gotcha. Core auth commands,
> safety rules, and the designated test matter live in the root `../CLAUDE.md` —
> read those first; come here only when you need the full permission list or hit
> a pagination bug.

Full permission list (Clio's own descriptions, for reference — each is independently
Read/Write toggleable per app):

| Permission | Description |
|---|---|
| Activities | Activities, time entries, expenses, timers, UTBMS codes |
| Accounting | Bank account and bank transaction information |
| Api | API |
| Billing | Bills, billable clients, bill themes, line items |
| Calendars | Calendar entries and reminders you have edit permission on |
| Communications | Logged phone calls, emails, secure messages |
| Contacts | Clients, companies, external co-counsel — includes notes/log entries on contacts |
| Court rules | Court Rules. **Available on select plans only** |
| Custom fields | Custom fields recording extra info on contacts and matters |
| Documents | Documents and folders uploaded/created in Clio, including Document Templates |
| Imports | Imports from Activities, Calendars, Contacts, Matters, Notes, Tasks into the firm's account |
| General | General |
| Matters | Matters, including notes and practice areas |
| Payment distributions | Payment info on bills, trust payments, credit memos, allocations |
| Reporting | Reports generated in Clio |
| Settings | Setting preferences, including text snippets and bill settings |
| Tasks | Tasks, task lists, task types — priority, due date, reminder details |
| Users | User/group info with login ability (not Clio Connect users) |
| Webhooks | Any information a webhook has been created against |
| Custom actions | Create Custom Actions within Clio, scoped to actions this app created |
| Client share permissions | Client share permissions |
| Grants | Grants within Clio (Legal Aid US Services) |
| Personal injury | Medical Records Details, Medical Records, Medical Bills, Damages, Liens |
| Clio payments | Create payment links, access resulting payment details |

**This app currently has:** Matters, Activities (the original `matters activities`
scope), plus **Billing (Read)** — granted 2026-07-22 for Bradford's live matter-rate
display, see `reference/invoices.md` (Bradford Invoice Import) — and **Accounting** —
granted 2026-07-30 for Trust Monitor's `account_balances` fetch, see
`reference/billing-monitors.md` (Trust Monitor & Replenishment Requests).
(A prior version of this note listed only Matters/Activities and
wasn't updated as those two were added.) **Confirmed missing:** Court Rules —
`GET /court_rules/*` returns `403 Forbidden` until it's checked in the Portal for this
app (not currently needed — see Court Rules Automation, cancelled).

### Pagination gotcha
`meta.paging.next` in a Clio API list response is a **full URL** (query params already
embedded, including `page_token`) — fetch it as-is. Re-extracting a token and rebuilding
your own params dict around it produces a URL nested inside a query param value, which
Clio rejects with `"page_token is invalid"` once there's a page 2. `matter_matching.py`
and `clio_users.py` both follow `next` directly for this reason.
