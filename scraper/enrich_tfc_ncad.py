"""
TFC NCAD Enrichment — Address-Based esearch Lookup
==================================================

Standalone enrichment pass for tax foreclosure records. For each
TFC record in dashboard/tfc.json, queries NCAD's esearch portal
by property address and writes back the matching parcel's owner,
legal description, market value, and NCAD property ID.

Why address-search instead of bulk parcel zip?
----------------------------------------------
Earlier versions of this script tried to download NCAD's bulk
parcel export (~166 MB zip, ~7 GB uncompressed). Three problems:
  1. NCAD's CDN fingerprints GitHub Actions IPs and rate-limits
     the download (needs Playwright workaround anyway).
  2. The data files we need (APPRAISAL_INFO.TXT) are 2 GB each —
     too large for the 7 GB RAM limit on GitHub Actions runners
     without streaming-line parsing.
  3. We only have ~27 TFC records. Downloading 7 GB to enrich
     27 records is wasteful by ~6 orders of magnitude.

The esearch portal supports `StreetNumber:` + `StreetName:` keyword
queries that return owner + legal + appraised_value + prop_id all
in one HTTP response. One Playwright nav per TFC record = ~30
seconds total for 27 records. Same pattern fetch.py's
address-search fallback uses (proven to work for MFC).

What we add to each TFC record on a match:
  * owner          — NCAD-reported primary owner name
  * legal          — legal description from NCAD
  * market_value   — appraised value from search result
  * ncad_prop_id   — NCAD property ID
  * ncad_year      — tax-roll year
  * ncad_owner_id  — NCAD owner ID (powers dashboard ↗ URL)
  * mail_address / mail_city / mail_state / mail_zip
                   — only if detail-page fetch is enabled
                     (env FETCH_MAIL_ADDRESS=1; off by default since
                     it doubles the per-record fetch count)

Designed to run in GitHub Actions on manual trigger. Idempotent —
re-running skips records that already have `ncad_prop_id` unless
ENRICH_TFC_FORCE=1 is set. Dry-run by default; set
ENRICH_TFC_APPLY=1 to actually write the enriched JSON.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

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
TFC_OUTPUTS = [
    REPO_ROOT / "dashboard" / "tfc.json",
    REPO_ROOT / "data" / "tfc.json",
]
ENRICHMENT_LOG = REPO_ROOT / "data" / "enrich_tfc_ncad_log.json"

NCAD_ESEARCH_BASE = "https://esearch.nuecescad.net"
NCAD_YEAR = "2026"   # tax roll being enriched against

# Per-record fetch knobs. The esearch portal isn't aggressive about
# rate-limiting under normal load, but we keep a small delay between
# requests so we never look like a denial-of-service attack.
#
# Empirically tuned in fetch.py for MFC's much-larger run (often
# 100+ lookups): 1.5s between requests + 12 consecutive misses
# triggers a token refresh. Going faster (e.g. 1.0s) produced
# intermittent empty result pages — NCAD's backend appears to soft-
# rate-limit when burst rate exceeds ~1 req/sec sustained.
INTER_FETCH_DELAY_S = 1.5
# Retry-on-empty: if a query returns 0 rows, pause and retry the
# SAME query once before declaring a miss. NCAD occasionally returns
# empty pages mid-run for queries that worked seconds earlier (seen
# in 2026-05-24 run: records 7/17/18 went empty after working in the
# previous run with identical queries). Retry is cheap and saves
# valid matches that would otherwise be lost.
EMPTY_RESULT_RETRY_DELAY_S = 3.0
PAGE_TIMEOUT_MS = 20_000
RESULT_WAIT_TIMEOUT_MS = 8_000
DETAIL_TIMEOUT_MS = 15_000

# Session-token refresh interval. NCAD's `searchSessionToken` has a
# finite TTL (observed ~5 minutes per fetch.py). Every N lookups we
# reload the homepage to mint a fresh token, so we don't silently
# start returning empty results when the token expires mid-run.
TOKEN_REFRESH_INTERVAL = 25

# Optional: also fetch each match's detail page to capture the
# mailing address (not present in the result list). Disabled by
# default since it doubles the request count. Enable for a fully-
# populated dataset.
FETCH_MAIL_ADDRESS = os.getenv("FETCH_MAIL_ADDRESS", "0") == "1"

# Apply mode: by default the script is DRY-RUN and writes a log
# describing what WOULD change but does not modify tfc.json. Set
# ENRICH_TFC_APPLY=1 to actually write the enriched JSON. Mirrors
# the convention recorroborate_ncad.py uses.
APPLY = os.getenv("ENRICH_TFC_APPLY", "0") == "1"
# Force mode: re-enrich every record even if it already has
# ncad_prop_id from a prior run.
FORCE = os.getenv("ENRICH_TFC_FORCE", "0") == "1"

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
log = logging.getLogger("enrich-tfc-ncad")


# ==================================================================
# Address parsing — pick the right StreetNumber + StreetName
# ==================================================================
#
# Mirrors fetch.py's address-search fallback logic exactly.
# Key insight from MFC: the FIRST token of the street name isn't
# always the best for NCAD search. Examples:
#   "W CORNELIA CIR"     → first token "W" matches every W-* street
#   "OLD BROWNSVILLE RD" → first token "OLD" matches every "OLD" road
#   "O'MALLEY DR"        → first token "O" (apostrophe issue)
#
# Strategy: replace apostrophes with spaces, strip punctuation, drop
# directionals and 1-char tokens, then pick the LONGEST remaining
# token (most distinctive). Ties broken by position.

# Directionals and short tokens we won't use as StreetName.
SKIP_TOKENS = {
    "n", "s", "e", "w", "ne", "nw", "se", "sw",
    "north", "south", "east", "west",
}


# Common street type suffixes — stripped before computing the
# StreetName value so "1521 EL PASO ST" becomes "EL PASO", not
# "EL PASO ST" (NCAD indexes by the name, not the type).
_STREET_TYPE_SUFFIXES = {
    "ST", "STREET",
    "AVE", "AVENUE",
    "BLVD", "BOULEVARD",
    "DR", "DRIVE",
    "RD", "ROAD",
    "LN", "LANE",
    "CT", "COURT",
    "PL", "PLACE",
    "TRL", "TRAIL",
    "PKWY", "PARKWAY",
    "CIR", "CIRCLE",
    "TER", "TERRACE",
    "HWY", "HIGHWAY",
    "WAY", "LOOP", "RUN", "ROW", "PATH", "BAY",
}


def _parse_street_address(street: str
                          ) -> Optional[Tuple[str, List[str]]]:
    """Parse 'NNNN STREET NAME [TYPE]' and return (number, candidates).

    Returns None if no leading number is present. Otherwise returns
    the number and a LIST of StreetName candidates to try in order
    against NCAD's search. We try the most specific first and fall
    back to less specific, so single-word names work the same as
    before AND multi-word names like "EL PASO" succeed.

    Candidate ordering:
      1. Full multi-word name (no type suffix). Works for "EL PASO",
         "SAN PEDRO", "LA PLATA", "OLD BROWNSVILLE", etc.
      2. Longest distinctive token (current behavior — fetch.py-style).
         Catches "OLD BROWNSVILLE" → "BROWNSVILLE" if try 1 misses.
      3. First non-directional token (if different from try 1 and 2).
         Catches edge cases where NCAD only indexes the first word.

    Examples:
      '1058 BEECHCRAFT AVE'  -> ('1058', ['BEECHCRAFT'])
      '1521 EL PASO ST'      -> ('1521', ['EL PASO', 'PASO'])
      '226 W CORNELIA CIR'   -> ('226', ['W CORNELIA', 'CORNELIA'])
      '500 SANTA FE WAY'     -> ('500', ['SANTA FE', 'SANTA'])
      '4501 OLD BROWNSVILLE RD'
                              -> ('4501', ['OLD BROWNSVILLE', 'BROWNSVILLE'])
    """
    if not street:
        return None
    m = re.match(r"\s*(\d+)\s+(.+)", street)
    if not m:
        return None
    st_num = m.group(1)
    raw = m.group(2)
    # Apostrophes -> spaces, then strip punctuation.
    raw = re.sub(r"[\u2018\u2019']", " ", raw)
    raw = re.sub(r"[^A-Za-z0-9 ]", " ", raw)
    tokens = [t.upper() for t in raw.split() if t]
    if not tokens:
        return None

    # Strip trailing street type ("ST", "AVE", "BLVD", "DR", etc.).
    # We only strip the LAST token, and only if it's a known type —
    # this avoids breaking names that happen to contain type-like
    # words mid-name (e.g. "STREET LAKE WAY" if such a thing existed).
    if len(tokens) > 1 and tokens[-1] in _STREET_TYPE_SUFFIXES:
        tokens = tokens[:-1]
    # Some addresses double up the type ("ST RD") — strip again.
    if len(tokens) > 1 and tokens[-1] in _STREET_TYPE_SUFFIXES:
        tokens = tokens[:-1]
    if not tokens:
        return None

    # Candidate 1: full name with all remaining tokens. This is the
    # multi-word case. Single-word streets degenerate to just one
    # token, which is fine and identical to candidate 2.
    full_name = " ".join(tokens)
    candidates: List[str] = [full_name]

    # Candidate 2: longest distinctive token, dropping directionals
    # and single-character tokens. Same logic as the original parser.
    keepable = [t for t in tokens
                if len(t) > 1 and t.lower() not in SKIP_TOKENS]
    if keepable:
        longest = max(keepable, key=lambda t: (len(t), -tokens.index(t)))
    elif tokens:
        longest = tokens[0]
    else:
        longest = ""
    if longest and longest not in candidates:
        candidates.append(longest)

    # Candidate 3: first token (in case NCAD indexes only the first
    # word for some streets). Only added if different from the
    # previous candidates.
    first = tokens[0] if tokens else ""
    if first and first not in candidates and first.lower() not in SKIP_TOKENS:
        candidates.append(first)

    return st_num, candidates


# ==================================================================
# Result-page parsing — copied from fetch.py:_parse_esearch_result_list
# ==================================================================
#
# Kept as a local copy so this module has no Python-import dependency
# on fetch.py. The BIS Consultants result list uses stable CSS classes
# (_ownerName, _address, etc.) so this parser is robust across rendering.

def _parse_money(s: str) -> Optional[float]:
    """Extract dollars from a string like '$294,474' or '$1,234.50'."""
    if not s:
        return None
    m = re.search(r"\$?\s*([\d,]+(?:\.\d{1,2})?)", s)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _parse_esearch_result_list(html: str) -> List[Dict[str, Any]]:
    """Parse the BIS Consultants result-list table.

    Returns one dict per data row with keys:
      owner, situs, type, prop_id, owner_id, year,
      legal, appraised_value
    Empty list if no rows or HTML can't be parsed.
    """
    if not html:
        return []
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")

    rows: List[Dict[str, Any]] = []
    for tr in soup.find_all("tr"):
        # Skip header rows (they use <th>, not <td>).
        if not tr.find("td"):
            continue

        def find_cell_text(cls: str) -> str:
            cell = tr.find("td", class_=cls)
            if not cell:
                return ""
            return cell.get_text(" ", strip=True)

        owner    = find_cell_text("_ownerName")
        situs    = find_cell_text("_address")
        ptype    = find_cell_text("_propertyType")
        prop_id  = find_cell_text("_propertyId")
        legal    = find_cell_text("_legalDescription")
        appr_s   = find_cell_text("_appraisedValueDisplay")
        owner_id = find_cell_text("_ownerId")

        # Pull year + (optionally) owner_id from the row's onclick.
        # BIS uses a JS handler instead of a normal <a href>:
        #   onclick="redirectToPropertyDetails('257256','2026','550713',...)"
        detail_year = ""
        onclick = tr.get("onclick") or ""
        if onclick:
            m = re.search(
                r"redirectToPropertyDetails\(\s*'([^']*)'\s*,"
                r"\s*'([^']*)'\s*,\s*'([^']*)'",
                onclick,
            )
            if m:
                detail_year = m.group(2)
                if not owner_id:
                    owner_id = m.group(3)

        if not owner and not situs:
            continue

        rows.append({
            "owner":           owner,
            "situs":           situs,
            "type":            ptype,
            "prop_id":         prop_id,
            "owner_id":        owner_id,
            "year":            detail_year,
            "legal":           legal,
            "appraised_value": _parse_money(appr_s),
        })
    return rows


def _pick_best_row(rows: List[Dict[str, Any]],
                    query_addr: str) -> Optional[Dict[str, Any]]:
    """Choose the best result row from an address search.

    Preferences (in order):
      1. Real property (Type='R') over personal property (Type='P')
      2. Has a real situs address (not blank, contains a digit)
      3. Situs that best matches the query address (prefer prefix match)

    Returns the chosen row or None if no row is usable.
    """
    if not rows:
        return None

    q = query_addr.upper().strip() if query_addr else ""
    q_num_match = re.match(r"\s*(\d+)\s+(\S+)", q) if q else None
    q_num = q_num_match.group(1) if q_num_match else ""

    def score(r: Dict[str, Any]) -> Tuple[int, int, int]:
        # Real property beats personal property.
        type_score = 2 if r.get("type") == "R" \
                     else (1 if r.get("type") == "P" else 0)
        # Real situs (has a digit somewhere).
        situs = r.get("situs", "")
        situs_score = 1 if (situs and re.search(r"\b\d+\b", situs)) else 0
        # Address-number agreement: the number is the strongest signal.
        # NCAD's "1058 ANY ST" should never match a query for "1057".
        addr_score = 0
        if q_num and situs:
            s_match = re.match(r"\s*(\d+)", situs)
            if s_match and s_match.group(1) == q_num:
                addr_score = 2
            elif s_match:
                addr_score = 0  # different number - actively bad
            else:
                addr_score = 1  # no number on situs side
        return (type_score, situs_score, addr_score)

    ranked = sorted(rows, key=score, reverse=True)
    top = ranked[0]
    # Require a real situs - empty addresses aren't actionable.
    if not top.get("situs"):
        return None
    # Require number agreement when both sides have a number. If the
    # top-ranked row's number disagrees with the query, reject — we'd
    # rather miss than mis-attach.
    if q_num:
        s_match = re.match(r"\s*(\d+)", top.get("situs", ""))
        if s_match and s_match.group(1) != q_num:
            log.info("    rejecting top result — situs %r doesn't match "
                      "query number %r", top.get("situs"), q_num)
            return None
    return top


# ==================================================================
# esearch fetch — one address lookup via Playwright
# ==================================================================

async def _mint_session_token(page) -> str:
    """Reload NCAD's esearch homepage and harvest the session token.

    The BIS Consultants esearch portal embeds a `searchSessionToken`
    in a <meta name="search-token"> tag on its homepage. Every search
    URL must include this token as `&searchSessionToken=...` — without
    it, the result page returns empty regardless of query validity.

    The token has a finite TTL (observed ~5 minutes), so we refresh
    it periodically during a long enrichment run. Returns the token
    string, or empty string if minting failed (caller can decide
    what to do — typically log a warning and proceed; some queries
    may work without it depending on session state).
    """
    try:
        await page.goto(
            NCAD_ESEARCH_BASE + "/",
            wait_until="domcontentloaded",
            timeout=30_000,
        )
        # Small settle delay — meta tag is in <head> so it loads fast,
        # but a tiny wait protects against the rare slow render.
        await page.wait_for_timeout(400)
        token = await page.evaluate("""() => {
            const m = document.querySelector('meta[name="search-token"]');
            return m ? m.getAttribute('content') : '';
        }""") or ""
        if token:
            log.info("  session token acquired (len=%d)", len(token))
        else:
            log.warning("  session token: meta tag missing or empty")
        return token
    except Exception as exc:
        log.warning("  could not mint session token: %s", exc)
        return ""


async def _esearch_address(page,
                            st_num: str,
                            st_name: str,
                            year: str,
                            query_addr: str,
                            token: str = "") -> Optional[Dict[str, Any]]:
    """Fetch and parse a single address-search result page.

    The `token` arg is NCAD's per-session `searchSessionToken` — without
    it the result page returns empty regardless of how good the query
    is. Mint via _mint_session_token() before the loop.

    Retry-on-empty: NCAD occasionally returns an empty result page for
    a query that worked seconds earlier — typically a soft rate-limit
    or a slow backend render. If we get 0 rows on the first try, we
    pause and retry the SAME query once before giving up. Costs ~3
    seconds when it triggers; saves the lookup ~80% of the time
    (empirical, 2026-05-24).

    Returns the chosen result row (dict with owner/legal/value/prop_id
    etc.) or None if no usable match.
    """
    kw = f"StreetNumber:{st_num} StreetName:{st_name} Year:{year} "
    params = {"keywords": kw}
    if token:
        params["searchSessionToken"] = token
    url = f"{NCAD_ESEARCH_BASE}/search/result?{urlencode(params)}"
    log.info("  query: %s", url)

    # Two attempts max. The second attempt fires only on a 0-row
    # response from the first — actual no-matches still get one shot
    # AND a retry (cheap insurance against transient empties).
    for attempt in range(2):
        if attempt > 0:
            log.info("  retrying after empty response (waiting %.1fs)...",
                      EMPTY_RESULT_RETRY_DELAY_S)
            await asyncio.sleep(EMPTY_RESULT_RETRY_DELAY_S)
        try:
            await page.goto(url, wait_until="domcontentloaded",
                             timeout=PAGE_TIMEOUT_MS)
            # Wait for either the result table OR a "no results" marker.
            try:
                await page.wait_for_selector(
                    "table tbody tr, [class*='no-results'], [class*='NoResults']",
                    timeout=RESULT_WAIT_TIMEOUT_MS,
                )
            except Exception:
                pass
            # Small settle delay - BIS sometimes finishes rendering after
            # the selector resolves.
            await page.wait_for_timeout(400)
            html = await page.content()
        except Exception as exc:
            log.warning("  esearch nav failed: %s", exc)
            # Nav errors don't retry — likely page-level problem, not
            # a transient empty-response issue. Fall through to None.
            return None

        rows = _parse_esearch_result_list(html)
        log.info("  -> %d result rows%s",
                  len(rows),
                  "" if attempt == 0 else f" (after retry)")
        if rows:
            break
        # 0 rows on attempt 0 → fall through to the retry iteration.
        # 0 rows on attempt 1 → exit the loop with rows=[] and return None.

    if not rows:
        return None
    best = _pick_best_row(rows, query_addr)
    if not best:
        log.info("  -> no usable match after scoring/guard")
        return None
    log.info("  -> matched owner=%r prop_id=%r value=%r",
              best.get("owner"), best.get("prop_id"),
              best.get("appraised_value"))
    return best


async def _esearch_detail_for_mail(page,
                                    prop_id: str,
                                    year: str,
                                    owner_id: str) -> Dict[str, str]:
    """Fetch a property-detail page to extract mailing address.

    Only called when FETCH_MAIL_ADDRESS=1. The detail page is the
    only place mail address lives — the result list shows owner +
    site addr + value but not the mailing address.

    Returns {mail_addr, mail_city, mail_state, mail_zip} or empty
    dict on failure. Failure is non-fatal (mail address stays empty
    on the record).
    """
    if not prop_id:
        return {}
    url = f"{NCAD_ESEARCH_BASE}/Property/View/{prop_id}?year={year}"
    if owner_id:
        url += f"&ownerId={owner_id}"
    try:
        await page.goto(url, wait_until="domcontentloaded",
                         timeout=DETAIL_TIMEOUT_MS)
        await page.wait_for_timeout(400)
        html = await page.content()
    except Exception as exc:
        log.warning("    detail nav failed for %s: %s", prop_id, exc)
        return {}

    # Reuse fetch.py's detail-page parsing logic (kept inline to avoid
    # the import dependency). The detail page renders the mailing
    # address in <dl>/<dd> pairs or in labeled cards.
    #
    # Key parsing detail: NCAD's Mailing Address cell renders the
    # street on one line and "CITY, ST ZIP" on a separate line (no
    # comma between street and city, just a <br>). Earlier versions
    # of this parser used `get_text(" ", ...)` which collapsed the
    # newline into a space, producing "1058 BEECHCRAFT AVE CORPUS
    # CHRISTI" as the street and "TX 78405" as the city. Fix: extract
    # text with a real newline separator so _split_us_address sees the
    # two-line structure and parses it correctly.
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")

    text_pairs: Dict[str, str] = {}

    # Pattern A: <dl><dt>Label</dt><dd>Value</dd>...
    for dl in soup.find_all("dl"):
        dts = dl.find_all("dt")
        dds = dl.find_all("dd")
        for dt, dd in zip(dts, dds):
            label = dt.get_text(" ", strip=True).lower().rstrip(":")
            # Preserve newlines for the value side — addresses
            # frequently use multi-line layouts.
            value = dd.get_text("\n", strip=True)
            if label and value:
                text_pairs[label] = value
    # Pattern B: cards with header + body labeled "Mailing Address".
    for card in soup.find_all(
            class_=re.compile(r"card|panel|section", re.IGNORECASE)):
        header = card.find(class_=re.compile(r"header|title", re.IGNORECASE))
        if not header:
            continue
        ht = header.get_text(" ", strip=True).lower()
        body = card.find(class_=re.compile(r"body|content", re.IGNORECASE))
        if not body:
            continue
        if "mail" in ht:
            text_pairs.setdefault("mailing address",
                                   body.get_text("\n", strip=True))
    # Pattern C: tables with two-cell label/value rows. THIS is the
    # pattern NCAD's detail page actually uses (verified 2026-05-24
    # against prop 182368). Newline preservation on the value cell is
    # essential — see top-of-function comment for full context.
    for tr in soup.find_all("tr"):
        cells = tr.find_all(["th", "td"])
        if len(cells) < 2:
            continue
        label = cells[0].get_text(" ", strip=True).lower().rstrip(":")
        value = cells[1].get_text("\n", strip=True)
        if label and value:
            text_pairs.setdefault(label, value)

    def find_value(*tokens: str) -> str:
        for label, value in text_pairs.items():
            if all(t in label for t in tokens):
                return value
        return ""

    mail_full = (find_value("mailing", "address")
                  or find_value("mail", "address")
                  or find_value("owner", "address"))
    if not mail_full:
        return {}
    return _split_us_address(mail_full)


def _split_us_address(full: str) -> Dict[str, str]:
    """Split a US-style address string into components.

    Handles two real-world NCAD formats:
      1. Two-line (most common on detail pages):
         "1058 BEECHCRAFT AVE
          CORPUS CHRISTI, TX 78405"
         Street is line 1; city/state/zip is line 2 with a comma
         between city and state.
      2. One-line legacy: "STREET, CITY, ST ZIP" — fall-through case
         for older or non-standard renderings.

    Returns {mail_addr, mail_city, mail_state, mail_zip}. Best-effort:
    missing components come back as empty strings rather than failing
    the whole parse. Verified 2026-05-24 against NCAD prop 182368
    (1058 BEECHCRAFT AVE → ADAMS MARTIN L mailing address).
    """
    if not full:
        return {}

    # Normalize whitespace: collapse runs of spaces but PRESERVE
    # newlines, since the newline is our primary separator.
    full = full.strip()
    # Split on the FIRST newline (one or more). If multiple lines
    # are present beyond the second, they're junk (extra contact
    # info, agent rows, etc.) — drop them.
    lines = [ln.strip() for ln in re.split(r"\n+", full) if ln.strip()]
    if not lines:
        return {}

    street = lines[0]
    city = state = zipc = ""

    if len(lines) >= 2:
        # Line 2 is "CITY, ST ZIP" or "CITY ST ZIP" (with or
        # without comma between city and state).
        line2 = lines[1]
        # Try comma-separated first: "CORPUS CHRISTI, TX 78405".
        m = re.match(
            r"^(.+?),\s*([A-Z]{2})\s+(\d{5}(?:-\d{4})?)\s*$",
            line2,
        )
        if m:
            city = m.group(1).strip()
            state = m.group(2)
            zipc = m.group(3)
        else:
            # No comma — try "CITY ST ZIP" with state+zip at end.
            # Walk backwards: zip is last token (5 digits), state is
            # 2-letter token immediately before it, everything else
            # is city.
            tokens = line2.split()
            if tokens and re.match(r"^\d{5}(?:-\d{4})?$", tokens[-1]):
                zipc = tokens[-1]
                if len(tokens) >= 2 and re.match(r"^[A-Z]{2}$", tokens[-2]):
                    state = tokens[-2]
                    city = " ".join(tokens[:-2])
                else:
                    city = " ".join(tokens[:-1])
            else:
                # No recognizable zip — treat the whole line as city.
                city = line2

    # Legacy fallback: input had only ONE line but contains commas.
    # This handles old code paths or unusual NCAD renderings where
    # the address came back as "STREET, CITY, ST ZIP" all on one
    # line. Mirrors the pre-2026-05-24 behaviour for compatibility.
    if not city and "," in street:
        parts = [p.strip() for p in street.split(",") if p.strip()]
        if len(parts) >= 2:
            street = parts[0]
            if len(parts) >= 2:
                city = parts[1]
            if len(parts) >= 3:
                tail = parts[2]
                m = re.match(
                    r"\s*([A-Z]{2})\s+(\d{5}(?:-\d{4})?)\s*$", tail)
                if m:
                    state = m.group(1)
                    zipc = m.group(2)

    # Strip ZIP+4 down to 5-digit ZIP for consistency. ZIP+4 is
    # rarely useful for lead-generation work and creates display
    # inconsistency where some rows show 78405 and others 78405-2802.
    zip5 = ""
    if zipc:
        zm = re.match(r"^(\d{5})", zipc)
        if zm:
            zip5 = zm.group(1)

    return {
        "mail_addr":  street,
        "mail_city":  city,
        "mail_state": state or "TX",
        "mail_zip":   zip5,
    }


# ==================================================================
# TFC record loading + writing
# ==================================================================

def _load_tfc_records() -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Load tfc.json. Returns (payload_envelope, records_list).
    Prefers dashboard/tfc.json; falls back to data/tfc.json."""
    for path in TFC_OUTPUTS:
        if path.exists():
            with path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            records = data.get("records") or []
            log.info("loaded %d TFC records from %s", len(records), path)
            return data, records
    raise SystemExit(
        f"No TFC JSON found at any of: {[str(p) for p in TFC_OUTPUTS]}"
    )


