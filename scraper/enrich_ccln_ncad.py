"""enrich_ccln_ncad.py — backfill ncad_prop_id / ncad_owner_id /
correct ncad_account_num onto CCLN records by looking each property
up on NCAD's esearch by address.

Why this exists
---------------
CCLN records are seeded from clerk-of-court filings and OCR'd from the
city lien PDFs. The OCR captures an `ncad_account_num` off the lien
document, but:

  1. None of these records carry an `ncad_prop_id` — so the dashboard's
     NCAD ↗ direct-link cannot be built. Every CCLN row falls back to
     the "copy owner & open NCAD search" UX.
  2. ~79 of the OCR'd account numbers are shared across genuinely
     different addresses (e.g. SOLIZ at 1321 FLORIDA AVE and at 3025
     ELGIN both carry 2277-0001-0220). The lien-PDF OCR misread a
     digit, so the account string maps to the wrong property on the
     tax office detail page (NCTAX ↗).

This script fixes both at the data layer. For each distinct CCLN
property address it:
  - mints a NCAD `searchSessionToken`,
  - queries esearch by StreetNumber + StreetName,
  - picks the best row (single-result trust, multi-result legal
    corroboration via the same `legal_descriptions_match` we hardened
    for the Sanchez foreclosure case),
  - optionally pulls the detail page for the mailing address,
  - writes `ncad_prop_id` / `ncad_owner_id` / `ncad_year` /
    `ncad_account_num` (the *correct* one) and value fields back onto
    every CCLN record sharing that normalized address,
  - logs every account-number change to an audit JSON so the
    Soliz-class wrong-account replacements are auditable.

Mirrors the proven `enrich_tfc_ncad.py` pattern and reuses the shared
`.cache/ncad_search_cache.json` so foreclosures and CCLN don't repeat
each other's lookups. Settings come straight from the NCAD esearch
memory notes (1.5s inter-fetch, token refresh every 25, retry-on-empty,
multi-candidate StreetName fallback).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# We rely on the matcher we hardened for the Sanchez fix. It lives in
# the same scraper/ dir so an in-process import is fine.
try:
    from pdf_text_extractor import legal_descriptions_match
except ImportError:
    # Allow running from repo root for ad-hoc tests
    sys.path.insert(0, str(Path(__file__).parent))
    from pdf_text_extractor import legal_descriptions_match  # type: ignore

# Playwright is the only way to talk to esearch reliably — requests
# hits Cloudflare and gets 403s within the first few calls (proven via
# memory note re: bulk-ZIP CDN). Same approach as fetch.py + TFC.
from playwright.async_api import async_playwright, BrowserContext, Page

log = logging.getLogger("enrich_ccln_ncad")

# ─── Proven NCAD esearch settings (memory notes 12-14) ────────────
INTER_FETCH_DELAY_S = 1.5
RETRY_ON_EMPTY_WAIT_S = 3.0
TOKEN_REFRESH_EVERY = 25
TOKEN_REFRESH_AFTER_MISSES = 5
PAGE_TIMEOUT_MS = 20_000
RESULT_WAIT_TIMEOUT_MS = 8_000
DETAIL_TIMEOUT_MS = 15_000
SETTLE_MS = 400

ESEARCH_HOME = "https://esearch.nuecescad.net/"
ESEARCH_RESULT = "https://esearch.nuecescad.net/search/result"

# Default to the year the rest of the pipeline uses (matches fetch.py
# behavior). Could be made dynamic later; for now the bulk export
# year is "2026" so we mirror that.
DEFAULT_NCAD_YEAR = "2026"

# Repo paths — assume invocation from repo root or scraper/
REPO_ROOT = Path(__file__).resolve().parent.parent
CCLN_PATH_CANDIDATES = [
    REPO_ROOT / "data" / "city_liens.json",
    REPO_ROOT / "dashboard" / "city_liens.json",
]
DASHBOARD_CCLN = REPO_ROOT / "dashboard" / "city_liens.json"
DATA_CCLN = REPO_ROOT / "data" / "city_liens.json"
CACHE_PATH = REPO_ROOT / ".cache" / "ncad_search_cache.json"


# ─── Address normalization ─────────────────────────────────────────
# Mirror of the dashboard's `_cclnNormalizeAddrForGrouping` (used in
# index.html for super-grouping). MUST stay aligned with that function
# so the same liens that visually super-group also enrich together.
_RE_UNIT_SUFFIX = re.compile(
    r"\s+(UNIT|APT|SUITE|STE|#)\s*[\w\d-]+\b", re.I)
_RE_TRIM_PUNCT = re.compile(r"^[.,;\s]+|[.,;\s]+$")
_RE_SPACED_DASH_RIGHT = re.compile(r"(\d)\s+-\s*(\d)")
_RE_SPACED_DASH_LEFT = re.compile(r"(\d)\s*-\s+(\d)")
_RE_TRAILING_DIR = re.compile(r"^(\d+)\s+(.*?)\s+(N|S|E|W)$")
_RE_CROSSSTREET = re.compile(r"\s*@\s*.+$")


def normalize_addr_for_grouping(addr: str) -> str:
    """Python equivalent of dashboard's _cclnNormalizeAddrForGrouping.
    Used to dedupe liens at the same building down to one NCAD lookup.
    """
    if not addr:
        return ""
    s = str(addr).upper().strip()
    s = " ".join(s.split())
    s = _RE_UNIT_SUFFIX.sub("", s)
    s = _RE_TRIM_PUNCT.sub("", s)
    s = _RE_SPACED_DASH_RIGHT.sub(r"\1-\2", s)
    s = _RE_SPACED_DASH_LEFT.sub(r"\1-\2", s)
    m = _RE_TRAILING_DIR.match(s)
    if m:
        s = f"{m.group(1)} {m.group(3)} {m.group(2)}"
    s = _RE_CROSSSTREET.sub("", s)
    return s.strip()


# Street-type suffixes to strip from StreetName when querying NCAD.
# Critical — NCAD's StreetName field does NOT include the suffix.
_RE_STREET_SUFFIX = re.compile(
    r"\s+(AVE?|AVENUE|ST|STREET|DR|DRIVE|RD|ROAD|LN|LANE|BLVD|"
    r"BOULEVARD|CT|COURT|PL|PLACE|CIR|CIRCLE|WAY|TRL|TRAIL|"
    r"PKWY|PARKWAY|HWY|HIGHWAY|TER|TERRACE)\.?$", re.I)


def parse_address_for_query(addr: str) -> Optional[Tuple[str, str]]:
    """Split a clean property address into (StreetNumber, StreetName)
    suitable for NCAD esearch. Returns None if we can't extract a
    leading number + name.

    Handles:
      "1321 FLORIDA AVE"      → ("1321", "FLORIDA")
      "1116-1120 LEOPARD ST"  → ("1116", "LEOPARD")  (lead # on range)
      "2917 S PORT AVE"       → ("2917", "S PORT")
      "100 OLD ROBSTOWN RD"   → ("100", "OLD ROBSTOWN")
    """
    if not addr:
        return None
    s = normalize_addr_for_grouping(addr)
    if not s:
        return None
    # Strip a city/state/zip tail if any slipped through (NCAD wants
    # just the street portion).
    s = re.sub(r",\s*[A-Z\s]+(?:\s+[A-Z]{2})?\s+\d{5}.*$", "", s)
    m = re.match(r"^(\d+)(?:-\d+)?\s+(.+)$", s)
    if not m:
        return None
    num = m.group(1)
    name = m.group(2).strip()
    # Strip trailing street-type suffix
    name = _RE_STREET_SUFFIX.sub("", name).strip()
    if not name:
        return None
    return num, name


def street_name_fallbacks(name: str) -> List[str]:
    """Multi-candidate fallback list for the StreetName param. Proven
    pattern (memory note 14): try the full multi-word name first, then
    the longest single token, then the first token. Catches misindexed
    multi-word streets without hammering NCAD.
    """
    tokens = [t for t in name.split() if t]
    candidates = [name]
    if len(tokens) > 1:
        longest = max(tokens, key=len)
        if longest != name and longest not in candidates:
            candidates.append(longest)
        if tokens[0] != longest and tokens[0] not in candidates:
            candidates.append(tokens[0])
    return candidates


# ─── Data classes ───────────────────────────────────────────────────
@dataclass
class EsearchRow:
    """One result row from NCAD esearch result list."""
    prop_id: str = ""
    owner_id: str = ""
    owner_name: str = ""
    situs: str = ""
    legal: str = ""
    appraised: str = ""
    type_code: str = ""   # 'R' real, 'P' personal
    year: str = DEFAULT_NCAD_YEAR
    account_num: str = ""   # geo/account ID (Geo ID col on detail)


@dataclass
class EnrichmentResult:
    """What we learned (or didn't) about one property address."""
    address: str
    norm_address: str
    chosen: Optional[EsearchRow] = None
    mail_address: str = ""
    mail_city: str = ""
    mail_state: str = ""
    mail_zip: str = ""
    miss_reason: str = ""   # "no_results" / "no_corroboration" / "skipped" / ""
    affected_doc_nums: List[str] = field(default_factory=list)


# ─── Token + page management ────────────────────────────────────────
async def mint_token(page: Page) -> str:
    """Visit the esearch homepage and pull searchSessionToken from
    the <meta name="search-token"> tag. Without this, every query
    returns 0 rows (memory note 13).
    """
    await page.goto(ESEARCH_HOME, wait_until="domcontentloaded",
                    timeout=PAGE_TIMEOUT_MS)
    token = await page.evaluate(
        "() => document.querySelector('meta[name=\"search-token\"]')?.content || ''"
    )
    if not token:
        # Some refreshes need a brief delay before the meta lands
        await page.wait_for_timeout(SETTLE_MS)
        token = await page.evaluate(
            "() => document.querySelector('meta[name=\"search-token\"]')?.content || ''"
        )
    if not token:
        raise RuntimeError("Could not mint searchSessionToken from NCAD homepage")
    return token


async def run_query(page: Page, token: str, street_num: str,
                    street_name: str, year: str) -> List[EsearchRow]:
    """Run one esearch query and parse the result rows. Returns []
    when esearch shows the no-results panel (NOT for transport errors —
    those bubble as exceptions).
    """
    # NB: NCAD uses spaces inside the `keywords` value, not %20.
    # Playwright will URL-encode this for us, which is what the live
    # site accepts; proven by fetch.py.
    url = (
        f"{ESEARCH_RESULT}?keywords="
        f"StreetNumber:{street_num} StreetName:{street_name} Year:{year}"
        f"&searchSessionToken={token}"
    )
    await page.goto(url, wait_until="domcontentloaded",
                    timeout=PAGE_TIMEOUT_MS)
    # Wait for either a result row or the no-results panel.
    try:
        await page.wait_for_selector(
            "table tbody tr, [class*='no-results'], [class*='NoResults']",
            timeout=RESULT_WAIT_TIMEOUT_MS,
        )
    except Exception:
        return []
    await page.wait_for_timeout(SETTLE_MS)
    # Parse rows using BIS's stable CSS class names (memory note 16).
    rows = await page.evaluate("""
        () => {
          const out = [];
          const trs = document.querySelectorAll('table tbody tr');
          trs.forEach(tr => {
            const cell = (cls) => tr.querySelector('.' + cls)?.innerText?.trim() || '';
            const onclickStr = tr.getAttribute('onclick') || '';
            const ymatch = onclickStr.match(/redirectToPropertyDetails\\([^,]+,\\s*'?(\\d{4})'?/);
            out.push({
              prop_id:     cell('_propertyId'),
              owner_id:    cell('_ownerId'),
              owner_name:  cell('_ownerName'),
              situs:       cell('_address'),
              legal:       cell('_legalDescription'),
              appraised:   cell('_appraisedValueDisplay'),
              type_code:   cell('_propertyType'),
              year:        ymatch ? ymatch[1] : '',
            });
          });
          return out;
        }
    """)
    return [EsearchRow(**{**r, "year": r.get("year") or year}) for r in rows]


# ─── Mailing-address parsing (detail page) ──────────────────────────
async def fetch_mail_address(page: Page, row: EsearchRow
                              ) -> Tuple[str, str, str, str, str]:
    """Pull the Mailing Address (and Geo ID for `account_num`) from
    the property detail page. Returns (mail_address, city, state, zip,
    account_num). Empty strings on any failure.

    NCAD detail page Mailing Address cell uses <br> between street and
    city/state/zip; ET UX co-owner names are prepended as extra lines
    (memory note 17). We walk lines until we find one starting with a
    digit (the street) and take everything below as city/state/zip.
    """
    if not row.prop_id:
        return ("", "", "", "", "")
    detail_url = (
        f"{ESEARCH_HOME.rstrip('/')}/Property/View/{row.prop_id}"
        f"?year={row.year or DEFAULT_NCAD_YEAR}"
        + (f"&ownerId={row.owner_id}" if row.owner_id else "")
    )
    try:
        await page.goto(detail_url, wait_until="domcontentloaded",
                        timeout=DETAIL_TIMEOUT_MS)
    except Exception as exc:
        log.debug("detail nav failed for %s: %s", row.prop_id, exc)
        return ("", "", "", "", "")
    await page.wait_for_timeout(SETTLE_MS)
    info = await page.evaluate("""
        () => {
          const result = { mail_text: '', geo_id: '' };
          // Pattern C: scan label/value table rows. The Mailing Address
          // row is identified by the label cell text.
          const rows = document.querySelectorAll('tr, .row, .property-detail-row');
          for (const r of rows) {
            const label = (r.querySelector('th, .label, td:first-child')?.innerText || '').trim();
            const value = (r.querySelector('td:last-child, .value, td + td')?.innerText || '').trim();
            if (!label) continue;
            const lab = label.toLowerCase();
            if (lab.includes('mailing address') && !result.mail_text) {
              // Re-pull with newline separator so <br> becomes \\n
              const td = r.querySelector('td:last-child, .value, td + td');
              if (td) {
                const walker = document.createTreeWalker(td, NodeFilter.SHOW_TEXT, null);
                const parts = [];
                let n;
                while ((n = walker.nextNode())) {
                  const t = n.nodeValue.replace(/\\s+/g, ' ').trim();
                  if (t) parts.push(t);
                }
                result.mail_text = parts.join('\\n');
              }
            }
            if ((lab.includes('geographic id') || lab.includes('geo id'))
                && !result.geo_id) {
              result.geo_id = value;
            }
          }
          return result;
        }
    """)
    mail_text = info.get("mail_text", "")
    geo_id = info.get("geo_id", "")
    mail_address, mail_city, mail_state, mail_zip = parse_mail_lines(mail_text)
    return (mail_address, mail_city, mail_state, mail_zip, geo_id)


_RE_CSZ = re.compile(
    r"^(?P<city>.+?),?\s+(?P<state>[A-Z]{2})\s+(?P<zip>\d{5}(?:-\d{4})?)\s*$"
)


def parse_mail_lines(text: str) -> Tuple[str, str, str, str]:
    """Parse the multi-line Mailing Address cell. Returns
    (street, city, state, zip). Empty strings on failure.

    Three known formats (memory notes 9 + 17):
      Clean 2-line: "1701 PARK PLACE\\nCORPUS CHRISTI, TX 78404"
      ET UX prepended: "JANE DOE\\n1701 PARK PLACE\\nCORPUS CHRISTI, TX 78404"
      Legacy 1-line: "1701 PARK PLACE, CORPUS CHRISTI, TX 78404"
    """
    if not text:
        return ("", "", "", "")
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    if not lines:
        return ("", "", "", "")
    # If only one line, it might be the legacy comma-separated form.
    if len(lines) == 1:
        # "STREET, CITY, ST ZIP"
        parts = [p.strip() for p in lines[0].split(",")]
        if len(parts) >= 3:
            street = parts[0]
            csz = ", ".join(parts[1:])
            m = _RE_CSZ.match(csz.replace(",", " "))
            if m:
                return (street, m.group("city").strip(), m.group("state"),
                        m.group("zip"))
        return (lines[0], "", "", "")
    # Multi-line: walk forward to the first line that starts with a
    # digit (the street line). Anything before is co-owner names.
    street_idx = None
    for i, ln in enumerate(lines):
        if re.match(r"^\d", ln):
            street_idx = i
            break
    if street_idx is None:
        # No digit-starting line — give up gracefully, return first as
        # street and hope for the best.
        street_idx = 0
    street = lines[street_idx]
    after = lines[street_idx + 1:]
    if not after:
        return (street, "", "", "")
    csz_line = " ".join(after).strip()
    m = _RE_CSZ.match(csz_line.replace(",", " "))
    if m:
        return (street, m.group("city").strip(), m.group("state"),
                m.group("zip"))
    return (street, csz_line, "", "")


# ─── Picking the best result row ────────────────────────────────────
def pick_best_row(rows: List[EsearchRow], record_legal: str,
                  record_address: str) -> Tuple[Optional[EsearchRow], str]:
    """Choose the row that corresponds to `record_address`. Returns
    (row_or_None, miss_reason). When `record_legal` is provided, uses
    legal-description corroboration (the Sanchez-fix matcher) to
    disambiguate between multiple results.
    """
    if not rows:
        return None, "no_results"
    real = [r for r in rows if r.type_code == "R" or not r.type_code]
    candidates = real or rows
    if len(candidates) == 1:
        return candidates[0], ""
    # Multiple candidates — corroborate by legal.
    if record_legal:
        for cand in candidates:
            if not cand.legal:
                continue
            try:
                if legal_descriptions_match(
                    record_legal, cand.legal,
                    address_a=record_address, address_b=cand.situs,
                ):
                    return cand, ""
            except Exception as exc:
                log.debug("legal_match raised on cand %s: %s", cand.prop_id, exc)
        return None, "no_corroboration"
    # No legal to corroborate with — fall back to the first real result
    # whose situs starts with our address number. Conservative: only
    # accept if there's exactly one such match.
    if record_address:
        addr_match = [
            c for c in candidates
            if c.situs and c.situs.split()[0] == record_address.split()[0]
        ]
        if len(addr_match) == 1:
            return addr_match[0], ""
    return None, "no_corroboration"


# ─── Cache ──────────────────────────────────────────────────────────
def load_cache() -> Dict[str, Any]:
    try:
        with open(CACHE_PATH) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as exc:
        log.warning("cache load failed (%s) — starting fresh", exc)
        return {}


def save_cache(cache: Dict[str, Any]) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_PATH.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(cache, f, indent=2, sort_keys=True)
    tmp.replace(CACHE_PATH)


def cache_key_for_address(norm_addr: str) -> str:
    return f"ccln_addr::{norm_addr}"


# ─── Main enrichment loop ───────────────────────────────────────────
async def enrich(
    records: List[Dict[str, Any]],
    *,
    force: bool = False,
    fetch_mail: bool = True,
    limit: int = 0,
    dry_run: bool = False,
    retry_misses: bool = False,
    year: str = DEFAULT_NCAD_YEAR,
) -> Tuple[List[EnrichmentResult], Dict[str, Any]]:
    """Walk the CCLN records, dedupe by normalized address, look each
    one up on NCAD esearch, and return (results, cache). Records are
    NOT mutated here — that's the caller's job once it has the
    results back, so dry_run paths don't need a copy.
    """
    cache = load_cache()
    # Build the dedupe map: norm_addr → (rep_record, [doc_nums])
    by_addr: Dict[str, Tuple[Dict[str, Any], List[str]]] = {}
    for r in records:
        if not r.get("prop_address"):
            continue
        if not force and r.get("ncad_prop_id"):
            continue
        norm = normalize_addr_for_grouping(r["prop_address"])
        if not norm:
            continue
        if norm not in by_addr:
            by_addr[norm] = (r, [])
        else:
            # Prefer a representative with a non-empty legal description
            existing_rep = by_addr[norm][0]
            if not existing_rep.get("legal") and r.get("legal"):
                by_addr[norm] = (r, by_addr[norm][1])
        by_addr[norm][1].append(r.get("doc_num", ""))

    addrs = sorted(by_addr.keys())
    log.info("CCLN enrichment: %d records → %d distinct addresses",
             len(records), len(addrs))
    if limit > 0 and len(addrs) > limit:
        log.info("  limiting to first %d (use limit=0 for full run)", limit)
        addrs = addrs[:limit]

    results: List[EnrichmentResult] = []
    misses_streak = 0
    lookups_this_token = 0

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context: BrowserContext = await browser.new_context()
        page = await context.new_page()
        token = await mint_token(page)
        lookups_done = 0

        for i, norm in enumerate(addrs, 1):
            rep, doc_nums = by_addr[norm]
            raw_addr = rep["prop_address"]
            cache_key = cache_key_for_address(norm)
            cached = cache.get(cache_key)
            er = EnrichmentResult(
                address=raw_addr, norm_address=norm,
                affected_doc_nums=doc_nums,
            )

            # Cache hit handling (memory note re: ESEARCH_RETRY_MISSES)
            if cached is not None:
                if cached.get("miss") and not retry_misses:
                    er.miss_reason = cached.get("miss_reason", "cached_miss")
                    results.append(er)
                    continue
                if cached.get("miss") and retry_misses:
                    log.info("  retry_misses: re-attempting cached miss %r", norm)
                    # fall through to live lookup
                elif "row" in cached:
                    er.chosen = EsearchRow(**cached["row"])
                    er.mail_address = cached.get("mail_address", "")
                    er.mail_city = cached.get("mail_city", "")
                    er.mail_state = cached.get("mail_state", "")
                    er.mail_zip = cached.get("mail_zip", "")
                    results.append(er)
                    continue

            parsed = parse_address_for_query(raw_addr)
            if not parsed:
                er.miss_reason = "unparseable_address"
                cache[cache_key] = {"miss": True, "miss_reason": er.miss_reason}
                results.append(er)
                continue
            street_num, street_name = parsed

            # Token refresh — proactive and reactive
            if lookups_this_token >= TOKEN_REFRESH_EVERY \
                    or misses_streak >= TOKEN_REFRESH_AFTER_MISSES:
                log.info("  refreshing token (lookups_this_token=%d, "
                         "misses_streak=%d)",
                         lookups_this_token, misses_streak)
                token = await mint_token(page)
                lookups_this_token = 0
                misses_streak = 0

            # Multi-candidate StreetName fallback (memory note 14)
            chosen_row: Optional[EsearchRow] = None
            miss_reason = ""
            for candidate_name in street_name_fallbacks(street_name):
                rows = await run_query(page, token, street_num,
                                       candidate_name, year)
                if not rows:
                    # Retry-on-empty once before falling through
                    await asyncio.sleep(RETRY_ON_EMPTY_WAIT_S)
                    rows = await run_query(page, token, street_num,
                                           candidate_name, year)
                if rows:
                    chosen_row, miss_reason = pick_best_row(
                        rows, rep.get("legal", ""), raw_addr)
                    if chosen_row:
                        break
                # else: try next StreetName candidate
            lookups_done += 1
            lookups_this_token += 1

            if not chosen_row:
                er.miss_reason = miss_reason or "no_results"
                cache[cache_key] = {"miss": True, "miss_reason": er.miss_reason}
                misses_streak += 1
                log.info("  [%d/%d] %r → MISS (%s)",
                         i, len(addrs), norm, er.miss_reason)
                results.append(er)
                await asyncio.sleep(INTER_FETCH_DELAY_S)
                continue

            misses_streak = 0
            er.chosen = chosen_row

            # Mailing-address detail fetch — pull geo_id too which gives
            # us the canonical dashed account_num.
            if fetch_mail:
                await asyncio.sleep(INTER_FETCH_DELAY_S / 2)
                ma, mc, ms, mz, geo_id = await fetch_mail_address(page, chosen_row)
                er.mail_address = ma
                er.mail_city = mc
                er.mail_state = ms
                er.mail_zip = mz
                if geo_id:
                    chosen_row.account_num = geo_id
                lookups_this_token += 1

            cache[cache_key] = {
                "row": chosen_row.__dict__,
                "mail_address": er.mail_address,
                "mail_city": er.mail_city,
                "mail_state": er.mail_state,
                "mail_zip": er.mail_zip,
            }
            log.info("  [%d/%d] %r → prop_id=%s acct=%s (%d docs)",
                     i, len(addrs), norm,
                     chosen_row.prop_id, chosen_row.account_num,
                     len(doc_nums))
            results.append(er)

            await asyncio.sleep(INTER_FETCH_DELAY_S)

        await context.close()
        await browser.close()

    if not dry_run:
        save_cache(cache)
    return results, cache


# ─── Writeback ──────────────────────────────────────────────────────
def apply_results_to_records(records: List[Dict[str, Any]],
                              results: List[EnrichmentResult],
                              ) -> List[Dict[str, Any]]:
    """Apply enrichment back to records. Mutates `records` in place and
    returns a list of audit entries describing every account-number
    change made.
    """
    # Index results by normalized address
    by_norm: Dict[str, EnrichmentResult] = {r.norm_address: r for r in results}
    audit: List[Dict[str, Any]] = []
    for r in records:
        ad = r.get("prop_address")
        if not ad:
            continue
        norm = normalize_addr_for_grouping(ad)
        res = by_norm.get(norm)
        if not res or not res.chosen:
            continue
        row = res.chosen
        old_acct = r.get("ncad_account_num", "") or ""
        new_acct = row.account_num or old_acct
        if row.prop_id:
            r["ncad_prop_id"] = row.prop_id
        if row.owner_id:
            r["ncad_owner_id"] = row.owner_id
        if row.year:
            r["ncad_year"] = row.year
        if new_acct and new_acct != old_acct:
            audit.append({
                "doc_num": r.get("doc_num", ""),
                "prop_address": ad,
                "old_account": old_acct,
                "new_account": new_acct,
                "norm_address": norm,
            })
            r["ncad_account_num"] = new_acct
        elif new_acct and not old_acct:
            r["ncad_account_num"] = new_acct
        # Value fields — fill only if blank, don't overwrite user edits
        if row.appraised and not r.get("appraised_value"):
            r["appraised_value"] = row.appraised
        if res.mail_address and not r.get("mail_address"):
            r["mail_address"] = res.mail_address
        if res.mail_city and not r.get("mail_city"):
            r["mail_city"] = res.mail_city
        if res.mail_state and not r.get("mail_state"):
            r["mail_state"] = res.mail_state
        if res.mail_zip and not r.get("mail_zip"):
            r["mail_zip"] = res.mail_zip
    return audit


# ─── CLI / main ─────────────────────────────────────────────────────
def setup_logging() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def load_ccln() -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Load the CCLN store. Prefer data/ over dashboard/ (data/ is the
    canonical scraper output; dashboard/ is a copy)."""
    for p in CCLN_PATH_CANDIDATES:
        if p.exists():
            with open(p) as f:
                doc = json.load(f)
            return doc, doc.get("records", [])
    raise FileNotFoundError(
        f"city_liens.json not found in any of: {CCLN_PATH_CANDIDATES}"
    )


def save_ccln(doc: Dict[str, Any]) -> None:
    """Write to both dashboard/ and data/ so Pages and downstream
    tools stay in sync (same pattern as fetch.py)."""
    for p in (DATA_CCLN, DASHBOARD_CCLN):
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(doc, f, indent=2)
        tmp.replace(p)
        log.info("wrote %s", p)


async def main_async(args: argparse.Namespace) -> int:
    setup_logging()
    log.info("=== CCLN NCAD enrichment ===")
    log.info("  force=%s fetch_mail=%s limit=%s dry_run=%s retry_misses=%s",
             args.force, args.fetch_mail, args.limit, args.dry_run,
             args.retry_misses)

    doc, records = load_ccln()
    log.info("loaded %d CCLN records", len(records))

    start = time.time()
    results, _cache = await enrich(
        records,
        force=args.force,
        fetch_mail=args.fetch_mail,
        limit=args.limit,
        dry_run=args.dry_run,
        retry_misses=args.retry_misses,
    )
    elapsed = time.time() - start

    hits = sum(1 for r in results if r.chosen)
    misses = sum(1 for r in results if not r.chosen)
    log.info("enrichment done in %.1fs — %d hits / %d misses",
             elapsed, hits, misses)

    audit = apply_results_to_records(records, results)
    log.info("audit: %d account-number changes", len(audit))

    if args.dry_run:
        log.info("dry_run set — not writing JSON")
    else:
        doc["records"] = records
        save_ccln(doc)
        if audit:
            audit_path = (REPO_ROOT / "data" /
                          f"ccln_enrichment_audit_"
                          f"{time.strftime('%Y-%m-%d')}.json")
            audit_path.parent.mkdir(parents=True, exist_ok=True)
            with open(audit_path, "w") as f:
                json.dump({
                    "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                   time.gmtime()),
                    "total_changes": len(audit),
                    "changes": audit,
                }, f, indent=2)
            log.info("wrote audit %s", audit_path)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description="Backfill NCAD prop_id and correct account_num "
                    "onto CCLN records via esearch by address.")
    p.add_argument("--force", action="store_true",
                   help="Re-enrich records that already have ncad_prop_id")
    p.add_argument("--no-fetch-mail", dest="fetch_mail",
                   action="store_false", default=True,
                   help="Skip the per-property detail-page fetch "
                        "(twice as fast, but no mail_address writeback)")
    p.add_argument("--limit", type=int, default=0,
                   help="Cap addresses per run (for testing). 0 = unlimited.")
    p.add_argument("--dry-run", action="store_true",
                   help="Run lookups, log results, write nothing")
    p.add_argument("--retry-misses", action="store_true",
                   help="Re-attempt cached misses")
    # Allow env vars to override (so the workflow can use simple
    # boolean inputs without --flag shenanigans)
    args = p.parse_args()
    if os.environ.get("CCLN_ENRICH_FORCE", "").lower() in ("1", "true", "yes"):
        args.force = True
    if os.environ.get("CCLN_ENRICH_NO_MAIL", "").lower() in ("1", "true", "yes"):
        args.fetch_mail = False
    if os.environ.get("CCLN_ENRICH_DRY_RUN", "").lower() in ("1", "true", "yes"):
        args.dry_run = True
    if os.environ.get("CCLN_ENRICH_RETRY_MISSES", "").lower() in ("1", "true", "yes"):
        args.retry_misses = True
    env_limit = os.environ.get("CCLN_ENRICH_LIMIT", "").strip()
    if env_limit.isdigit():
        args.limit = int(env_limit)
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
