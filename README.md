[README.md](https://github.com/user-attachments/files/27500401/README.md)
# Nueces County Motivated Seller Lead Scraper

Daily-refreshed motivated-seller lead pipeline for **Nueces County, Texas**
(Corpus Christi). Pulls indicator filings from the County Clerk
([nueces.tx.publicsearch.us](https://nueces.tx.publicsearch.us/)) and enriches
them with mailing/site address data from the Nueces Central Appraisal
District ([nuecescad.net](https://nuecescad.net/downloads-reports/)).

## What it captures

| Code      | Document family                                                      |
|-----------|----------------------------------------------------------------------|
| `LP`      | Lis Pendens                                                          |
| `NOFC`    | Notice of Foreclosure / Substitute Trustee Sale                      |
| `TAXDEED` | Tax Deed                                                             |
| `JUD`     | Judgment / Abstract of Judgment / Certified Judgment / Domestic Jud. |
| `LNFED`   | Federal / IRS / Corporate Tax Lien                                   |
| `LN`      | General Lien / Mechanic Lien / HOA Lien                              |
| `MEDLN`   | Medicaid Lien                                                        |
| `PRO`     | Probate / Letters Testamentary / Affidavit of Heirship               |
| `NOC`     | Notice of Commencement                                               |
| `RELLP`   | Release of Lis Pendens                                               |

## Outputs

| File                          | Purpose                                       |
|-------------------------------|-----------------------------------------------|
| `dashboard/records.json`      | Read by the static dashboard site             |
| `data/records.json`           | Same content, kept in `data/` for archiving   |
| `data/leads_for_ghl.csv`      | Drop-in import for GoHighLevel                |

The dashboard at `dashboard/index.html` is auto-deployed to GitHub Pages.

## Seller score (0–100)

```
base                       30
+ 10 per flag matched
+ 20 if owner has BOTH Lis Pendens AND Foreclosure
+ 15 if amount > $100,000
+ 10 if amount >  $50,000
+  5 if filed within 7 days
+  5 if a property/site address was matched
```

Flags: *Lis pendens, Pre-foreclosure, Judgment lien, Tax lien, Mechanic
lien, Probate / estate, Medicaid lien, LLC / corp owner, New this week*.

## Local run

```bash
pip install -r scraper/requirements.txt
python -m playwright install --with-deps chromium
python scraper/fetch.py
```

## Schedule

Runs daily at **07:00 UTC** via GitHub Actions
(`.github/workflows/scrape.yml`). Manual runs: *Actions → Scrape Nueces
leads → Run workflow*.

## Important notes

* The NCAD bulk export is ~150 MB and ships **once per appraisal cycle**
  (preliminary in spring, certified in late summer). The latest available
  ZIP is auto-discovered each run, so you don't need to update URLs.
* The NCAD host sits behind a WAF that blocks plain `requests` calls. The
  scraper transparently falls back to a real headless Chromium fetch
  whenever it gets a 4xx, so this works on GitHub Actions runners.
* The Clerk portal is a JavaScript SPA. We drive it with Playwright and
  intercept its JSON responses on the wire — far more resilient than
  scraping rendered HTML.
* Every record is dedup'd by `doc_num`. Multiple search queries per
  category increase recall without inflating result count.
