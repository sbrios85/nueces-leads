[TODO.md](https://github.com/user-attachments/files/27547964/TODO.md)
# TODO

## Open Items

### 1. PDF reading for foreclosure documents
**Priority: high — required for actionable foreclosure leads**

Currently the Foreclosures tab captures Sale Date, Recorded Date, Doc Type,
Doc Number, and a legal description (e.g. `LT 9 BK 2 DOUGLAS UNIT TWO`).
The actual document PDF — accessible from the clerk portal via the doc-link
column — contains the **owner name, loan amount, real street address, and
fuller legal description**.

Build a step that, for each pre-foreclosure record:
1. Follows `clerk_url` (the doc-link in our captured data) to the document
   detail page on the clerk portal
2. Downloads the PDF
3. Runs OCR (likely `pytesseract` or a similar OSS lib, or a paid API if
   accuracy matters more than cost)
4. Extracts: owner name, loan amount, real street address
5. Populates `ForeclosureRecord.owner`, `loan_amount`, `prop_address`,
   `prop_city`, `prop_zip`, and runs the NCAD reverse-lookup-by-address
   to fill in mailing address (where different from property address)

This unlocks marketability — without it, the Foreclosures tab tells you
*when* the auction is but not *who to call*.

### 2. Reverse lookup (property address → owner) for foreclosures
Tied to (1): once we have a real property address from the PDF, query
NCAD esearch by `PropertyAddress:<addr>` to get the property owner and
mailing address. The query format is the same as our existing owner
search but with a different keyword scope.

### 3. Migrate CRM data from localStorage to GitHub-backed storage
**Priority: high — required for mobile / multi-device access**

The CCLN CRM tab currently stores status, notes, and last-contact date in
the browser's localStorage. That works fine on a single computer but
won't sync to a phone or another laptop.

Two paths forward (decide one):

**Path A: Direct GitHub commit from the dashboard**
- User generates a GitHub Personal Access Token (PAT) once
- Dashboard prompts for the PAT (stored in localStorage), then makes
  authenticated calls to the GitHub API to commit `data/crm_state.json`
  whenever the user changes a status / saves notes
- Pros: works from any device, no backend
- Cons: PAT management; users have to be admins of the repo; rate limits

**Path B: Tiny backend (e.g. Cloudflare Worker + KV)**
- Deploy a small worker that exposes GET/PUT for `crm_state.json`
- Dashboard calls the worker; worker writes to a KV store
- A nightly workflow syncs the KV state into the repo for backup
- Pros: clean UX, no PAT prompts, syncs across all users immediately
- Cons: requires a Cloudflare account, slight ongoing cost

Recommend **Path B** for production use — UX matters when the user is in
the field on a phone. Path A is fine for a personal-use tool.

When this lands, the dashboard's CRM logic switches from
`localStorage.getItem(CRM_KEY)` to a fetch-based store. Migration: read
existing localStorage on first run, push to backend, then forget the
local copy.

### 4. Build out the full CRM
**Priority: medium — current CRM is intentionally minimal**

The CCLN CRM tab is a v1 — it has status, notes, last-contact-date.
The full vision includes:

- **Apply CRM features to all lead types**, not just CCLN. Right now only
  the City of Corpus Christi tab has the side-drawer + status workflow.
  Eventually every record (judgments, liens, foreclosures, etc.) should
  support per-lead status tracking.
- **Scheduled follow-ups / callbacks** — when a status is set to "Contacted"
  or "Negotiating", let the user pick a date+time for a follow-up reminder.
  Surface upcoming follow-ups on a dedicated tab.
- **Call-log / activity timeline** — every status change, every note save,
  every follow-up completion gets logged with a timestamp. Visible in the
  side drawer as a chronological timeline.
- **Pipeline value calculation** — for each lead in "Negotiating" or "Closed"
  status, capture estimated deal value. Sum across the pipeline for a
  forecast number on the stats bar.
- **Email / SMS integration** — one-click outreach. Probably via Twilio
  (SMS) and SendGrid or AWS SES (email). Templates per status.
- **Kanban view** — alternative to the table: cards grouped by status,
  drag-and-drop to change status. Better for visual pipeline review.
- **Multi-user / sharing** — if more than one person works the leads,
  attribute notes/status changes to a user. Requires the GitHub-backed
  or Cloudflare-backed storage from item 3.

### 5. Eventually clean up legacy categories
The user noted they'll review the kept-but-unused legacy categories
(Tax Deed, Federal/IRS Tax Lien, Medicaid Lien, Probate, Notice of
Commencement, Release of Lis Pendens) and decide which to drop. These
currently still run as keyword searches in `KEYWORD_CATEGORIES`.

### 6. Show grantee column in dashboard
When the grantor/grantee swap doesn't fire (because the grantor isn't
recognized as institutional), the user can't see who's on the other
side of the recording. Adding a Grantee column to the leads table —
or showing both fields — would make uncaught swaps recoverable
manually.

## Done

- Initial pipeline (clerk portal scraping, NCAD bulk attempt, scoring)
- 30-day lookback window
- Legal-description address extraction for clerk records
- NCAD esearch (per-name property lookup) integration
- Persistent JSON cache for esearch results
- Owner-swap logic for IRS, banks, courts, debt collectors, etc.
- Dashboard with leads table, search, filters, sort
- Mortgage Foreclosure separate stream + tab
- Per-portal-filter category fetches (`_docTypes=L3` etc.)
- City of Corpus Christi liens — separate persistent file + CRM tab
- 24-month one-shot backfill script for CCLN
- Basic CRM v1: status, notes, last-contact, dead-lead filter, CSV export
- Page size bumped to 250 (portal max) — 5x faster pagination
