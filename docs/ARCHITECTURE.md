[ARCHITECTURE.md](https://github.com/user-attachments/files/28902934/ARCHITECTURE.md)
# Lead Pipeline — Architecture & Universal Rules

The portable "engine" behind the Nueces County dashboard: the rules and patterns
that are **not** specific to one county's websites. Read this first when porting
to a new county — it's the written version of "build it keeping all the same
rules." County-specific data access (the scrapers, field mappings) is documented
separately per source; this doc is the part that travels.

> Companion docs: `NCAD_EXPORT_RUNBOOK.md` (the appraisal-export refresh).
> The code itself is the live source of truth — `index.html` and the scrapers
> carry detailed inline comments. This doc captures the *stable* rules so they
> don't have to be re-derived.

---

## 0. What this is

A single-page dashboard that surfaces **distressed-property leads** in a county by
stacking public-record signals — mortgage foreclosures, tax foreclosures, city
liens, delinquent taxes, code violations, vacancies, tax deferrals — and flagging
the properties where **multiple signals overlap** (the Stack). The user works
these as a motivated-seller CRM and door-knocks. Design values, applied
everywhere: dense functional UI, real data over mockups, honest capability
limits, incremental validated builds, individual owners only (companies filtered
out).

---

## 1. System architecture

- **Front end:** one `index.html` served on **GitHub Pages**. No framework, no
  build step. All logic is one inline `<script>`.
- **Data:** each lead type is a static JSON file fetched client-side at load
  (`code_violations.json`, `city_liens.json`, `delq_records.json`,
  `foreclosures.json`, `tfc.json`, `ncad_exemptions.json`, …). First-deploy safe:
  a missing file shows an empty-state, never an error.
- **Producers:** Python **scrapers** + **GitHub Actions** workflows fetch/clean
  source data and commit the JSON. Some data (Vacant Houses, Stack) is **derived
  client-side** from already-loaded JSON — no file of its own.
- **Rendering:** panes are **lazy-rendered** — a tab's table rows are built the
  first time it's opened. Sidebar counts + stats compute eagerly (cheap).
- **CRM state** lives in the **browser's localStorage**, keyed per lead type
  (e.g. `nueces_cv_status_v1`, `nueces_deferral_status_v1`). It is **not** in the
  repo — it's per-device and survives data refreshes because it's keyed by stable
  IDs (account/geo_id/case number).

---

## 2. Lead types & where each comes from

| Tab | Source (Nueces) | Vendor pattern (portable?) |
|-----|-----------------|----------------------------|
| Mortgage Foreclosures (FC) | County Clerk records | Per-county clerk site — custom each time |
| Tax Foreclosures (TFC) | **LGBS** tax-sale API | LGBS/Linebarger serves many TX counties — often portable |
| City Liens (CCLN) | City lien PDFs | City-specific; may not exist elsewhere |
| Delinquent Taxes (DELQ) | Tax office XLS | Format varies; re-map columns |
| Code Violation (CV) | City PIA request | City-specific; often manual |
| Vacant Houses | derived from CV (Vacant Building types) | Derived — free wherever CV exists |
| Tax Deferral | **NCAD/PACS** appraisal export File #19 | BIS/PACS CADs — often portable |
| Stack | derived from all of the above | Derived — always free |

The Stack and exemption layers are **derived**, so they come along automatically
once their inputs exist.

---

## 3. Universal lead-quality rules

These are the filters that define "an actionable lead." Constants are
configurable per county, but the *logic* is fixed.

**Residential only.** Keep Texas PTAD state codes: **`A1`** (single-family),
**`B1`–`B9`** (multi-family duplex→apartment), **`C1`** (vacant residential lot).
Everything else (commercial, industrial, ag, mineral) is dropped. (`KEEP_CODES`
in `import_delq_xls.py`.)

**Value cap.** `MAX_MARKET_VALUE = 500,000` — drops high-end properties unlikely
to be wholesale/flip targets. Tune per market.

**Geography allowlist.** A zip-code allowlist (`CC_ZIPS`) scopes to the target
area. Re-build per county.

**Owner classifier — exclude companies (`ccln_owner_filter.py`).** The single
most reusable, county-agnostic asset. `classify_owner(name) → (kind, keep)`:

- **KEEP:** `individual` (a person), `estate` (`ESTATE OF…`, `…DECD` — heirs may
  be motivated), `family_trust` (`… LIVING TRUST`, `… FAMILY TRUST`, plus the
  CAD's abbreviated forms `LVG TRST`, `FAM TRST`, `REV LVG TRUST`, …).
- **EXCLUDE:** `company` (LLC/INC/CORP/LP/REALTY/INVESTMENTS/…), `trust_inst`
  (institutional land trusts), `religious`, `school` (ISD/college), `government`
  (`COUNTY OF X` / `X COUNTY`, city/state/federal), `nonprofit` (IRS-code
  forms), `hoa`.
- **False-positive guard:** if a name is exactly 2 tokens (ignoring JR/SR
  suffixes) and one is a surname that collides with a company keyword
  (`CARLA BANK`, `JOHN BAPTIST`), treat as individual.
- **Bias:** on genuine ambiguity at the boundary, bias **toward exclusion** —
  we accept losing a few real leads to filter out far more corporate noise.

Companies almost never produce motivated-seller deals (they sell through brokers),
so this filter runs at ingestion across every applicable source.

---

## 4. Name formatting

- **Display:** `fmtOwner` / `fmtTitle` normalize ALL-CAPS county data to readable
  Title Case for owners and addresses.
- **Search round-trip:** `flipOwnerForNcad` converts display order back to the
  CAD/tax-office **"LAST FIRST"** search format, and the NCAD/NCTAX cell helpers
  copy it to the clipboard + open the search page so the user pastes and hits
  Search. This pattern ports to any county whose search expects "Last First."

---

## 5. Cross-source matching — the Stack

The Stack groups records from every source by **property** and surfaces ones with
overlapping signals.

- **Match key:** primary = **`ncad_account_num`** (the dashed geo_id, e.g.
  `0386-0005-0060`) — same account = same property, definitively. Fallback =
  **normalized address** (uppercased, whitespace-collapsed, street-suffix
  canonicalized via a suffix map: STREET→ST, AVENUE→AVE, …, unit tokens
  stripped). No key on either → record can't be stacked (rare).
- **Adding a source = a 5-field object** in `STACK_SOURCES`: `type`, `label`,
  `badgeClass` (+ a CSS color), `autoInclude`, and accessors
  (`getRecords/isActive/primaryKey/addressKey/ncadKey/ownerKey/filedKey/openCrm`).
  The renderer iterates the registry — new sources slot in with zero other
  changes.
- **`autoInclude`:** `true` = strong enough to appear alone (foreclosure, lien,
  vacant, deferral); `false` = overlap-only, appears only when it stacks with
  another signal (e.g. non-vacant code violations).
- **One pin/row per property.** A property in N sources shows N pills and counts
  as N signal-types (no cross-source dedup of the *signal*, but one *row*).

---

## 6. CRM model

- **Stages (`MFC_STAGES`, shared by every lead type):** `new`→"New",
  `contacted`→**"Working"** (stored key stayed `contacted`; only the label
  changed), `negotiating`, `under_contract`→"Under Contract",
  `closed_won`→"Closed" (terminal), `dead`→"Dead" (terminal).
- **Status pill** on each row opens a stage menu; selection writes to that lead
  type's localStorage store.
- **Dead leads** collapse into a footer toggle ("N deleted leads — click to
  expand"), with a Restore button. Per-tab, persisted.

---

## 7. Exemptions & tax deferrals (Texas / PACS)

If the county's appraisal district runs **BIS Consultants / True Automation PACS**
(the `esearch.<cad>.net` pattern + the 8.0.x flat-file export), this whole layer
ports nearly directly:

- **File #2** `APPRAISAL_INFO.TXT` — fixed-width; single-char `T`/`F` exemption
  flags (HS/OV65/DP/DVHS/EX/…) at fixed byte positions, keyed by `geo_id`.
- **File #19** `APPRAISAL_TAX_DEFERRAL_INFO.TXT` — the authoritative §33.06
  deferral list (catches what the appraisal-roll "(TD)" name tag misses).
- Extracted by `extract_ncad_exemptions.py` (PC, stdlib), joined to the CAD
  reference on `geo_id` for address, → `ncad_exemptions.json`.
- Surfaces as: the **Tax Deferral tab**, a **Stack source**, and **exemption
  tags** under every lead's owner (`HS`=owner-occupied, OV65/DP/DV/DVHS=
  deferral-eligible, EXEMPT=skip). Full detail in `NCAD_EXPORT_RUNBOOK.md`.

---

## 8. Deploy & operations

- **Redeploy rule:** a plain commit (or an `index.html`-only upload) does **not**
  reliably rebuild Pages. After such uploads, run the **"Backfill deed_date → ISO"**
  workflow to force a rebuild, then hard-refresh (**Ctrl+Shift+R**). Workflows
  that publish Pages themselves (geocode, build_cv, ccln proptype enrich) do
  **not** need a Backfill after; data-only commit workflows (the delinquent-tax
  import) **do**.
- **Workflow permissions:** any workflow that commits back to the repo needs
  `permissions: contents: write` in its YAML.
- **Token:** enrichment workflows use a fine-grained PAT ("1st Token"); update the
  repo secret after any regeneration.
- **Refresh cadences (Today-tab reminders):** CV import ~30 days, delinquent-tax
  import ~30 days, **NCAD appraisal export ~180 days** (twice a year — April
  Preliminary + summer Certified).

---

## 9. Porting to a new county — the playbook

**Ports for free (the engine):** the dashboard shell, tab/pane/nav layout, the
Stack engine, the CRM, name formatting, the **owner classifier**, the deploy
machinery, the cadence reminders, and — if it's a Texas county — the residential
state codes and all property-tax concepts.

**Re-derived per county (the adapters):** each scraper (clerk, tax office,
appraisal, city), the field/column mappings, the zip allowlist, the value cap,
and confirmation of which signals the county even publishes.

**Vendor multipliers — check these first, they decide the effort:**
1. **In Texas?** → same tax code, PTAD codes, exemptions, §33.06 deferrals,
   July-25 certification. The whole exemption/deferral concept + runbook carry.
2. **Appraisal district on BIS/PACS** (`esearch.<cad>.net`)? → the exemption +
   deferral extractor ports nearly as-is.
3. **Tax sales via LGBS/Linebarger**? → the TFC scraper is largely the same API.

**Per-county availability caveat (honest):** not every signal exists everywhere.
A county may publish foreclosures + delinquent taxes but have no online
city-lien or code-violation feed. The dashboard degrades gracefully (empty-state
tabs), but a signal a county doesn't expose can't be conjured.

**Build sequence:** (1) inventory the three+ sites, confirm what's reachable;
(2) stand up the shell with the engine; (3) build scrapers one source at a time,
each validated against real data before the next; (4) wire exemptions if PACS;
(5) the Stack lights up automatically as sources land.

---

*Maintained alongside the code. When a rule here changes in `index.html` or a
scraper, update this doc so it doesn't drift.*
