[TODO.md](https://github.com/user-attachments/files/27570301/TODO.md)
# TODO

## Open Items

### 1. PDF reader for document files (foreclosures + city liens)
**Priority: high — biggest remaining unlock**

This is the next milestone. Reading the actual PDF documents linked
from clerk records gives us several fields we can't get any other way.

For **foreclosures** (high-value):
- Owner name (currently "—" on the dashboard — required for marketing)
- Real street address (currently we only have the legal subdivision
  description like `LT 9 BK 2 DOUGLAS UNIT TWO`)
- Loan amount
- Once we have a street address, run NCAD reverse-lookup-by-address
  to fill in mailing address (where different)

For **city liens** (medium-value bonus):
- **Consideration / lien amount** — the dollar amount the city is
  charging the homeowner. Visible on each detail page in the portal,
  but the detail-page URL uses an internal ID we can't map to
  doc_num without authentication. CSV export would solve it but is
  login-gated. PDF reading bypasses both — the AFFIDAVIT OF LIEN
  document itself contains the amount, owner, real address, and
  fuller legal description.

#### Implementation plan

1. For each pre-foreclosure / CCLN record, follow `clerk_url` (the
   doc-link in our captured data) to the document detail page on the
   clerk portal
2. Download the PDF (the portal exposes a download button or PDF URL
   when authenticated; for unauthenticated access, OCR the page-image
   that's rendered in the document viewer)
3. Run OCR (`pytesseract` is the simplest open-source option, but
   accuracy varies on scanned forms — consider AWS Textract or a
   similar paid API if quality matters)
4. Extract owner name, loan amount, real street address, consideration
   via regex / template matching on the OCR'd text
5. Populate `ForeclosureRecord.owner`, `loan_amount`, `prop_address`,
   `prop_city`, `prop_zip` (foreclosures) or
   `ClerkRecord.amount` for CCLN
6. Run NCAD reverse-lookup-by-address to fill in mailing address

#### Open design questions for the implementer

- Document detail URLs use an internal database ID (e.g.
  `/doc/169217155`), not the human-readable doc_num. The search-
  results page exposes this internal ID via a result row's link —
  but rows currently don't render anchor tags in the static HTML
  (likely added via JS click handlers). Will need to either find
  the internal ID via Redux state inspection, click rows
  programmatically and capture the resulting URL, or use the
  authenticated CSV-export endpoint to get URLs for all docs at once.

- The portal's PDF viewer renders images, not raw PDFs. Two options:
  (a) reconstruct the PDF from the page images, (b) OCR each image
  directly. (b) is simpler.

### 2. Migrate CRM data from localStorage to GitHub-backed storage
**Priority: high — required for mobile / multi-device access**

The CCLN CRM tab currently stores status, notes, and last-contact date
in the browser's localStorage. That works fine on a single computer
but won't sync to a phone or another laptop.

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

### 3. Build out the full CRM
**Priority: medium — current CRM is intentionally minimal**

The CCLN CRM tab is a v1 — it has status, notes, last-contact-date.
The full vision includes:

- **Apply CRM features to all lead types**, not just CCLN. Right now
  only the City of Corpus Christi tab has the side-drawer + status
  workflow. Eventually every record (judgments, liens, foreclosures,
  etc.) should support per-lead status tracking.
- **Scheduled follow-ups / callbacks** — when a status is set to
  "Contacted" or "Negotiating", let the user pick a date+time for a
  follow-up reminder. Surface upcoming follow-ups on a dedicated tab.
- **Call-log / activity timeline** — every status change, every note
  save, every follow-up completion gets logged with a timestamp.
  Visible in the side drawer as a chronological timeline.
- **Pipeline value calculation** — for each lead in "Negotiating" or
  "Closed" status, capture estimated deal value. Sum across the
  pipeline for a forecast number on the stats bar.
- **Email / SMS integration** — one-click outreach. Probably via
  Twilio (SMS) and SendGrid or AWS SES (email). Templates per status.
- **Kanban view** — alternative to the table: cards grouped by status,
  drag-and-drop to change status. Better for visual pipeline review.
- **Multi-user / sharing** — if more than one person works the leads,
  attribute notes/status changes to a user. Requires the GitHub-backed
  or Cloudflare-backed storage from item 2.

### 4. Eventually clean up legacy categories
The user noted they'll review the kept-but-unused legacy categories
(Tax Deed, Federal/IRS Tax Lien, Medicaid Lien, Probate, Notice of
Commencement, Release of Lis Pendens) and decide which to drop. These
currently still run as keyword searches in `KEYWORD_CATEGORIES`.

### 5. Show grantee column in dashboard
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
- Persistent JSON cache for esearch results, with token-refresh logic
  to prevent silent expiry
- Owner-swap logic for IRS, banks, courts, debt collectors, etc.
- Dashboard with leads table, search, filters, sort
- Mortgage Foreclosure separate stream + tab
- Per-portal-filter category fetches (`_docTypes=L3` etc.)
- City of Corpus Christi liens — separate persistent file + CRM tab
- 24-month one-shot backfill script for CCLN
- Basic CRM v1: status, notes, last-contact, dead-lead filter, CSV export
- Page size bumped to 250 (portal max) — 5x faster pagination

## Investigated and parked

- **Consideration / lien amount on CCLN tab**: investigated three paths
  — (a) parse from search-results JSON XHR (data not in payload),
  (b) parse from card-view DOM (toggle button selector unstable),
  (c) per-document detail page fetch (URLs use internal IDs not
  exposed in static HTML; SPA hydration timing makes scraping
  unreliable). CSV export of all results works but requires login.
  Decision: defer until item (1) PDF reader is built — the AFFIDAVIT
  OF LIEN PDF contains the amount and is parseable as part of that
  work item.
