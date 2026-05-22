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

# Path to the NCAD owner-search cache used by scraper/fetch.py. When we
# evict a wrong-matched record here, the cache still holds the same
# wrong-parcel result for that owner name. The next daily scrape would
# re-attach the wrong parcel via the cache shortcut (the cache returns
# the same answer instantly, never re-running the variant logic). To
# break that loop we expire cache entries for evicted owner names —
# next scrape then re-runs NCAD lookup with current logic and either
# finds the correct parcel or honestly records a miss.
#
# This MUST stay in sync with the constant in scraper/fetch.py
# (NCAD_SEARCH_CACHE). If that file moves, this one breaks silently.
NCAD_CACHE_PATH = REPO_ROOT / ".cache" / "ncad_search_cache.json"

NCAD_BASE = "https://esearch.nuecescad.net"

# NCAD detail pages load fast on a single fetch, but the same parcel
# often backs many foreclosure records (e.g. 8 Plutus Properties docs
# all point at the same prop_id 194322). Fetching the same URL 8 times
# back-to-back is both wasteful and a textbook rate-limit trigger —
# the first dry-run showed NCAD start returning empty pages after ~12
# rapid identical hits. We mitigate three ways:
#   (1) Per-parcel in-memory cache keyed by (prop_id, year, owner_id).
#       Each unique URL is fetched at most once per run.
#   (2) Modest base delay between fetches (1.0s — courtesy + safety).
#   (3) Retry-with-backoff: if a fetch returns no legal, wait, retry
#       once. NCAD sometimes serves a transient empty body that's fine
#       after a brief pause.
PAGE_TIMEOUT_MS = 15_000
INTER_FETCH_DELAY_S = 1.0
RETRY_BACKOFF_S = 4.0
MAX_RETRIES = 2  # initial + 1 retry

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

# Conditionally-evicted prop_address fields. These are PDF-sourced
# IN THEORY, but if the PDF parser failed to extract an address (e.g.
# the "Property Address/Mailing Address:" combined-label format) the
# scraper falls back to using the NCAD-cache site_addr as the
# foreclosure's prop_address. When that NCAD parcel is later evicted
# as a wrong match (Cardenas, Flores — name-search matched the wrong
# person), prop_address is the wrong parcel's address too.
#
# Logic at apply time: look up the record's owner in the NCAD search
# cache. If the cached site_addr matches the record's prop_address
# (after normalization), we know prop_address came from esearch
# attachment, not from the PDF parser — clear it too.
PROP_ADDR_FIELDS = (
    "prop_address", "prop_city", "prop_state", "prop_zip",
)
NCAD_SEARCH_CACHE = REPO_ROOT / ".cache" / "ncad_search_cache.json"

