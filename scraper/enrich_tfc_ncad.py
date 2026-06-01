"""
FC NCAD Enrichment — Account-Number Backfill via Detail Page
============================================================

Backfills the NCAD account number (the dashed 12-digit "Geographic
ID", e.g. "6855-0012-0170") onto mortgage-foreclosure records so the
dashboard's NCTAX column can deep-link to the Nueces County Tax
Office. The tax office's account-details page is keyed on that number
(`account-details.jsp?can=685500120170`); without it, every FC row
falls back to the address-search magnifier instead of the green
"direct link" house.

Why detail-page-only (no address search)?
-----------------------------------------
The TFC enricher (enrich_tfc_ncad.py) does an address-based esearch
because TFC records arrive with no NCAD identifiers — it has to FIND
the prop_id first. FC records are different: every one already carries
`ncad_prop_id`, `ncad_year`, and `ncad_owner_id` (set by the FC
matcher). That's exactly what's needed to open the property detail
page directly:

    /Property/View/{prop_id}?year={year}&ownerId={owner_id}

So this script skips the entire fragile search half — no token
minting, no street-name candidate parsing, no result-list scoring,
no retry-on-empty soft-throttle handling. It's just one detail-page
fetch per record, and the Geographic ID is read straight off that
page. Far fewer failure modes than the address-search enrichers.

What we add to each FC record on a successful read:
  * ncad_account_num — the dashed 12-digit Geographic ID

Nothing else is touched. FC records already have good owner / legal /
market_value / mailing-address data from the matcher, and overwriting
those has caused regressions before, so this script writes ONE field
and leaves everything else alone.

Operational notes
-----------------
* DRY-RUN by default. The run writes a log
  (data/enrich_fc_ncad_log.json) listing the account number it WOULD
  set per record, but does not modify foreclosures.json. Eyeball the
  log first — confirm the extracted numbers are dashed 4-4-4 values
  that match the property — THEN re-run with ENRICH_FC_APPLY=1.
* Idempotent. Re-running skips records that already have
  `ncad_account_num` unless ENRICH_FC_FORCE=1.
* Designed for GitHub Actions on manual trigger. The workflow that
  commits the result back needs `permissions: contents: write`.

Env vars:
  ENRICH_FC_APPLY=1   actually write foreclosures.json (default: dry-run)
  ENRICH_FC_FORCE=1   re-read records that already have an account num
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from bs4 import BeautifulSoup
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "BeautifulSoup4 is required (pip install beautifulsoup4 lxml)"
    ) from exc

try:
    from playwright.async_api import async_playwright
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "Playwright is required (pip install playwright; "
        "python -m playwright install chromium)"
    ) from exc


# ==================================================================
# Configuration
# ==================================================================

REPO_ROOT = Path(__file__).resolve().parent.parent

# Mirror the TFC enricher's dual-location convention: the dashboard
# (GitHub Pages) serves its own copy and data/ holds the canonical
# build output. We load from whichever exists first and write back to
# every candidate whose parent directory is present, so the served
# file and the source file both stay in sync.
FC_OUTPUTS = [
    REPO_ROOT / "dashboard" / "foreclosures.json",
    REPO_ROOT / "data" / "foreclosures.json",
]
ENRICHMENT_LOG = REPO_ROOT / "data" / "enrich_fc_ncad_log.json"

NCAD_ESEARCH_BASE = "https://esearch.nuecescad.net"
NCAD_YEAR_DEFAULT = "2026"   # used only if a record has no ncad_year

# Detail pages aren't the rate-limited search endpoint, but we keep
# the same conservative 1.5s spacing the address-search enrichers use
# so a 130-record run never looks like a burst. ~130 * 1.5s ≈ 3-4 min.
INTER_FETCH_DELAY_S = 1.5
DETAIL_TIMEOUT_MS = 15_000

# Re-warm the BIS session (re-navigate the homepage) every N fetches.
# The detail page doesn't require the searchSessionToken, but the
# session cookie set on the homepage keeps long runs healthy — same
# ~5-minute-TTL caution the token-refresh logic uses elsewhere.
SESSION_REWARM_INTERVAL = 25

# Apply mode: DRY-RUN by default — writes the log but does not modify
# foreclosures.json. Set ENRICH_FC_APPLY=1 to actually write.
APPLY = os.getenv("ENRICH_FC_APPLY", "0") == "1"
# Force mode: re-read every record even if it already has an account
# number from a prior run.
FORCE = os.getenv("ENRICH_FC_FORCE", "0") == "1"

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("enrich-fc-ncad")


# ==================================================================
# Account-number (Geographic ID) extraction
# ==================================================================
#
# On the BIS Consultants detail page the tax account number is shown
# as the "Geographic ID" — a dashed 12-digit value like
# "6855-0012-0170". That is the SAME number the Nueces County Tax
# Office keys on, just with the dashes stripped for its `can=`
# parameter (verified 2026-05-28: 6855-0012-0170 -> can=685500120170).
# We store the dashed form to match how DELQ/CCLN store
# ncad_account_num; the dashboard strips non-digits itself.

# The canonical Nueces geo-id shape. Distinctive enough that a
# whole-page scan for it rarely false-positives — a property detail
# page carries exactly one geo id (its own).
GEO_ID_RE = re.compile(r"\b(\d{4}-\d{4}-\d{4})\b")

# Label tokens to look for, most specific first. Each entry is a tuple
# of substrings that must ALL appear (lowercased) in the field label.
ACCOUNT_LABEL_TOKEN_SETS = [
    ("geographic", "id"),
    ("geo", "id"),
    ("property", "account"),
    ("account",),
    ("parcel",),
]


def _build_text_pairs(soup: "BeautifulSoup") -> Dict[str, str]:
    """Extract label->value pairs from the detail page.

    Mirrors the TFC enricher's detail parsing: NCAD renders the
    property's fields as <dl> pairs, labeled cards, and two-cell
    label/value table rows. We harvest all three into one dict.
    """
    text_pairs: Dict[str, str] = {}

    # Pattern A: <dl><dt>Label</dt><dd>Value</dd>
    for dl in soup.find_all("dl"):
        dts = dl.find_all("dt")
        dds = dl.find_all("dd")
        for dt, dd in zip(dts, dds):
            label = dt.get_text(" ", strip=True).lower().rstrip(":")
            value = dd.get_text(" ", strip=True)
            if label and value:
                text_pairs.setdefault(label, value)

    # Pattern B: labeled cards/panels/sections (header + body).
    for card in soup.find_all(
            class_=re.compile(r"card|panel|section", re.IGNORECASE)):
        header = card.find(class_=re.compile(r"header|title", re.IGNORECASE))
        body = card.find(class_=re.compile(r"body|content", re.IGNORECASE))
        if not header or not body:
            continue
        label = header.get_text(" ", strip=True).lower().rstrip(":")
        value = body.get_text(" ", strip=True)
        if label and value:
            text_pairs.setdefault(label, value)

    # Pattern C: two-cell label/value table rows — the layout NCAD's
    # detail page actually uses (verified against prop 182368).
    for tr in soup.find_all("tr"):
        cells = tr.find_all(["th", "td"])
        if len(cells) < 2:
            continue
        label = cells[0].get_text(" ", strip=True).lower().rstrip(":")
        value = cells[1].get_text(" ", strip=True)
        if label and value:
            text_pairs.setdefault(label, value)

    return text_pairs


def _extract_account_num(html: str) -> str:
    """Pull the dashed 12-digit Geographic ID off a detail page.

    Strategy, in order:
      1. Look for it under an account-like label (Geographic ID, etc.)
         and confirm the value matches the dashed geo-id shape.
      2. Label-agnostic: scan every label/value pair for a geo-id.
      3. Whole-page: if the page text contains exactly one distinct
         geo-id, use it (a single-property page has just its own).

    Returns the dashed form ("6855-0012-0170") or "" if nothing
    confidently matched (logged so it can be handled manually).
    """
    if not html:
        return ""
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")

    text_pairs = _build_text_pairs(soup)

    def labeled_value(token_set: Tuple[str, ...]) -> str:
        for label, value in text_pairs.items():
            if all(tok in label for tok in token_set):
                return value
        return ""

    # 1. Labeled lookup, validated against the geo-id shape.
    for token_set in ACCOUNT_LABEL_TOKEN_SETS:
        value = labeled_value(token_set)
        if not value:
            continue
        m = GEO_ID_RE.search(value)
        if m:
            return m.group(1)

    # 2. Label-agnostic: any pair whose value is a geo-id.
    for value in text_pairs.values():
        m = GEO_ID_RE.search(value)
        if m:
            return m.group(1)

    # 3. Whole-page unique match. A single property's page should show
    #    exactly one geo id. If the page somehow shows several distinct
    #    ones, bail rather than guess wrong.
    page_text = soup.get_text(" ", strip=True)
    found = set(GEO_ID_RE.findall(page_text))
    if len(found) == 1:
        return next(iter(found))

    return ""


# ==================================================================
# NCAD session + detail-page fetch
# ==================================================================

async def _warm_session(page) -> None:
    """Navigate the esearch homepage to establish/refresh the BIS
    session cookie. The detail page doesn't need the searchSessionToken,
    but a warm session keeps long runs from going stale. Non-fatal on
    failure — the detail fetch will still be attempted.
    """
    try:
        await page.goto(
            NCAD_ESEARCH_BASE + "/",
            wait_until="domcontentloaded",
            timeout=30_000,
        )
        await page.wait_for_timeout(300)
    except Exception as exc:
        log.warning("  session warm-up failed (continuing): %s", exc)


async def _fetch_detail_html(page,
                             prop_id: str,
                             year: str,
                             owner_id: str) -> str:
    """Fetch the property detail page HTML for a known prop_id.

    Builds the same URL the TFC enricher uses for mailing addresses:
        /Property/View/{prop_id}?year={year}&ownerId={owner_id}
    Returns the rendered HTML, or "" on navigation failure.
    """
    if not prop_id:
        return ""
    url = f"{NCAD_ESEARCH_BASE}/Property/View/{prop_id}?year={year}"
    if owner_id:
        url += f"&ownerId={owner_id}"
    try:
        await page.goto(url, wait_until="domcontentloaded",
                        timeout=DETAIL_TIMEOUT_MS)
        await page.wait_for_timeout(400)
        return await page.content()
    except Exception as exc:
        log.warning("    detail nav failed for prop_id=%s: %s", prop_id, exc)
        return ""


# ==================================================================
# FC record loading + writing
# ==================================================================

def _load_fc_records() -> Tuple[Path, Dict[str, Any], List[Dict[str, Any]]]:
    """Load foreclosures.json. Returns (source_path, payload, records).
    Prefers dashboard/foreclosures.json; falls back to data/."""
    for path in FC_OUTPUTS:
        if path.exists():
            with path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            records = data.get("records") or []
            log.info("loaded %d FC records from %s", len(records), path)
            return path, data, records
    raise SystemExit(
        f"No foreclosures.json found at any of: "
        f"{[str(p) for p in FC_OUTPUTS]}"
    )


def _write_fc_records(payload: Dict[str, Any],
                      records: List[Dict[str, Any]]) -> None:
    """Write the enriched payload back. Updates `records` and stamps
    `ncad_account_enriched_at`; leaves total / pre_foreclosure /
    post_foreclosure untouched. Writes to every FC_OUTPUTS path whose
    parent directory exists (so both the served copy and the source
    copy update), matching the TFC enricher's dual-write convention.
    """
    payload["records"] = records
    payload["ncad_account_enriched_at"] = datetime.now(
        timezone.utc).isoformat(timespec="seconds")
    wrote_any = False
    for path in FC_OUTPUTS:
        if not path.parent.exists():
            log.info("skipping %s (parent dir absent)", path)
            continue
        with path.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
        log.info("wrote enriched JSON to %s", path)
        wrote_any = True
    if not wrote_any:
        log.warning("no FC_OUTPUTS paths were writable — nothing saved")


def _write_log(entries: List[Dict[str, Any]],
               counts: Dict[str, int]) -> None:
    ENRICHMENT_LOG.parent.mkdir(parents=True, exist_ok=True)
    out = {
        "ran_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "apply_mode": APPLY,
        "force_mode": FORCE,
        "summary": counts,
        "entries": entries,
    }
    with ENRICHMENT_LOG.open("w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2, ensure_ascii=False)
    log.info("wrote run log to %s", ENRICHMENT_LOG)


# ==================================================================
# Main async driver
# ==================================================================

async def _enrich_all(records: List[Dict[str, Any]]
                      ) -> Tuple[Dict[str, int], List[Dict[str, Any]]]:
    """Iterate FC records, fetch each detail page, extract the account
    number, and (when APPLY) write it back in place. Returns
    (counts, log_entries).
    """
    log_entries: List[Dict[str, Any]] = []
    matched = 0          # account number found
    no_account = 0       # detail page fetched but no geo-id parsed
    no_prop_id = 0       # record had no ncad_prop_id to fetch with
    skipped_already = 0  # already had an account number (no FORCE)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True, args=["--no-sandbox"],
        )
        context = await browser.new_context(user_agent=USER_AGENT)
        page = await context.new_page()

        try:
            log.info("warming session...")
            await _warm_session(page)
            fetches_since_rewarm = 0

            for i, rec in enumerate(records):
                doc_num = rec.get("doc_num", "")
                addr = rec.get("prop_address", "")
                prop_id = str(rec.get("ncad_prop_id") or "").strip()
                year = str(rec.get("ncad_year") or NCAD_YEAR_DEFAULT).strip()
                owner_id = str(rec.get("ncad_owner_id") or "").strip()

                # No prop_id → nothing to fetch with. FC records should
                # all have one, but guard anyway.
                if not prop_id:
                    no_prop_id += 1
                    log_entries.append({
                        "doc_num": doc_num,
                        "prop_address": addr,
                        "result": "no-prop-id",
                    })
                    continue

                # Idempotent skip.
                if rec.get("ncad_account_num") and not FORCE:
                    skipped_already += 1
                    continue

                # Periodic session re-warm for long runs.
                if fetches_since_rewarm >= SESSION_REWARM_INTERVAL:
                    log.info("  re-warming session (after %d fetches)",
                             fetches_since_rewarm)
                    await _warm_session(page)
                    fetches_since_rewarm = 0

                log.info("[%d/%d] doc=%s prop_id=%s addr=%r",
                         i + 1, len(records), doc_num, prop_id, addr)

                html = await _fetch_detail_html(page, prop_id, year, owner_id)
                fetches_since_rewarm += 1

                account = _extract_account_num(html)

                if account:
                    matched += 1
                    if APPLY:
                        rec["ncad_account_num"] = account
                    log.info("  -> account=%s", account)
                    log_entries.append({
                        "doc_num": doc_num,
                        "prop_address": addr,
                        "ncad_prop_id": prop_id,
                        "result": "matched",
                        "ncad_account_num": account,
                    })
                else:
                    no_account += 1
                    log.info("  -> no account number found on detail page")
                    log_entries.append({
                        "doc_num": doc_num,
                        "prop_address": addr,
                        "ncad_prop_id": prop_id,
                        "result": "no-account-on-page",
                    })

                await asyncio.sleep(INTER_FETCH_DELAY_S)
        finally:
            await context.close()
            await browser.close()

    counts = {
        "matched": matched,
        "no_account_on_page": no_account,
        "no_prop_id": no_prop_id,
        "skipped_already": skipped_already,
        "total": len(records),
    }
    log.info("summary: matched=%d, no-account=%d, no-prop-id=%d, "
             "skipped-already=%d, total=%d",
             matched, no_account, no_prop_id, skipped_already, len(records))
    return counts, log_entries


# ==================================================================
# Main
# ==================================================================

def main() -> int:
    log.info("=== FC NCAD Account Enrichment (detail-page) — "
             "apply=%s force=%s ===", APPLY, FORCE)

    _src_path, payload, records = _load_fc_records()

    if not records:
        log.warning("no records to enrich — exiting cleanly")
        return 0

    counts, log_entries = asyncio.run(_enrich_all(records))
    _write_log(log_entries, counts)

    if APPLY:
        _write_fc_records(payload, records)
        log.info("APPLIED — enriched JSON written.")
    else:
        log.info("DRY-RUN — no changes written. Review %s, then "
                 "re-run with ENRICH_FC_APPLY=1 to apply.",
                 ENRICHMENT_LOG)

    return 0


if __name__ == "__main__":
    sys.exit(main())