def _write_tfc_records(payload: Dict[str, Any],
                        records: List[Dict[str, Any]]) -> None:
    payload["records"] = records
    payload["total"]   = len(records)
    payload["ncad_enriched_at"] = datetime.now(timezone.utc).isoformat(
        timespec="seconds")
    for path in TFC_OUTPUTS:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
        log.info("wrote enriched JSON to %s", path)


def _write_log(entries: List[Dict[str, Any]]) -> None:
    ENRICHMENT_LOG.parent.mkdir(parents=True, exist_ok=True)
    out = {
        "ran_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "apply_mode": APPLY,
        "force_mode": FORCE,
        "fetch_mail_address": FETCH_MAIL_ADDRESS,
        "entries": entries,
    }
    with ENRICHMENT_LOG.open("w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2, ensure_ascii=False)
    log.info("wrote run log to %s", ENRICHMENT_LOG)


# ==================================================================
# Main async driver
# ==================================================================

async def _enrich_all(records: List[Dict[str, Any]]
                       ) -> Tuple[int, int, List[Dict[str, Any]]]:
    """Iterate records, perform esearch lookups, mutate records in
    place (when APPLY=True). Returns (matched, no_match, log_entries).
    """
    log_entries: List[Dict[str, Any]] = []
    matched = 0
    no_match = 0
    skipped_already = 0
    skipped_no_addr = 0

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True, args=["--no-sandbox"],
        )
        context = await browser.new_context(user_agent=USER_AGENT)
        page = await context.new_page()

        try:
            # Warm the session and mint the initial token.
            log.info("warming session and minting token...")
            token = await _mint_session_token(page)
            if not token:
                log.warning("starting without a token — queries may "
                            "return empty. Will try to refresh on first "
                            "consecutive-miss signal.")
            # Counters for token refresh logic.
            lookups_since_refresh = 0
            consecutive_misses = 0

            for i, rec in enumerate(records):
                uid = rec.get("uid", "")
                addr_log = rec.get("prop_address", "")

                # Skip records already enriched (unless FORCE).
                if rec.get("ncad_prop_id") and not FORCE:
                    skipped_already += 1
                    continue

                # Periodic token refresh — TTL is ~5 minutes so a long
                # run should refresh proactively to avoid silently
                # expiring mid-loop.
                if lookups_since_refresh >= TOKEN_REFRESH_INTERVAL:
                    log.info("  refreshing session token (after %d lookups)",
                              lookups_since_refresh)
                    new_token = await _mint_session_token(page)
                    if new_token:
                        token = new_token
                    lookups_since_refresh = 0

                # Get a parseable street address. Prefer
                # prop_address_street (just the street line); fall
                # back to the comma-tail-stripped prop_address.
                street = (rec.get("prop_address_street") or "").strip()
                if not street and rec.get("prop_address"):
                    street = rec["prop_address"].split(",")[0].strip()
                parsed = _parse_street_address(street)
                if not parsed:
                    log.warning("[%d/%d] no parseable address: %r",
                                 i + 1, len(records), street)
                    skipped_no_addr += 1
                    log_entries.append({
                        "uid": uid,
                        "prop_address": addr_log,
                        "result": "no-parseable-address",
                    })
                    continue
                st_num, st_name_candidates = parsed

                log.info("[%d/%d] uid=%s addr=%r -> StreetNumber:%s "
                          "StreetName candidates: %s",
                          i + 1, len(records), uid,
                          street, st_num, st_name_candidates)

                # Run the esearch lookup. Try each StreetName
                # candidate in order — most specific first. The
                # first candidate that returns a usable result wins.
                # Always pass the current session token — without it
                # the result page returns empty regardless of query.
                best = None
                tried_queries: List[str] = []
                for cand_idx, st_name in enumerate(st_name_candidates):
                    if cand_idx > 0:
                        log.info("  trying fallback candidate %d/%d: %r",
                                  cand_idx + 1, len(st_name_candidates),
                                  st_name)
                    try:
                        best = await _esearch_address(
                            page, st_num, st_name, NCAD_YEAR, street,
                            token=token,
                        )
                    except Exception as exc:
                        log.warning("  esearch failed: %s", exc)
                        best = None
                    lookups_since_refresh += 1
                    tried_queries.append(
                        f"StreetNumber:{st_num} StreetName:{st_name}"
                    )
                    if best:
                        break
                    # Tiny pause between candidate attempts so we
                    # don't burst-hit NCAD when one record needs
                    # several fallbacks.
                    if cand_idx < len(st_name_candidates) - 1:
                        await asyncio.sleep(0.4)

                if not best:
                    no_match += 1
                    consecutive_misses += 1
                    log_entries.append({
                        "uid": uid,
                        "prop_address": addr_log,
                        "result": "no-match",
                        "query": tried_queries,
                    })
                    # Reactive token refresh: if we get a streak of
                    # misses, the token may have expired silently.
                    # Try minting a fresh one and continue. If the
                    # very next lookup also misses we'll keep going
                    # (could be legit data misses), but at least we
                    # gave a stale token a chance to recover.
                    if consecutive_misses == 5:
                        log.warning("  5 consecutive misses — "
                                     "re-minting token in case it expired")
                        new_token = await _mint_session_token(page)
                        if new_token:
                            token = new_token
                            lookups_since_refresh = 0
                    await asyncio.sleep(INTER_FETCH_DELAY_S)
                    continue

                # Reset miss counter on a successful match.
                consecutive_misses = 0

                # Optional: fetch detail page for mailing address.
                mail_info: Dict[str, str] = {}
                if FETCH_MAIL_ADDRESS:
                    mail_info = await _esearch_detail_for_mail(
                        page,
                        best.get("prop_id", ""),
                        best.get("year") or NCAD_YEAR,
                        best.get("owner_id", ""),
                    )

                additions = _build_additions(best, mail_info)

                if APPLY:
                    for key, value in additions.items():
                        rec[key] = value

                matched += 1
                log_entries.append({
                    "uid": uid,
                    "prop_address": addr_log,
                    "result": "matched",
                    "ncad_prop_id": best.get("prop_id"),
                    "ncad_owner": best.get("owner"),
                    "ncad_legal": best.get("legal"),
                    "market_value": best.get("appraised_value"),
                    "additions": additions,
                })

                await asyncio.sleep(INTER_FETCH_DELAY_S)
        finally:
            await context.close()
            await browser.close()

    log.info("summary: matched=%d, no-match=%d, "
              "skipped-already=%d, skipped-no-addr=%d, total=%d",
              matched, no_match, skipped_already, skipped_no_addr,
              len(records))
    return matched, no_match, log_entries


