"""Re-corroborate NCAD parcel matches against current clerk legals.

Standalone post-PDF-parse pass that fixes a known structural defect in
the daily scrape flow: at the time NCAD owner-name search runs, the
foreclosure record's `legal` field is often still empty (it gets
populated later by the PDF-text parser). The result is that the
corroboration guard in fetch.py:_pick_best_esearch_row short-circuits
(no legal to compare against) and a wrong-but-plausible parcel can be
attached purely by owner-name score.

Concrete example from real data — doc 2026000263 (Leo Rodriguez):
  - Clerk-side legal (now): "WOODLAND CREEK LOT 15 BLOCK 10 UNIT V"
  - Attached NCAD parcel 190276 has legal:
        "BAY TERR NO 2 LTS 19 AND 2O BK 16O2"
  - These do NOT match. The matcher would have rejected this parcel
    if it had been called with the clerk legal — but the legal was
    empty at scrape time.

What this script does
---------------------
For every record in dashboard/foreclosures.json that has BOTH:
  (a) an attached `ncad_prop_id`, AND
  (b) a clerk-side `legal` that normalizes cleanly (subdivision + lot),

we open the NCAD property page directly (no session token needed —
/Property/View/{id} is not session-gated), parse its Legal Description,
and run pdf_text_extractor.legal_descriptions_match() between the two.

Eviction rules (conservative by default — never reject on uncertainty):
  - matcher returns True            -> good match, no change
  - matcher returns False AND both
    sides normalize cleanly         -> EVICT (clear NCAD-derived fields)
  - matcher returns False because
    NCAD-side legal didn't normalize
    or wasn't fetchable              -> KEEP (logged as 'ambiguous')
  - any error fetching the page      -> KEEP (logged as 'unverifiable')

On eviction we clear ONLY the NCAD-sourced fields:
    ncad_prop_id, ncad_owner_id, ncad_year, appraised_value,
    mail_address, mail_city, mail_state, mail_zip
Fields populated by the PDF parser (owner, legal, lender, loan_amount,
prop_address) are NEVER touched. Browser-side manual overrides live in
localStorage and are likewise untouched (we can't see them here; they
win at render time).

Modes
-----
Default: DRY-RUN. Walks every eligible record, fetches its NCAD page,
prints a verdict + summary, and writes data/recorroborate_log.json so
you can review exactly what WOULD change. The JSON data files are NOT
modified.

Apply mode (set env RECORROBORATE_APPLY=1): same as dry-run, but also
mutates dashboard/foreclosures.json AND data/foreclosures.json with
the evictions. The log is still written.

Rollback: this script never deletes the log; each run overwrites the
previous one. To undo evictions, `git revert` the commit produced by
the workflow — both JSON files are committed together so a single
revert restores prior state.

Usage
-----
Local: python scraper/recorroborate_ncad.py
       RECORROBORATE_APPLY=1 python scraper/recorroborate_ncad.py

CI: triggered manually via .github/workflows/recorroborate_ncad.yml.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Allow standalone execution from the repo root: ensure the scraper
# directory is on sys.path so the pdf_text_extractor import resolves.
HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from pdf_text_extractor import (  # type: ignore
    legal_descriptions_match,
    normalize_legal_for_match,
)

try:
    from bs4 import BeautifulSoup
except ImportError as exc:
    raise SystemExit(
        "BeautifulSoup4 is required (pip install beautifulsoup4 lxml)"
    ) from exc

try:
    from playwright.async_api import async_playwright
except ImportError as exc:
    raise SystemExit(
        "Playwright is required (pip install playwright; "
        "python -m playwright install chromium)"
    ) from exc


# ----------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------
REPO_ROOT = HERE.parent
DASH_JSON = REPO_ROOT / "dashboard" / "foreclosures.json"
DATA_JSON = REPO_ROOT / "data" / "foreclosures.json"
LOG_JSON  = REPO_ROOT / "data" / "recorroborate_log.json"

NCAD_BASE = "https://esearch.nuecescad.net"

# NCAD detail pages load fast; even on cold start ~1s is enough. We add
# a generous timeout for safety but each fetch is short. A small pause
# between fetches is courtesy, not strictly required.
PAGE_TIMEOUT_MS = 15_000
INTER_FETCH_DELAY_S = 0.4

# Fields cleared on eviction. These are exactly the NCAD-derived fields
# — we deliberately do NOT touch owner / legal / lender / loan_amount /
# prop_address (those are PDF-sourced or clerk-sourced).
EVICT_FIELDS = (
    "ncad_prop_id",
    "ncad_owner_id",
    "ncad_year",
    "appraised_value",
    "mail_address",
    "mail_city",
    "mail_state",
    "mail_zip",
)

# Mode: dry-run (default) or apply.
APPLY_MODE = os.environ.get("RECORROBORATE_APPLY", "").strip() == "1"

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("recorroborate_ncad")


# ----------------------------------------------------------------------
# Property-page legal extraction
# ----------------------------------------------------------------------
def _parse_property_legal(html: str) -> str:
    """Return the 'Legal Description' value from a NCAD property page,
    or '' if not found. The page uses standard label/value layouts —
    we try the common patterns the BIS Consultants UI exposes.
    """
    if not html:
        return ""
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")

    # Pattern A: <dl><dt>Legal Description</dt><dd>...value...</dd></dl>
    for dl in soup.find_all("dl"):
        dts = dl.find_all("dt")
        dds = dl.find_all("dd")
        for dt, dd in zip(dts, dds):
            label = dt.get_text(" ", strip=True).lower().rstrip(":")
            if "legal description" in label:
                v = dd.get_text(" ", strip=True)
                if v:
                    return v

    # Pattern B: tables with two-cell rows, label-then-value.
    for tr in soup.find_all("tr"):
        cells = tr.find_all(["th", "td"])
        if len(cells) < 2:
            continue
        label = cells[0].get_text(" ", strip=True).lower().rstrip(":")
        if "legal description" in label:
            v = cells[1].get_text(" ", strip=True)
            if v:
                return v

    # Pattern C: any element whose text starts with "Legal Description:"
    # followed by the value (defensive fallback for unusual layouts).
    text = soup.get_text("\n", strip=True)
    m = re.search(
        r"Legal\s+Description\s*:?\s*\n?\s*([^\n]+)",
        text, re.IGNORECASE,
    )
    if m:
        return m.group(1).strip()

    return ""


# ----------------------------------------------------------------------
# Eligibility / decision logic
# ----------------------------------------------------------------------
def _is_eligible(r: Dict) -> Tuple[bool, str]:
    """Returns (eligible, reason). A record is eligible for the
    re-corroboration check when it has an NCAD parcel attached AND a
    clerk-side legal that normalizes into a usable (subdivision, lot)
    key. If the legal can't be normalized, we cannot safely tell good
    from bad, so we leave the record alone — conservative by default.
    """
    if not r.get("ncad_prop_id"):
        return False, "no ncad_prop_id attached"
    legal = (r.get("legal") or "").strip()
    if not legal:
        return False, "clerk legal empty (can't safely verify)"
    sub, lot, _blk = normalize_legal_for_match(legal)
    if not sub or not lot:
        return False, ("clerk legal too sparse to compare "
                       f"(sub={sub!r}, lot={lot!r})")
    return True, ""


def _decide(clerk_legal: str, ncad_legal: str) -> str:
    """Return one of: 'match', 'mismatch', 'ambiguous'."""
    if not ncad_legal:
        return "ambiguous"
    sub_n, lot_n, _ = normalize_legal_for_match(ncad_legal)
    if not sub_n or not lot_n:
        return "ambiguous"
    return "match" if legal_descriptions_match(clerk_legal, ncad_legal) \
        else "mismatch"


# ----------------------------------------------------------------------
# Network: fetch each property page
# ----------------------------------------------------------------------
def _property_url(r: Dict) -> str:
    pid = r["ncad_prop_id"]
    year = r.get("ncad_year") or "2026"
    oid = r.get("ncad_owner_id") or ""
    url = f"{NCAD_BASE}/Property/View/{pid}?year={year}"
    if oid:
        url += f"&ownerId={oid}"
    return url


async def _fetch_property_legal(page, url: str) -> Tuple[str, str]:
    """Returns (legal, error_str). On success error_str is ''.
    On failure legal is '' and error_str describes the problem."""
    try:
        await page.goto(url, wait_until="domcontentloaded",
                        timeout=PAGE_TIMEOUT_MS)
        # Small wait for any client-side rendering. NCAD detail pages
        # are mostly server-rendered so this is brief.
        await page.wait_for_timeout(400)
        html = await page.content()
    except Exception as exc:
        return "", f"fetch error: {exc}"
    legal = _parse_property_legal(html)
    if not legal:
        return "", "legal description not found on page"
    return legal, ""


# ----------------------------------------------------------------------
# Main pass
# ----------------------------------------------------------------------
async def _run() -> Dict:
    if not DASH_JSON.exists():
        raise SystemExit(f"missing {DASH_JSON}")
    payload = json.loads(DASH_JSON.read_text(encoding="utf-8"))
    records = payload.get("records") or []
    log.info("loaded %d records from %s", len(records), DASH_JSON)

    eligible: List[Dict] = []
    skipped: List[Tuple[str, str]] = []  # (doc_num, reason)
    for r in records:
        ok, why = _is_eligible(r)
        if ok:
            eligible.append(r)
        else:
            skipped.append((r.get("doc_num", "?"), why))

    log.info("eligible for re-check: %d", len(eligible))
    log.info("skipped:               %d  (kept as-is, conservative)",
              len(skipped))

    # Reporting buckets
    matches: List[Dict] = []
    mismatches: List[Dict] = []   # WOULD evict (or DID evict in apply)
    ambiguous: List[Dict] = []    # kept
    errors: List[Dict] = []       # kept

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        for i, r in enumerate(eligible, 1):
            dn = r.get("doc_num", "?")
            clerk_legal = r.get("legal", "")
            url = _property_url(r)
            ncad_legal, err = await _fetch_property_legal(page, url)

            entry = {
                "doc_num": dn,
                "ncad_prop_id": r.get("ncad_prop_id"),
                "url": url,
                "clerk_legal": clerk_legal,
                "ncad_legal": ncad_legal,
            }
            if err:
                entry["verdict"] = "error"
                entry["error"] = err
                errors.append(entry)
                log.info("[%d/%d] %s  ERROR: %s",
                          i, len(eligible), dn, err)
            else:
                verdict = _decide(clerk_legal, ncad_legal)
                entry["verdict"] = verdict
                if verdict == "match":
                    matches.append(entry)
                    log.info("[%d/%d] %s  match", i, len(eligible), dn)
                elif verdict == "ambiguous":
                    ambiguous.append(entry)
                    log.info("[%d/%d] %s  AMBIGUOUS (kept) — "
                             "ncad_legal=%r",
                             i, len(eligible), dn,
                             ncad_legal[:70])
                else:  # mismatch
                    mismatches.append(entry)
                    log.info("[%d/%d] %s  MISMATCH%s",
                             i, len(eligible), dn,
                             " (will evict)" if APPLY_MODE
                             else " (would evict — DRY RUN)")
                    log.info("       clerk: %r", clerk_legal[:80])
                    log.info("       ncad : %r", ncad_legal[:80])
            await asyncio.sleep(INTER_FETCH_DELAY_S)

        await context.close()
        await browser.close()

    # ------------------------------------------------------------------
    # Apply evictions (only when explicitly requested)
    # ------------------------------------------------------------------
    if APPLY_MODE and mismatches:
        evicted_docs = {e["doc_num"] for e in mismatches}
        for r in records:
            if r.get("doc_num") in evicted_docs:
                for k in EVICT_FIELDS:
                    if k in r:
                        # Use the same "empty" representation that the
                        # rest of the data uses for never-matched
                        # records (string for IDs, None for value).
                        if k == "appraised_value":
                            r[k] = None
                        else:
                            r[k] = ""
        DASH_JSON.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        if DATA_JSON.exists():
            # Keep data/foreclosures.json in lockstep (the daily scrape
            # writes both). This is just the canonical copy under data/.
            data_payload = json.loads(DATA_JSON.read_text(encoding="utf-8"))
            data_recs = data_payload.get("records") or []
            for r in data_recs:
                if r.get("doc_num") in evicted_docs:
                    for k in EVICT_FIELDS:
                        if k in r:
                            r[k] = None if k == "appraised_value" else ""
            DATA_JSON.write_text(
                json.dumps(data_payload, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        log.info("APPLIED: evicted NCAD link on %d record(s)",
                  len(evicted_docs))

    # ------------------------------------------------------------------
    # Structured log so the workflow run is reviewable later
    # ------------------------------------------------------------------
    summary = {
        "mode": "apply" if APPLY_MODE else "dry_run",
        "total_records": len(records),
        "eligible": len(eligible),
        "skipped": len(skipped),
        "matches": len(matches),
        "mismatches": len(mismatches),
        "ambiguous": len(ambiguous),
        "errors": len(errors),
        "mismatch_details": mismatches,
        "ambiguous_details": ambiguous,
        "error_details": errors,
        # Skipped list is for completeness; usually mundane.
        "skipped_details": [{"doc_num": d, "reason": r}
                            for d, r in skipped],
    }
    LOG_JSON.parent.mkdir(parents=True, exist_ok=True)
    LOG_JSON.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    log.info("---- summary ----")
    log.info("mode          : %s", summary["mode"])
    log.info("matches       : %d", summary["matches"])
    log.info("mismatches    : %d %s",
              summary["mismatches"],
              "(evicted)" if APPLY_MODE else "(would evict — dry run)")
    log.info("ambiguous     : %d (kept)", summary["ambiguous"])
    log.info("errors        : %d (kept)", summary["errors"])
    log.info("skipped       : %d (ineligible)", summary["skipped"])
    log.info("log written to %s", LOG_JSON)
    if not APPLY_MODE and mismatches:
        log.info("DRY RUN — no JSON files changed. "
                 "Set RECORROBORATE_APPLY=1 to apply evictions.")
    return summary


if __name__ == "__main__":
    asyncio.run(_run())
