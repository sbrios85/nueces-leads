[NCAD_EXPORT_RUNBOOK.md](https://github.com/user-attachments/files/28902928/NCAD_EXPORT_RUNBOOK.md)
# NCAD Appraisal Export → Dashboard Runbook

How the NCAD appraisal-roll export is used to populate the **Tax Deferral tab**,
the **Stack "Tax Deferral" source**, and the **exemption tags** on every lead —
and exactly how to refresh it next year. Built from the **2026 Preliminary**
export; this doc is the memory for re-running it annually.

---

## TL;DR — the annual refresh (6 steps)

1. **Twice a year** — in **April** (Preliminary) and again **late July onward**
   (Certified) — check <https://nuecescad.net/downloads-reports/> for the new
   roll export (a ~150–170 MB ZIP). Grabbing both keeps the gap short.
2. **Download + extract** the ZIP into a folder (right-click → Extract All — see
   the extractor's notes if "Extract All" is hidden).
3. **Check the layout version.** The same page lists *"Formatting Guides for
   Appraisal Roll Flat Files"* — currently `Export-Layout-Files-8.0.33.zip`.
   If the version changed (e.g. 8.0.34), the field byte-positions may have moved —
   send the new layout `.xlsx` to Claude to re-verify before trusting the output.
4. **Run the extractor** on your PC:
   `python extract_ncad_exemptions.py --export-dir "C:\path\to\unzipped\export"`
   It prints a SUMMARY block — keep it.
5. **Build the dashboard file.** Send the new `ncad_exemptions.csv.gz` (+ the SUMMARY)
   to Claude; it joins to the NCAD reference for addresses and produces
   `ncad_exemptions.json`.
6. **Deploy.** Upload `ncad_exemptions.json` to `dashboard/`, run **Backfill
   deed_date → ISO**, hard-refresh (Ctrl+Shift+R). On the **Today** tab, tap the
   **"NCAD appraisal export (annual)"** card to mark it done.

---

## Source & cadence

- **Page:** <https://nuecescad.net/downloads-reports/>
- **Preliminary** roll export — posts ~**early April** (the 2026 build used this:
  uploaded 04/02/2026, dataset dated 2026-04-01).
- **Certified** roll export — posts ~**late July–September**, after the chief
  appraiser certifies values (Texas statutory deadline July 25). 2025 posted
  Sep 10; 2024 in July. **Prefer the Certified roll** — it's the final, settled
  snapshot of values, exemptions, and deferrals after protests.
- Refresh **twice a year** (April Preliminary + summer Certified) — the gap
  between them is ~5 months and deferrals/exemptions shift in that window. The
  Today-tab reminder auto-flags ~6 months after each refresh.

---

## What's in the export (and what we actually use)

The export is a **PACS fixed-width, multi-file ZIP** (~19 `.TXT` files named like
`2026-04-01_001218_APPRAISAL_INFO.TXT`). We use exactly **two** of them, plus the
layout guide:

| File | Name | Size | What we read |
|------|------|------|--------------|
| **#2** | `APPRAISAL_INFO.TXT` | ~2 GB | One row per property/owner. We pull `prop_id`, `geo_id`, and the exemption `T`/`F` flags. Streamed line-by-line (never loaded into RAM). |
| **#19** | `APPRAISAL_TAX_DEFERRAL_INFO.TXT` | ~1 MB | **The authoritative tax-deferral list.** We pull `prop_id`, `geo_id`, `exmpt_type_cd`, `deferral_start`, `owner_name`. This is what catches deferrals the appraisal-roll "(TD)" owner-name tag misses (e.g. 6333 Fitzhugh). |
| layout | `Appraisal Export Layout - 8.0.33.xlsx` (inside `Export-Layout-Files-8.0.33.zip`) | small | Defines every field's byte position. **Version 8.0.33 for 2026.** |

### Field positions (1-indexed, inclusive) — layout 8.0.33

**File #2 (APPRAISAL_INFO.TXT):**
- `prop_id` 1–12 · `geo_id` 547–596
- Exemption flags (single `T`/`F` char): HS @2609 · OV65 @2610 · OV65S @2661 ·
  DP @2662 · DV1 @2663 · DV1S @2664 · DV2 @2665 · DV2S @2666 · DV3 @2667 ·
  DV3S @2668 · DV4 @2669 · DV4S @2670 · EX @2671 · DPS @5435 · DVHS @5463 · DVHSS @7239

**File #19 (APPRAISAL_TAX_DEFERRAL_INFO.TXT):**
- `prop_id` 1–12 · `owner_id` 13–24 · `exmpt_type_cd` 25–29 ·
  `deferral_start` 30–54 · `deferral_end` 55–79 · `geo_id` 80–129 ·
  `owner_name` 130–199

> ⚠️ **These positions are tied to layout 8.0.33.** If next year's guide is a
> different version, re-verify before trusting the output. The extractor
> self-checks the first row and aborts if `prop_id` isn't numeric, but a subtle
> column shift could still skew the exemption flags — so always eyeball the
> SUMMARY counts against the baseline below.

---

## The extractor

`scraper/extract_ncad_exemptions.py` — stdlib only (no pip installs), runs on the PC.

```
python extract_ncad_exemptions.py --export-dir "C:\path\to\unzipped\export"
```

Outputs `ncad_exemptions.csv` (+ `.csv.gz`) and prints a SUMMARY. Columns:
`geo_id, prop_id, exemptions, tax_deferral, deferral_types, deferral_start, deferral_owner`.

---

## From CSV → the dashboard file

The dashboard loads `dashboard/ncad_exemptions.json`, which is the extractor CSV
**joined to the NCAD reference** (`ncad_reference.csv.gz`, for situs address /
owner / market value / legal) and split into two payloads:

- `deferrals: [...]` — every parcel with an active deferral, enriched with address.
  → powers the **Tax Deferral tab** + the **Stack source**.
- `exemptions: { geo_id: "HS;OV65;..." }` — → powers the **exemption tags**.

**Join key:** `geo_id`, dashed form (e.g. `9896-0005-0090`), which equals the
`ncad_account_num` carried on dashboard leads. In 2026, **100% of the 3,271
deferrals matched** the reference.

> The NCAD reference is itself derived from this same export's situs fields, so a
> fresh reference can be rebuilt from next year's export if desired
> (`build_ncad_reference.ps1`). For the exemption/deferral refresh the reference
> is only used to attach addresses — re-running with a current reference is a
> nice-to-have, not required.

---

## Where the data shows up

- **Tax Deferral tab** (Lead Types) — all deferrals, sortable, with status pills;
  columns: deferral type, deferred-since, market value, exemptions, NCAD/NCTAX.
- **Stack** — "Tax Deferral" source (rose pill), auto-included. Overlaps with a
  code violation / vacancy / city lien / delinquency are the **hot** leads.
- **Exemption tags** under each lead's owner across DELQ / Vacant / CCLN / CV / Stack:
  `HS` = owner-occupied · `OV65/DP/DV/DVHS` = elderly/disabled (deferral-eligible) ·
  `EXEMPT` = total-exempt parcel (usually a non-lead to skip).
- **Today tab** — "NCAD appraisal export (annual)" reminder card; auto-flags
  ~11 months after the last refresh.

---

## Sanity numbers — 2026 baseline (compare next year)

- Property rows scanned: **219,800**
- Parcels with ≥1 exemption: **75,239**
  (HS 73,784 · OV65 31,720 · DVHS 3,933 · DV4 3,438 · DP 2,876 · EX 234)
- Deferral-eligible (OV65/DP/DV/DVHS family): **39,387**
- **Actual tax deferrals (File #19): 3,271**

If next year's numbers are wildly off (deferrals = 0, exemptions = 200k+, etc.),
the layout positions likely shifted — **stop and re-verify the field offsets.**

---

## Gotchas

- **Layout version** (8.0.33) — re-check every year; it's the #1 break risk.
- **Certified, not Preliminary** — use the certified roll for the final annual snapshot.
- **No dollar balance** in the export for deferrals — the Tax Deferral tab's NCTAX
  link pulls the live deferred balance per account (a full NCTAX balance checker is
  a separate TODO).
- **Clean-sweep refresh** — the new `ncad_exemptions.json` fully replaces the old
  one. No merge, no manual deletion.
- **2025 exemption-amount change** — Texas raised the homestead exemption mid-2025
  (the page shows "before/after HS/OV65/DP exemption change" totals). We read
  *flags*, not *amounts*, so this didn't affect us — but be aware exemption
  amounts can change year to year.

---

*Last refresh: 2026-06-12 (from the 2026 Preliminary export, layout 8.0.33).*
