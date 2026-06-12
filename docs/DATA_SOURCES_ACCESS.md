# Data Sources & Access

The hard-won **access** knowledge for each data feed — endpoints, the settings we
found by trial, the gotchas, and the **dead-ends so we don't re-try them**. This
is deliberately *not* a re-statement of the scraper logic (that lives, commented,
in the `.py` files). It's the environmental stuff that exists nowhere else but our
heads and old transcripts.

> For each source: **where**, **how to reach it**, **settings/gotchas**, and a
> pointer to the code that holds the parsing logic.

---

## ⚠️ NCAD esearch — the appraisal district lookup *(the painful one)*

**Where:** `https://esearch.nuecescad.net/`
Property page: `/Property/View/{prop_id}?year=YYYY&ownerId={owner_id}`

**How to reach it:** token-gated JSON search. **You must mint a token first** —
load the homepage and read it from `<meta name="search-token">`. Every search
needs a current token; there is no anonymous query.

**Proven throttle settings (do not deviate without re-testing):**
- **~12 consecutive lookups** before a soft-throttle kicks in — it stops erroring
  and instead just returns **empty results**, which is easy to mistake for "no
  match." Watch for a *run* of empties.
- **`INTER_FETCH_DELAY_S = 1.5`** seconds between lookups.
- **Refresh the token every ~25 lookups**, and **reactively after 5 consecutive
  misses** (a miss-streak usually means the token went stale, not that the
  properties don't exist).
- **Retry-on-empty:** wait ~3s and try once more before declaring no-match.
- Matching helpers: multi-word `StreetName` fallback; strip street-type suffixes
  before querying.

**Biggest gotcha — IP blocking:** esearch returns **403 from GitHub Actions IPs.**
The NCAD enrichment **cannot run in a workflow** — it runs from the PC (or any
non-Actions IP). (`raw.githubusercontent.com` works from anywhere, so repo reads
are fine in Actions; it's only the live esearch calls that are blocked.)

**Confirmed dead-ends — do NOT waste time re-attempting:**
- Bulk parcel **ZIP** download via esearch — blocked/infeasible.
- Pulling `APPRAISAL_INFO.TXT` *through esearch* — infeasible.
  *(Note: that file IS available as part of the downloads-page export ZIP — see
  the runbook. That's a different, working path.)*

**Known edge case:** a few parcels exist in NCAD with **every field blank**
(e.g. prop_id `219511`). That's "no NCAD match possible — NCAD data incomplete,"
**not** a matcher bug.

**Logic lives in:** the FC/TFC NCAD enricher scripts
(`enrich_fc_ncad_search.py`, `enrich_tfc_ncad.py`).

---

## NCTAX — Nueces County Tax Office (acttax)

**Where:** `https://actweb.acttax.com/nueces/nueces/`
Account page: `account-details.jsp?can={CAN}`

**How:** the `CAN` is the **NCAD account number with all non-digits stripped**
(dashed `0386-0005-0060` → `038600050060`). Authoritative for **live tax balances
and deferral status** — the appraisal export has no dollar balance, so this is the
source for "how much is actually owed."

**Planned:** a per-account NCTAX checker (prior-year-due + deferral) — would run
from the PC (same IP caution as esearch likely applies; untested in Actions).

---

## LGBS — Tax Foreclosures (TFC)

**Where:** `https://taxsales.lgbs.com/api/property_sales/` — a clean JSON API,
**found via browser DevTools** (network tab) on the LGBS tax-sale site.

**Portable:** LGBS / Linebarger runs tax sales for **many Texas counties**, so
this same API pattern often works for a new county (different filter params).

**Logic lives in:** `scrape_tfc.py`.

---

## County Clerk — Mortgage Foreclosures (MFC)

**Where:** Nueces County Clerk records (notices of substitute trustee sale).

**How:** reverse-engineered from the clerk's records portal. Per-county and the
least portable feed — every clerk site differs. Watch for OCR garble on scanned
filings (handled in the parser).

**Logic lives in:** the MFC scraper.

---

## Delinquent Tax XLS (DELQ)

**Where:** Nueces County Tax Office delinquent-tax export (`.xls`), **downloaded
by hand**.

**How:** drop the file in `data/delq_uploads/`, run the **"Import Delinquent Tax
XLS"** workflow (commits `delq_records.json` only → needs a Backfill after to
redeploy Pages).

**Column map (0-indexed) — the part that's painful to re-derive:**

| Col | Field | Notes |
|----:|-------|-------|
| 0 | ACCOUNT # | tax-office ID (primary key) |
| 1 | APPR DIST # | **NCAD account** — links to other systems |
| 3 | STATE PROP | residential filter (A1/B1-B9/C1) |
| 8 | PROP ADDR | situs address |
| 9 | ZIP | situs zip |
| 11 | OWNER | owner name |
| 12–14 | ADDR2/3/4 | mailing address lines |
| 15–17 | CITY / STATE / ZIP2 | mailing city/state/zip |
| 25 | SUIT/JUDGEMENT FLAG | **`L` = suit filed, `J` = judgment** |
| 26 | BANKR FLAG | bankruptcy |
| 28 | BAD ADDR FLAG | |
| 29 | TAX DEF CODE | tax deferral on file |
| 30 | PAY AGREE CODE | payment plan |
| 31–36 | EX 1…EX 6 | exemption codes |

**Filters:** `KEEP_CODES` = A1 / B1–B9 / C1 · `MAX_MARKET_VALUE` = 500,000 ·
`CC_ZIPS` allowlist = 78401–78419 **minus 78410 (Calallen)** · corporate-owner
filter.

**Gotcha (a real bug we hit):** bankruptcy rows must be **KEPT** (and flagged),
not dropped — an early version silently dropped every `BANKR='Y'` row and hid
~$103K of owed taxes. Flags emitted: `in_suit`, `has_judgment`, `tax_deferral`,
`payment_agreement`, `bankruptcy`, `bad_address`.

**Logic lives in:** `import_delq_xls.py`.

---

## City Liens (CCLN)

**Where:** City of Corpus Christi lien filings (**PDFs**).

**How:** PDF extractor → corporate-owner filter → NCAD enrichment. **Scope:
code-enforcement liens only** (substandard / carrion / filth / demolition);
auto-skips real-estate-lien-note and correcting-document filings.

**Logic lives in:** `extract_ccln_pdfs.py`, `ccln_owner_filter.py`.

---

## Code Violations (CV)

**Where:** City of Corpus Christi — obtained via a **Public Information Act (PIA)
request** (ref **CCPIA 26-1224**). Manual; not a live feed.

**How:** receive the XLS → `build_cv.py` cleans / matches / filters / enriches →
`code_violations.json`. Vacant Houses tab + the Vacant Stack source are **derived
client-side** from this (Vacant Building / Unsecured Vacant Building types).

**Logic lives in:** `build_cv.py`.

---

## NCAD Appraisal Export (exemptions + tax deferrals)

**Where:** `https://nuecescad.net/downloads-reports/` — Preliminary (~April) +
Certified (~summer). Manual download. **Full procedure in `NCAD_EXPORT_RUNBOOK.md`.**

**NCAD reference** (`ncad_reference.csv.gz`, geo_id → situs/owner/value) is built
from this same export's situs fields (`build_ncad_reference.ps1`) and is what the
exemption/deferral data joins to for addresses.

---

## Sandbox / GitHub network facts

- GitHub **API** is rate-limited unauthenticated from the build sandbox — prefer
  `raw.githubusercontent.com` for repo reads (works reliably).
- **NCAD esearch / NCTAX are blocked (403) from Actions IPs** — any live lookup
  against them runs from the PC, never in a workflow.
- Workflows that commit back to the repo need `permissions: contents: write`.

---

*Lean by design — when a scraper's logic changes, update the `.py`, not this doc.
Update this doc only when an **endpoint, access method, or limit** changes.*
