[TODO.md](https://github.com/user-attachments/files/28040501/TODO.md)
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

- **NCAD re-corroboration pass: SHIPPED. Underlying scrape-order bug
  still unsolved.**

  *Status: 38 wrong-parcel matches evicted from the live data on
  2026-05-20 via `scraper/recorroborate_ncad.py` + workflow
  "Re-corroborate NCAD matches". Process verified: dry-run first,
  audited mismatches against subdivision overlap (zero false-positive
  evictions), then apply.*

  The pass is a clean-up tool, not a fix for the root cause:

  1. **Root cause (still unsolved).** In the daily scrape, NCAD owner
     search runs BEFORE the PDF parser populates the clerk legal.
     The corroboration guard inside `fetch.py:_pick_best_esearch_row`
     short-circuits when `record_legal` is empty, so a wrong-but-
     plausible parcel can be attached purely by owner-name score. The
     45% wrong-match rate the first apply revealed (38 of 85
     eligible) is the size of the leak.

  2. **What's shipped (workaround).** A standalone re-corroboration
     pass that runs AFTER PDF parsing has populated legals. For each
     record with both an attached `ncad_prop_id` and a clean clerk
     legal, fetches the NCAD property page, parses its Legal
     Description, runs `legal_descriptions_match`. On active rejection
     evicts the NCAD-derived fields. Conservative by default — never
     evicts on ambiguity or fetch errors. Dry-run first, then apply.

  3. **Required matcher loosening (also shipped).** The original
     matcher rejected "LOTS 7,8" vs "LOT 7" as a mismatch — common
     when clerk lists both lots on a 2-lot parcel and NCAD lists only
     the lead lot or partial slices. Loosened to set-overlap. Three
     previously-false-positive evictions (docs 243, 264, 247) became
     correct matches. Validated against all 435 cross-pairs of real
     records — zero spurious matches introduced.

  4. **Rate-limit mitigation (also shipped).** First dry-run hit NCAD
     rate-limiting after ~12 rapid identical-URL requests (Plutus
     cluster: 8 records share one parcel). Added per-run URL cache
     + 1.0s inter-fetch delay + retry-with-backoff. Second run: zero
     errors. Cache saved 19 of 85 fetches in that run.

  5. **The real fix (NOT shipped).** Reorder `fetch.py` so PDF parsing
     populates `legal_by_name` BEFORE the NCAD esearch loop runs.
     That way the corroboration guard has a real legal to compare
     against on first attempt and rejects wrong parcels at scrape
     time rather than requiring a separate cleanup pass. Bigger
     refactor; not started.

  6. **Operational note.** Re-run the re-corroboration workflow
     after each "Re-parse text archive" run (PDF parsing may unlock
     legals on newly-uploaded foreclosures, surfacing wrong matches
     that previously couldn't be checked). Dry-run first every time.

- **TRACT-form legals: parsed + auto-deaded on dashboard, but NCAD
  address still not auto-matched (two separate issues — read both)**

  *Status: dashboard auto-dead SHIPPED. Underlying NCAD-match gap and a
  distinct wrong-parcel bug remain UNSOLVED.*

  Three things happened, and they are not the same thing — keep them
  separate when revisiting:

  1. **Parser fix (done, shipped).** `pdf_text_extractor.py` previously
     had only LOT/BLOCK legal regexes, so TRACT-form legals (e.g. doc
     2026000292 "TRACTS ONE (1) AND TWO (2), SHARPSBURG ADDITION")
     parsed to a BLANK legal. Added `_RE_LEGAL_TRACT` (third fallback)
     + matching assembly in `extract_uploaded_pdfs.py`. Regression-
     tested over all 103 text-archive samples: zero owner/lender/
     amount/address regressions. After the "Re-parse text archive"
     workflow run, 2026000292 and 2026000266 now carry their legals.

  2. **Dashboard auto-dead (done, shipped).** Per user decision, the
     dashboard (`dashboard/index.html`) now auto-routes any record
     whose EFFECTIVE legal contains the whole word TRACT/TRACTS to the
     dead-leads section, labeled "Tract (auto)". Soft default: an
     explicit Restore writes status:"active" which overrides the rule
     (restored tract leads stay active and do NOT bounce back).
     Currently catches 2026000292, 2026000291, 2026000266. NOTE: this
     also auto-kills 2026000292 = 4646 Sharpsburg, which the
     foreclosure PDF proves is a LEGITIMATE in-town Corpus Christi
     house. User accepted this tradeoff knowingly; the "Tract (auto)"
     label + one-click Restore is the mitigation.

  3. **Still unsolved — TRACT legals don't auto-match NCAD address.**
     The text-archive re-parse path deliberately SKIPS the NCAD
     cross-reference (no browser), so 2026000292 / 2026000266 still
     show their old/wrong addresses (3442 XANADU, 1941 YALE). Even on
     the full daily scrape path, `legal_descriptions_match` keys on
     LOT/BLOCK — tract legals have neither, so they will not
     auto-confirm an NCAD parcel even with a correct legal. Fixing
     this needs a tract-aware NCAD matcher (match on subdivision +
     tract id, or capture each candidate parcel's own legal to
     compare). Not started.

  4. **Distinct bug — 2026000266 wrong-parcel match.** Separate from
     the above: 2026000266's legal is the Robstown "GEORGE H PAUL
     SUBDIVISION… TRACT 38" but the scraper attached NCAD parcel
     249198 = "JONES JOHN BLK 3 LOT 21" = 1941 Yale, Corpus Christi —
     a different property. Owner-name collision (owner has multiple
     parcels) defeated the corroboration guard in fetch.py
     `_pick_best_esearch_row` / `_legal_match`, which is skippable
     when clerk legal is empty and too loose when it isn't. This is a
     corroboration-guard defect, NOT the parser or the auto-dead rule.
     Auto-dead now hides this record anyway, so it's lower urgency,
     but the underlying wrong-match logic is still there for any
     non-tract record.

- **Consideration / lien amount on CCLN tab via clerk portal**:
  investigated three paths — (a) JSON XHR payloads don't include it,
  (b) card-view DOM toggle selector unstable, (c) per-document detail
  page fetch (URLs use internal IDs not exposed in static HTML, SPA
  hydration makes scraping unreliable). CSV export of all results
  works but requires login. The CCLN AFFIDAVIT OF LIEN PDF itself
  contains the amount — if the foreclosure PDF reader proves out,
  extending it to CCLN PDFs would solve this too. Volume is much
  larger (~1900 CCLN records) so consider rate-limiting carefully.
