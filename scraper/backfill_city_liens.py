"""
One-shot backfill: City of Corpus Christi liens, last 24 months.
================================================================

Pulls every City-of-Corpus-Christi-grantor lien recorded in the past
24 months and writes them to `data/city_liens.json` (and mirrors a copy
to `dashboard/city_liens.json` for the CRM tab).

Run via the dedicated GitHub Actions workflow `.github/workflows/backfill.yml`
exactly **once** to seed the cumulative file. After that, the daily
`scraper/fetch.py` keeps it growing.

Implementation notes:
  * Uses the same Playwright + table-extraction stack as `fetch.py`.
  * Iterates pagination (`offset=0`, `offset=250`, ...) until a page
    returns fewer than `limit` rows or returns no rows at all.
  * Page size is `limit=250` (portal max — confirmed by the user).
  * Hard wall-clock cap of 50 minutes; aborts gracefully if exceeded.
  * Idempotent: if `city_liens.json` already has data, this script
    merges (deduped by doc_num) — running it twice doesn't double up.

Expected runtime: 5-15 minutes for 24 months of CCLN data, depending
on how many records exist (typically a few hundred per year).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
import traceback
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import urlencode

# Allow importing fetch.py from the same scraper directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fetch import (  # noqa: E402  — local import after sys.path tweak
    CLERK_BASE,
    USER_AGENT,
    DASHBOARD_DIR,
    DATA_DIR,
    PORTAL_FILTERED_CATEGORIES,
    CAT_TO_LABEL,
    ClerkRecord,
    _build_clerk_search_url,
    _extract_clerk_table_rows,
    _extract_clerk_card_rows,
    _normalize_clerk_row,
    _extract_rows_from_html,
    enrich_with_parcels,
    enrich_via_ncad_search,
    build_owner_cat_index,
    compute_flags_and_score,
    load_city_liens,
    merge_city_liens,
    save_city_liens,
)

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("nueces-backfill-ccln")

LOOKBACK_MONTHS = 24
PAGE_SIZE = 250
MAX_PAGES = 100               # safety: stop after 25,000 records
PHASE_BUDGET_SECONDS = 50 * 60  # 50 minutes


def _build_paginated_url(start_iso: str, end_iso: str, offset: int) -> str:
    """Same shape as fetch._build_clerk_search_url, but with explicit offset."""
    base = _build_clerk_search_url(
        start_iso, end_iso,
        query="city of corpus christi",
        doc_types="L3",
    )
    # Replace `offset=0` with the actual offset.
    return base.replace("offset=0", f"offset={offset}")


async def _scrape_all_pages(start_iso: str, end_iso: str
                              ) -> List[ClerkRecord]:
    """Iterate through every page of CCLN results in the given window."""
    try:
        from playwright.async_api import async_playwright  # type: ignore
    except ImportError:
        log.error("playwright not installed; cannot run backfill")
        return []

    deadline = time.time() + PHASE_BUDGET_SECONDS
    seen: Dict[str, ClerkRecord] = {}

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
        context = await browser.new_context(user_agent=USER_AGENT)
        page = await context.new_page()

        for page_num in range(MAX_PAGES):
            if time.time() > deadline:
                log.warning("backfill time budget exhausted at page %d",
                            page_num)
                break
            offset = page_num * PAGE_SIZE
            url = _build_paginated_url(start_iso, end_iso, offset)
            log.info("page %d: offset=%d  url=%s",
                     page_num + 1, offset, url[:140])
            try:
                await page.goto(url, wait_until="domcontentloaded",
                                 timeout=30_000)
                try:
                    await page.wait_for_function(
                        """() => {
                            const rows = document.querySelectorAll(
                                'table tbody tr');
                            for (const r of rows) {
                                const docCell = r.querySelector('.col-7');
                                if (docCell && docCell.textContent.trim())
                                    return true;
                            }
                            const txt = document.body.innerText || '';
                            return txt.includes('No Results Found') ||
                                   txt.includes('returned no results');
                        }""",
                        timeout=20_000,
                    )
                except Exception:
                    pass
                await page.wait_for_timeout(600)
                html = await page.content()
            except Exception as exc:
                log.error("nav failed for page %d: %s", page_num + 1, exc)
                break

            rows = _extract_clerk_table_rows(html)
            if not rows:
                # Try the redux fallback.
                try:
                    rows = await page.evaluate("""() => {
                        try {
                            const d = (window.__data || {}).documents;
                            if (!d || !d.workspaces) return [];
                            const ws = Object.values(d.workspaces)[0];
                            if (!ws || !ws.data) return [];
                            return Object.values(ws.data.byHash || {});
                        } catch (e) { return []; }
                    }""")
                except Exception:
                    rows = []
            if not rows and html:
                rows = _extract_rows_from_html(html)

            log.info("  → %d raw rows", len(rows))

            # Click into card view to harvest Consideration + Instrument
            # Date (not exposed in list view). Failure is non-fatal.
            if rows:
                try:
                    await page.evaluate("""() => {
                        const btns = Array.from(document.querySelectorAll(
                            'button[aria-label], button[title], [role=button]'));
                        for (const b of btns) {
                            const lbl = ((b.getAttribute('aria-label') || '') +
                                         ' ' + (b.getAttribute('title') || '')
                                         ).toLowerCase();
                            if (lbl.includes('card') ||
                                lbl.includes('grid')  ||
                                lbl.includes('detail')) {
                                b.click();
                                return true;
                            }
                        }
                        return false;
                    }""")
                    await page.wait_for_timeout(1500)
                    card_html = await page.content()
                    card_rows = _extract_clerk_card_rows(card_html)
                    log.info("  card view: %d cards parsed", len(card_rows))
                    by_doc = {r.get("doc_number"): r
                              for r in rows if r.get("doc_number")}
                    cons_added = 0
                    for cr in card_rows:
                        dn = cr.get("doc_number")
                        if not dn or dn not in by_doc:
                            continue
                        for fld in ("consideration", "instrument_date",
                                    "doc_status", "num_pages"):
                            v = cr.get(fld)
                            if v and not by_doc[dn].get(fld):
                                by_doc[dn][fld] = v
                                if fld == "consideration":
                                    cons_added += 1
                    log.info("  consideration merged into %d rows", cons_added)
                except Exception as exc:
                    log.warning("card-view scrape failed (continuing): %s", exc)

            kept = 0
            for raw in rows:
                # CCLN-specific filter: only keep records where the
                # CITY OF CORPUS CHRISTI is the GRANTOR (filing the lien).
                # The homeowner is the grantee — the owner-swap in
                # _normalize_clerk_row puts them in the owner field.
                raw_grantor = (raw.get("grantor")
                               or raw.get("Grantor") or "").upper()
                if "CITY OF CORPUS CHRISTI" not in raw_grantor:
                    continue

                try:
                    rec = _normalize_clerk_row(raw, default_cat="CCLN")
                    if rec is None:
                        continue
                    rec.cat = "CCLN"
                    rec.cat_label = CAT_TO_LABEL.get("CCLN", "City Lien")
                    if rec.doc_num and rec.doc_num not in seen:
                        seen[rec.doc_num] = rec
                        kept += 1
                except Exception as exc:
                    log.debug("bad row skipped: %s", exc)
                    continue
            log.info("  kept %d new (total so far: %d)", kept, len(seen))

            # Stop if this page returned fewer than the page size — that
            # means we hit the end of the result set.
            if len(rows) < PAGE_SIZE:
                log.info("page returned < %d rows; stopping pagination",
                         PAGE_SIZE)
                break

        await context.close()
        await browser.close()
    return list(seen.values())


def main() -> int:
    # Backfill is a one-shot run with a 60-min workflow timeout. Override
    # the module-level esearch budgets so we process every record (not
    # just the first 100), at a slightly tighter polling cadence to fit
    # the time window. Daily scraper still uses the conservative defaults.
    import fetch as _fetch
    _fetch.NCAD_SEARCH_MAX_LOOKUPS    = 5000   # effectively unlimited
    _fetch.NCAD_SEARCH_DELAY_SEC      = 1.0    # was 1.5; still polite
    _fetch.NCAD_SEARCH_PHASE_BUDGET_SEC = 55 * 60  # was 8 min
    log.info("backfill esearch knobs: max=%d, delay=%.1fs, budget=%ds",
             _fetch.NCAD_SEARCH_MAX_LOOKUPS,
             _fetch.NCAD_SEARCH_DELAY_SEC,
             _fetch.NCAD_SEARCH_PHASE_BUDGET_SEC)

    today = datetime.now(timezone.utc).date()
    # 24 months back ≈ today minus ~730 days
    start = today - timedelta(days=LOOKBACK_MONTHS * 30 + 15)
    start_iso = start.isoformat()
    end_iso = today.isoformat()
    log.info("=== CCLN backfill: %s .. %s (%d months) ===",
             start_iso, end_iso, LOOKBACK_MONTHS)

    # 1) Pull all pages.
    try:
        records = asyncio.run(_scrape_all_pages(start_iso, end_iso))
    except Exception as exc:
        log.error("pagination failed: %s\n%s", exc, traceback.format_exc())
        records = []

    log.info("=== %d unique CCLN records pulled ===", len(records))
    if not records:
        log.warning("no CCLN records — writing empty file (does NOT clobber"
                    " existing if it has data)")

    # 2) Pull addresses from legal-description text (fast, no network).
    enrich_with_parcels(records, owner_lookup={})

    # 3) NCAD esearch enrichment for owner-name lookups. This is the slow
    # phase — for hundreds of records expect 10-20 minutes. The cache
    # speeds up subsequent runs.
    try:
        gained = enrich_via_ncad_search(records)
        log.info("esearch gained addresses for %d records", gained)
    except Exception as exc:
        log.error("esearch enrichment failed: %s", exc)

    # 4) Score so the CRM tab can show flags + a relative score.
    idx = build_owner_cat_index(records)
    for rec in records:
        try:
            compute_flags_and_score(rec, end_iso, idx)
        except Exception:
            pass
    records.sort(key=lambda r: r.score or 0, reverse=True)

    # 5) Replace city_liens.json wholesale (NOT merge). The backfill is
    # authoritative for the 24-month window; if a doc_num was previously
    # there with stale/incomplete data (no consideration), the new
    # version replaces it. Daily scrapes downstream use merge to preserve
    # this richer data and just add new records.
    save_city_liens([asdict(r) for r in records])
    log.info("=== backfill done: %d cumulative CCLN records (replaced) ===",
             len(records))
    return 0


if __name__ == "__main__":
    sys.exit(main())