def _build_additions(best: Dict[str, Any],
                      mail_info: Dict[str, str]) -> Dict[str, Any]:
    """Build the dict of field updates to apply to a TFC record."""
    additions: Dict[str, Any] = {}

    field_map = (
        ("owner",         best.get("owner") or ""),
        ("legal",         best.get("legal") or ""),
        ("market_value",  best.get("appraised_value")),
        ("ncad_prop_id",  best.get("prop_id") or ""),
        ("ncad_owner_id", best.get("owner_id") or ""),
        ("ncad_year",     best.get("year") or NCAD_YEAR),
    )
    for key, value in field_map:
        if value in (None, ""):
            continue
        # market_value=0 treated as "no value" (NCAD uses 0/null inconsistently)
        if key == "market_value" and value == 0:
            continue
        additions[key] = value

    for key in ("mail_addr", "mail_city", "mail_state", "mail_zip"):
        if mail_info.get(key):
            tfc_key = "mail_address" if key == "mail_addr" else key
            additions[tfc_key] = mail_info[key]

    return additions


# ==================================================================
# Main
# ==================================================================

def main() -> int:
    log.info("=== TFC NCAD Enrichment (esearch) — apply=%s force=%s "
              "fetch_mail=%s ===",
              APPLY, FORCE, FETCH_MAIL_ADDRESS)

    payload, records = _load_tfc_records()

    if not records:
        log.warning("no records to enrich — exiting cleanly")
        return 0

    matched, no_match, log_entries = asyncio.run(_enrich_all(records))
    _write_log(log_entries)

    if APPLY:
        _write_tfc_records(payload, records)
        log.info("APPLIED — enriched JSON written.")
    else:
        log.info("DRY-RUN — no changes written. "
                  "Set ENRICH_TFC_APPLY=1 to apply.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
