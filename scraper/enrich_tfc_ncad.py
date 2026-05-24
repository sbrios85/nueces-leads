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
INTER_FETCH_DELAY_S = 1.2
PAGE_TIMEOUT_MS = 20_000
RESULT_WAIT_TIMEOUT_MS = 8_000
DETAIL_TIMEOUT_MS = 15_000

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


def _parse_street_address(street: str) -> Optional[Tuple[str, str]]:
    """Parse 'NNNN STREET NAME [TYPE]' into (street_number, street_name).

    Returns None if the input doesn't have a leading number followed
    by at least one alphabetic word. The street type (AVE/DR/etc.)
    is dropped — NCAD's search doesn't use it.

    Examples (mirrors fetch.py's logic exactly):
      '1058 BEECHCRAFT AVE'    -> ('1058', 'BEECHCRAFT')
      'W CORNELIA CIR'         -> None (no number)
      '226 W CORNELIA CIR'     -> ('226', 'CORNELIA')  (W dropped)
      'OLD BROWNSVILLE RD'     -> None (no number)
      '4501 OLD BROWNSVILLE RD'-> ('4501', 'BROWNSVILLE') (longest)
      "4525 O'MALLEY DR"       -> ('4525', 'MALLEY')
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
    tokens = [t for t in raw.split() if t]
    # Filter out directionals and single-character tokens — too
    # generic to be selective for NCAD's keyword index.
    keepable = [t for t in tokens
                if len(t) > 1 and t.lower() not in SKIP_TOKENS]
    if keepable:
        # Longest token = most distinctive. Ties -> earliest position.
        st_name = max(keepable, key=lambda t: (len(t), -tokens.index(t)))
    elif tokens:
        st_name = tokens[0]
    else:
        return None
    return st_num, st_name.upper()


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

async def _esearch_address(page,
                            st_num: str,
                            st_name: str,
                            year: str,
                            query_addr: str) -> Optional[Dict[str, Any]]:
    """Fetch and parse a single address-search result page.

    Returns the chosen result row (dict with owner/legal/value/prop_id
    etc.) or None if no usable match.
    """
    kw = f"StreetNumber:{st_num} StreetName:{st_name} Year:{year} "
    url = f"{NCAD_ESEARCH_BASE}/search/result?{urlencode({'keywords': kw})}"
    log.info("  query: %s", url)
    try:
        await page.goto(url, wait_until="domcontentloaded",
                         timeout=PAGE_TIMEOUT_MS)
        # Wait for either the result table OR a "no results" marker.
        # The wait_for_selector pattern is the same as fetch.py.
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
        return None

    rows = _parse_esearch_result_list(html)
    log.info("  -> %d result rows", len(rows))
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
            value = dd.get_text(" ", strip=True)
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
                                   body.get_text(" \n", strip=True))
    # Pattern C: tables with two-cell label/value rows.
    for tr in soup.find_all("tr"):
        cells = tr.find_all(["th", "td"])
        if len(cells) < 2:
            continue
        label = cells[0].get_text(" ", strip=True).lower().rstrip(":")
        value = cells[1].get_text(" ", strip=True)
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
    """Split 'STREET, CITY, ST ZIP' into components. Best-effort —
    NCAD's address strings have several variations."""
    if not full:
        return {}
    parts = [p.strip() for p in full.split(",") if p.strip()]
    if not parts:
        return {}
    street = parts[0]
    city = state = zipc = ""
    if len(parts) >= 2:
        city = parts[1]
    if len(parts) >= 3:
        tail = parts[2]
        m = re.match(r"\s*([A-Z]{2})\s+(\d{5}(?:-\d{4})?)\s*$", tail)
        if m:
            state = m.group(1)
            zipc = m.group(2)
        else:
            tokens = tail.split()
            if tokens and re.match(r"\d{5}", tokens[-1]):
                zipc = tokens[-1]
                if len(tokens) >= 2 and len(tokens[-2]) == 2:
                    state = tokens[-2]
    return {
        "mail_addr":  street,
        "mail_city":  city,
        "mail_state": state or "TX",
        "mail_zip":   re.match(r"(\d{5})", zipc).group(1)
                       if zipc and re.match(r"(\d{5})", zipc) else "",
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
            for i, rec in enumerate(records):
                uid = rec.get("uid", "")
                addr_log = rec.get("prop_address", "")

                # Skip records already enriched (unless FORCE).
                if rec.get("ncad_prop_id") and not FORCE:
                    skipped_already += 1
                    continue

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
                st_num, st_name = parsed

                log.info("[%d/%d] uid=%s addr=%r -> StreetNumber:%s "
                          "StreetName:%s", i + 1, len(records), uid,
                          street, st_num, st_name)

                # Run the esearch lookup.
                try:
                    best = await _esearch_address(
                        page, st_num, st_name, NCAD_YEAR, street,
                    )
                except Exception as exc:
                    log.warning("  esearch failed: %s", exc)
                    best = None

                if not best:
                    no_match += 1
                    log_entries.append({
                        "uid": uid,
                        "prop_address": addr_log,
                        "result": "no-match",
                        "query": f"StreetNumber:{st_num} StreetName:{st_name}",
                    })
                    await asyncio.sleep(INTER_FETCH_DELAY_S)
                    continue

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