def _normalize_addr(s: str) -> str:
    """Lowercase + collapse whitespace + strip punctuation for address
    comparison. Same logic used elsewhere to detect address equality
    despite minor formatting differences.

    Apostrophe handling: Irish-derived street names like O'Malley,
    O'Brien, O'Reilly appear in PDFs WITH an apostrophe (ASCII ' or
    curly ') but NCAD's database stores them WITHOUT — "O MALLEY",
    "O BRIEN". We normalize both to the no-apostrophe form so they
    compare equal.
        "4401 O'MALLEY DR" → "4401 o malley dr"
        "4401 O MALLEY DR" → "4401 o malley dr"  (same)
    Both ASCII and Unicode (U+2018/U+2019) apostrophes are stripped.
    """
    s = (s or "").lower()
    s = re.sub(r"[.,]", "", s)
    # Replace apostrophes (ASCII and Unicode curly) with a space so
    # "o'malley" and "o malley" normalize identically. We use a space
    # rather than empty-string so we don't accidentally fuse tokens
    # that were already separated by a comma+apostrophe pattern.
    s = re.sub(r"[\u2018\u2019']", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def _ncad_cache_addr_for_owner(owner_name: str) -> str:
    """Return the cached NCAD site_addr for this owner name, or empty
    if not in cache. Reads the cache file fresh each call (no caching
    here) — cache file is small and this only runs at apply time."""
    if not owner_name or not NCAD_SEARCH_CACHE.exists():
        return ""
    try:
        with open(NCAD_SEARCH_CACHE) as f:
            cache_doc = json.load(f)
        data = cache_doc.get("data") or {}
        entry = data.get(owner_name) or {}
        return (entry.get("site_addr") or "").strip()
    except Exception:
        return ""

# Mode: dry-run (default) or apply.
APPLY_MODE = os.environ.get("RECORROBORATE_APPLY", "").strip() == "1"

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("recorroborate_ncad")


# ----------------------------------------------------------------------
# NCAD search cache expiry
# ----------------------------------------------------------------------
def _expire_ncad_cache_entries(owner_names: List[str]) -> Dict[str, int]:
    """Remove cache entries for the given owner names so the next daily
    scrape re-runs NCAD lookup with current logic rather than returning
    the stale (wrong-parcel) cached result.

    Cache file structure (must match scraper/fetch.py):
        {"_version": "v6", "data": {<owner_name>: {...} | None}}

    Returns a stats dict: {"expired": int, "missing": int, "total_keys_after": int}.
      - expired: entries that were found and removed
      - missing: names we tried to expire but weren't in the cache
      - total_keys_after: cache size after expiry (sanity check)

    If the cache file doesn't exist or is unreadable, returns
    {"expired": 0, "missing": <count>, "error": "..."} and logs a
    warning. We DELIBERATELY do not raise — the eviction in
    foreclosures.json is the important state change; failing to clean
    the cache means the next scrape will reattach a wrong parcel, but
    that's recoverable (re-run this script), and crashing here would
    leave the JSON evictions only half-applied.
    """
    if not owner_names:
        return {"expired": 0, "missing": 0, "total_keys_after": 0}
    if not NCAD_CACHE_PATH.exists():
        log.warning("NCAD cache file not found at %s — skipping cache "
                    "expiry (next scrape will populate it fresh)",
                    NCAD_CACHE_PATH)
        return {"expired": 0, "missing": len(owner_names),
                "error": "cache_file_missing"}
    try:
        raw = json.loads(NCAD_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("could not read NCAD cache (%s) — skipping expiry",
                    exc)
        return {"expired": 0, "missing": len(owner_names),
                "error": f"read_failed: {exc}"}

    # Unwrap the versioned envelope. We only know how to expire v6.
    # If we encounter a different version, refuse to touch it — fetch.py
    # has its own upgrade machinery that will rebuild the cache cleanly
    # on the next scrape, which is a safe fallback.
    version = raw.get("_version") if isinstance(raw, dict) else None
    if version != "v6":
        log.warning("NCAD cache version is %r, not v6 — skipping expiry "
                    "(fetch.py will rebuild on next scrape)", version)
        return {"expired": 0, "missing": len(owner_names),
                "error": f"unsupported_version: {version}"}

    data = raw.get("data") or {}
    if not isinstance(data, dict):
        log.warning("NCAD cache 'data' field is not a dict — skipping expiry")
        return {"expired": 0, "missing": len(owner_names),
                "error": "malformed_data"}

    expired = 0
    missing = 0
    for name in owner_names:
        if name in data:
            del data[name]
            expired += 1
        else:
            missing += 1

    # Only rewrite the file if something actually changed. Avoids
    # spurious "modified file" diffs in git for no-op runs.
    if expired > 0:
        raw["data"] = data
        try:
            NCAD_CACHE_PATH.write_text(
                json.dumps(raw, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            log.info("NCAD cache: expired %d entries, kept %d",
                     expired, len(data))
        except Exception as exc:
            log.warning("could not write NCAD cache after expiry (%s) — "
                        "next scrape may still see stale entries", exc)
            return {"expired": 0, "missing": missing,
                    "error": f"write_failed: {exc}"}
    else:
        log.info("NCAD cache: no entries to expire (none of the %d "
                 "evicted owner names were in the cache)",
                 len(owner_names))

    return {"expired": expired, "missing": missing,
            "total_keys_after": len(data)}


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


async def _fetch_property_legal(page, url: str,
                                 cache: Dict[str, Tuple[str, str]]
                                 ) -> Tuple[str, str]:
    """Returns (legal, error_str). On success error_str is ''.
    On failure legal is '' and error_str describes the problem.

    Uses an in-memory cache so repeated requests for the same URL
    (multiple foreclosure records sharing one NCAD parcel) reuse the
    first answer rather than re-hitting the server. Retries once on
    transient "no legal found" responses, which the first dry-run
    showed are usually rate-limiting blips that resolve after a pause.
    """
    if url in cache:
        return cache[url]
    last_err = ""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            await page.goto(url, wait_until="domcontentloaded",
                            timeout=PAGE_TIMEOUT_MS)
            await page.wait_for_timeout(400)
            html = await page.content()
        except Exception as exc:
            last_err = f"fetch error: {exc}"
            html = ""
        if html:
            legal = _parse_property_legal(html)
            if legal:
                result = (legal, "")
                cache[url] = result
                return result
            last_err = "legal description not found on page"
        if attempt < MAX_RETRIES:
            log.debug("retrying %s after %ss (attempt %d/%d)",
                       url, RETRY_BACKOFF_S, attempt, MAX_RETRIES)
            await asyncio.sleep(RETRY_BACKOFF_S)
    result = ("", last_err)
    # Cache the failure too — no point hammering a URL that just
    # failed twice; the workflow can be re-run later.
    cache[url] = result
    return result


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

        # Per-run cache keyed by full URL. Many records share parcels
        # (the Plutus cluster is the obvious case); this avoids the
        # rate-limit pattern the first dry-run hit. We also count cache
        # hits for the summary so the speedup is visible.
        url_cache: Dict[str, Tuple[str, str]] = {}
        cache_hits = 0

        for i, r in enumerate(eligible, 1):
            dn = r.get("doc_num", "?")
            clerk_legal = r.get("legal", "")
            url = _property_url(r)
            was_cached = url in url_cache
            ncad_legal, err = await _fetch_property_legal(
                page, url, url_cache)
            if was_cached:
                cache_hits += 1

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
                    log.info("[%d/%d] %s  match%s", i, len(eligible),
                              dn, "  (cached)" if was_cached else "")
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
            # Skip the inter-fetch delay on cache hits — no network
            # was hit, nothing to space out.
            if not was_cached:
                await asyncio.sleep(INTER_FETCH_DELAY_S)

        await context.close()
        await browser.close()

    # ------------------------------------------------------------------
    # Apply evictions (only when explicitly requested)
    # ------------------------------------------------------------------
    cache_expiry_stats = None
    if APPLY_MODE and mismatches:
        evicted_docs = {e["doc_num"] for e in mismatches}
        # Collect the OWNER NAMES of evicted records BEFORE we mutate
        # anything else. We need these to expire NCAD-search-cache
        # entries — without that, the next daily scrape would re-attach
        # the same wrong parcels via the cache shortcut (the cache is
        # keyed by owner name and returns the same parcel-id for the
        # same name every time, no matter how many evictions we did).
        evicted_owner_names: List[str] = []
        # Track which records ALSO had their prop_address cleared because
        # it matched the now-evicted NCAD parcel's site_addr (proof it
        # came from esearch attachment, not from PDF parser).
        prop_addr_also_evicted: List[str] = []
        for r in records:
            if r.get("doc_num") in evicted_docs:
                owner_name = (r.get("owner") or "").strip()
                if owner_name:
                    evicted_owner_names.append(owner_name)
                for k in EVICT_FIELDS:
                    if k in r:
                        # Use the same "empty" representation that the
                        # rest of the data uses for never-matched
                        # records (string for IDs, None for value).
                        if k == "appraised_value":
                            r[k] = None
                        else:
                            r[k] = ""
                # Conditional prop_address eviction. When the record's
                # prop_address matches the NCAD search cache's site_addr
                # for the same owner, it means prop_address was sourced
                # from esearch (the PDF parser couldn't extract it).
                # Since we're evicting the NCAD link as a wrong match,
                # the address attached from that wrong parcel is also
                # wrong and must be cleared.
                rec_addr = _normalize_addr(r.get("prop_address") or "")
                cached_addr = _normalize_addr(
                    _ncad_cache_addr_for_owner(owner_name))
                if rec_addr and cached_addr and rec_addr == cached_addr:
                    for k in PROP_ADDR_FIELDS:
                        if k in r:
                            r[k] = ""
                    prop_addr_also_evicted.append(r.get("doc_num"))
                    log.info("  also cleared prop_address on %s "
                             "(was '%s' — matched evicted NCAD parcel)",
                             r.get("doc_num"), r.get("prop_address") or "")
        if prop_addr_also_evicted:
            log.info("APPLIED: also cleared prop_address on %d record(s) "
                     "(came from now-evicted NCAD parcel)",
                     len(prop_addr_also_evicted))
        DASH_JSON.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        if DATA_JSON.exists():
            # Keep data/foreclosures.json in lockstep (the daily scrape
            # writes both). This is just the canonical copy under data/.
            # Mirror the conditional prop_address eviction here too —
            # use the same prop_addr_also_evicted set computed above so
            # both files end up identical.
            prop_evict_set = set(prop_addr_also_evicted)
            data_payload = json.loads(DATA_JSON.read_text(encoding="utf-8"))
            data_recs = data_payload.get("records") or []
            for r in data_recs:
                if r.get("doc_num") in evicted_docs:
                    for k in EVICT_FIELDS:
                        if k in r:
                            r[k] = None if k == "appraised_value" else ""
                    if r.get("doc_num") in prop_evict_set:
                        for k in PROP_ADDR_FIELDS:
                            if k in r:
                                r[k] = ""
            DATA_JSON.write_text(
                json.dumps(data_payload, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        log.info("APPLIED: evicted NCAD link on %d record(s)",
                  len(evicted_docs))

        # Expire the NCAD-search-cache entries for the evicted records.
        # This is the structural fix for the cache/eviction misalignment:
        # without it, the next daily scrape would just reattach the same
        # wrong parcels via cache shortcuts. With it, the next scrape
        # re-runs full NCAD lookup with current variant logic.
        cache_expiry_stats = _expire_ncad_cache_entries(evicted_owner_names)

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
        "unique_parcels_fetched": len(url_cache),
        "cache_hits": cache_hits,
        "ncad_cache_expiry": cache_expiry_stats,
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
    log.info("unique parcels: %d  (cache reused %d times)",
              len(url_cache), cache_hits)
    log.info("log written to %s", LOG_JSON)
    if not APPLY_MODE and mismatches:
        log.info("DRY RUN — no JSON files changed. "
                 "Set RECORROBORATE_APPLY=1 to apply evictions.")
    return summary


if __name__ == "__main__":
    asyncio.run(_run())
