[TODO.md](https://github.com/user-attachments/files/27544795/TODO.md)
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

### 3. Better grantor/grantee swap detection (DONE — 2026-05-08)
Catch debt collectors, credit-card issuers, tax-lien funds, hospital
billing entities. See `INSTITUTIONAL_PLAINTIFF_RE` in `scraper/fetch.py`.

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
- Persistent JSON cache for esearch results
- Owner-swap logic for IRS, banks, courts, debt collectors, etc.
- Dashboard with leads table, search, filters, sort
- Mortgage Foreclosure separate stream + tab
- Per-portal-filter category fetches (`_docTypes=L3` etc.)
