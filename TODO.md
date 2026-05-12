[TODO.md](https://github.com/user-attachments/files/27653434/TODO.md)
# TODO

## Open Items

### 1. Migrate CRM data from localStorage to GitHub-backed storage
**Priority: high — required for mobile / multi-device access**

The CCLN CRM tab currently stores status, notes, and last-contact date
in the browser's localStorage. That works fine on a single computer
but won't sync to a phone or another laptop.

Two paths forward:

**Path A: Direct GitHub commit from the dashboard**
- User generates a GitHub Personal Access Token (PAT) once
- Dashboard prompts for the PAT (stored in localStorage), then makes
  authenticated calls to the GitHub API to commit `data/crm_state.json`
  whenever the user changes a status / saves notes
- Pros: works from any device, no backend
- Cons: PAT management; users have to be admins of the repo

**Path B: Tiny backend (e.g. Cloudflare Worker + KV)**
- Deploy a small worker that exposes GET/PUT for `crm_state.json`
- Dashboard calls the worker; worker writes to a KV store
- A nightly workflow syncs the KV state into the repo for backup
- Pros: clean UX, no PAT prompts, syncs across all users immediately
- Cons: requires a Cloudflare account, slight ongoing cost

Recommend **Path B** for production use.

### 2. Build out the full CRM
**Priority: medium**

- **Apply CRM features to all lead types**, not just CCLN
- **Scheduled follow-ups / callbacks** with reminders
- **Call-log / activity timeline** in the side drawer
- **Pipeline value calculation** (estimated deal value per lead)
- **Email / SMS integration** (Twilio + SendGrid/SES)
- **Kanban view** — alternative to the table
- **Multi-user / sharing** — requires GitHub-backed or worker storage

### 3. Eventually clean up legacy categories
The user noted they'll review the kept-but-unused legacy categories
(Tax Deed, Federal/IRS Tax Lien, Medicaid Lien, Probate, Notice of
Commencement, Release of Lis Pendens) and decide which to drop.

### 4. Show grantee column in dashboard
When the grantor/grantee swap doesn't fire, the user can't see who's
on the other side. Adding a Grantee column to the leads table would
make uncaught swaps recoverable manually.

### 5. Tune PDF parser regex based on real PDFs
The borrower/lender regex patterns were written from typical Texas
foreclosure-notice templates but haven't been validated against real
Nueces County PDFs yet. After the first few real PDFs are processed,
review the extracted fields and tune the patterns for any consistent
misses (e.g. unusual borrower-name phrasings, atypical lender labels).

### 6. Foreclosure recurrence tracking (deferred per user decision)
Some properties show up on the foreclosure list multiple times because
the foreclosure gets cancelled and then re-filed months later (e.g.
March 2026 → cancelled → April 2026 → cancelled → May 2026). A
recurrence-count column would help identify these chronic situations,
which often signal serial loan-modification rather than a genuinely
motivated seller.

User explicitly deferred this for v1. If revisiting:
- Persistent `data/foreclosure_history.json` accumulates every record
  we've ever seen
- Fingerprint = normalized borrower name + subdivision + lot + block
- New column `recurrence_count` on the foreclosure record
- Dashboard column on the Foreclosures tab with tooltip showing prior dates

## In Progress

### Foreclosure PDF reader v1 — manual upload model (built, awaiting validation)

User manually downloads PDFs from the clerk portal and uploads them to
`pdfs/foreclosures/` via GitHub's web UI. A manual-trigger workflow
parses each PDF (extracting borrower, loan amount, deed date, address,
lender) and updates `foreclosures.json`. Successfully-processed PDFs
are deleted from the folder; unprocessed ones stay for manual review.

For records that got a borrower + legal description from the PDF but
no street address, the workflow runs NCAD reverse-lookup-by-name and
matches the legal description to recover the property's situs address.

**Pending validation:**
1. Upload 2-3 real foreclosure PDFs to `pdfs/foreclosures/`
2. Run the "Parse uploaded foreclosure PDFs" workflow
3. Verify that records get enriched with borrower/loan/address
4. Tune regex patterns if any fields aren't extracting well

## Done

- Initial pipeline (clerk portal scraping, NCAD bulk attempt, scoring)
- 30-day lookback window
- Legal-description address extraction for clerk records
- NCAD esearch (per-name property lookup) integration with token refresh
- Owner-swap logic for IRS, banks, courts, debt collectors
- Dashboard with leads table, search, filters, sort
- Mortgage Foreclosure separate stream + tab
- Per-portal-filter category fetches (`_docTypes=L3` etc.)
- City of Corpus Christi liens — separate persistent file + CRM tab
- 24-month one-shot backfill script for CCLN
- Basic CRM v1: status, notes, last-contact, dead-lead filter, CSV export
- Page size bumped to 250 (portal max) — 5x faster pagination
- Foreclosure PDF reader v1 framework (manual upload + auto-parse) — see above

## Archived (kept for possible future revisit)

### Headless-Chrome foreclosure PDF automation
**Archived 2026-05-11 in `archive/failed-headless-automation-2026-05-11.zip`.**

Attempted to automate the clerk portal cart-flow PDF download via
Playwright in headless mode inside GitHub Actions. After 8 iterations
the conclusion was that the portal (BIS Consultants / Neumo) detects
headless Chrome and silently refuses to fire its data-loading XHR.
Login succeeded; navigation to search-results pages succeeded; but the
SPA workspace state stayed `isLoading: true` indefinitely with zero
XHRs firing, so we could never extract document IDs or click through
to detail pages.

The README inside the archive zip explains:
- Which parts of the code are reusable (the PDF text parsing and the
  NCAD legal-match cross-reference are solid — and have been moved
  into the active `scraper/pdf_text_extractor.py` and
  `scraper/extract_uploaded_pdfs.py`)
- Three different strategies to try (non-headless local Playwright,
  paid stealth services like ScrapingBee/Browserless, or a browser
  extension that runs in the user's real Chrome)
- That a public scraper exists for `bexar.tx.publicsearch.us` (same
  portal family) using non-headless Selenium, suggesting the
  non-headless approach is the most promising retry path

**Future revisit:** if manual uploads become tedious (e.g. 50+/day
when broadening to other counties), consider:
1. Non-headless local Playwright script (~60-75% success likelihood
   per prior research)
2. Paid stealth service (~$50-100/month, professionally maintained)
3. Browser extension (works inside user's real Chrome, no detection)

## Investigated and parked

- **Consideration / lien amount on CCLN tab via clerk portal**:
  investigated three paths — (a) JSON XHR payloads don't include it,
  (b) card-view DOM toggle selector unstable, (c) per-document detail
  page fetch (URLs use internal IDs not exposed in static HTML, SPA
  hydration makes scraping unreliable). CSV export of all results
  works but requires login. The CCLN AFFIDAVIT OF LIEN PDF itself
  contains the amount — if the foreclosure PDF reader proves out,
  extending it to CCLN PDFs would solve this too. Volume is much
  larger (~1900 CCLN records) so consider rate-limiting carefully.
