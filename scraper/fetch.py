"""
Nueces County, TX — Motivated Seller Lead Scraper
==================================================

Pulls motivated-seller indicators (lis pendens, foreclosures, judgments, liens,
tax deeds, probate, etc.) recorded with the Nueces County Clerk in the past 7
days from https://nueces.tx.publicsearch.us/, then enriches each record with
mailing/site address data from the Nueces Central Appraisal District (NCAD)
bulk parcel export (https://nuecescad.net/downloads-reports/).

Outputs:
  - dashboard/records.json
  - data/records.json
  - data/leads_for_ghl.csv  (GoHighLevel-importable)

Designed to run in GitHub Actions on a daily cron.

Robustness rules:
  * Never crash on a single bad record — log and continue.
  * Retry every network call up to 3 times with exponential backoff.
  * If NCAD enrichment fails, still emit clerk records (with empty mail fields).
  * If the clerk portal yields zero results, emit an empty (but valid) JSON
    so the dashboard / downstream consumers don't break.

Author: generated for Corpus Christi / Nueces County motivated-seller workflow.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import os
import re
import sys
import time
import traceback
import zipfile
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote, urlencode

import requests
from bs4 import BeautifulSoup

# Playwright is imported lazily inside the async fetcher so the module can
# still be imported (e.g. for unit testing the scoring logic) on machines
# that don't have playwright + chromium installed.

# dbfread is optional — only used if a .dbf actually appears inside the
# NCAD bulk export. NCAD currently ships pipe-delimited text files, so the
# fall-through CSV/TXT parser is the hot path in practice.
try:
    from dbfread import DBF  # type: ignore
    _HAS_DBFREAD = True
except Exception:  # pragma: no cover
    _HAS_DBFREAD = False


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

ROOT_DIR = Path(__file__).resolve().parent.parent
DASHBOARD_DIR = ROOT_DIR / "dashboard"
DATA_DIR = ROOT_DIR / "data"
CACHE_DIR = ROOT_DIR / ".cache"

CLERK_BASE = "https://nueces.tx.publicsearch.us"
CLERK_RESULTS_API = f"{CLERK_BASE}/results"
CLERK_DOC_URL = f"{CLERK_BASE}/doc"

# NCAD ("Nueces Central Appraisal District") — official bulk parcel export.
# This is the Preliminary or Certified roll, distributed as a ZIP of flat
# pipe-delimited TXT files (Texas PTAD layout). The URL changes each year;
# we discover the most-recent one by parsing the downloads page.
NCAD_DOWNLOADS_PAGE = "https://nuecescad.net/downloads-reports/"

LOOKBACK_DAYS = 30   # Window covering recent filings. The clerk portal
                     # is typically 5-10 days behind real-time as records
                     # work through certification. 30 days gives us a
                     # comfortable buffer; "New this week" is still
                     # flagged in the scoring layer for fresh records.

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Full browser-like header set — required because NCAD's WordPress host
# sits behind a WAF that 403's on minimal/scripted-looking requests.
BROWSER_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
              "image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Ch-Ua": '"Chromium";v="124", "Not-A.Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

# Document-type taxonomy (what we search the clerk portal for).
# `query` is the search term we send to the Neumo portal — it matches
# both the doc-type code and the human-readable name. Multiple queries
# are run per category to maximize recall, then deduplicated by doc_num.
LEAD_CATEGORIES: List[Dict[str, Any]] = [
    {"cat": "LP",        "label": "Lis Pendens",
     "queries": ["LIS PENDENS", "LP"]},
    {"cat": "NOFC",      "label": "Notice of Foreclosure",
     "queries": ["NOTICE OF FORECLOSURE", "NOFC", "NOTICE OF SUBSTITUTE TRUSTEE SALE"]},
    {"cat": "TAXDEED",   "label": "Tax Deed",
     "queries": ["TAX DEED", "TAXDEED"]},
    {"cat": "JUD",       "label": "Judgment",
     "queries": ["JUDGMENT", "ABSTRACT OF JUDGMENT", "CCJ", "DRJUD"]},
    {"cat": "LNFED",     "label": "Federal / IRS / Corp Tax Lien",
     "queries": ["FEDERAL TAX LIEN", "IRS LIEN", "LNIRS", "LNFED", "LNCORPTX"]},
    {"cat": "LN",        "label": "Lien",
     "queries": ["LIEN", "MECHANICS LIEN", "LNMECH", "HOA LIEN", "LNHOA"]},
    {"cat": "MEDLN",     "label": "Medicaid Lien",
     "queries": ["MEDICAID LIEN", "MEDLN"]},
    {"cat": "PRO",       "label": "Probate",
     "queries": ["PROBATE", "LETTERS TESTAMENTARY", "AFFIDAVIT OF HEIRSHIP"]},
    {"cat": "NOC",       "label": "Notice of Commencement",
     "queries": ["NOTICE OF COMMENCEMENT", "NOC"]},
    {"cat": "RELLP",     "label": "Release of Lis Pendens",
     "queries": ["RELEASE OF LIS PENDENS", "RELLP"]},
]

# Map raw doc-type strings (from clerk results) to our category code.
DOC_TYPE_TO_CAT: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"\bRELEASE\b.*\bLIS\s*PENDENS\b", re.I),  "RELLP"),
    (re.compile(r"\bLIS\s*PENDENS\b", re.I),               "LP"),
    (re.compile(r"\bNOTICE\b.*\bFORECLOSURE\b", re.I),     "NOFC"),
    (re.compile(r"\bSUBSTITUTE\s*TRUSTEE\b", re.I),        "NOFC"),
    (re.compile(r"\bTAX\s*DEED\b", re.I),                  "TAXDEED"),
    (re.compile(r"\bMEDICAID\s*LIEN\b", re.I),             "MEDLN"),
    (re.compile(r"\bIRS\s*LIEN\b|\bFEDERAL\s*TAX\s*LIEN\b|\bLNIRS\b|\bLNFED\b|\bLNCORPTX\b", re.I),  "LNFED"),
    (re.compile(r"\bMECHANIC", re.I),                      "LN"),
    (re.compile(r"\bHOA\s*LIEN\b|\bLNHOA\b", re.I),        "LN"),
    (re.compile(r"\bLIEN\b", re.I),                        "LN"),
    (re.compile(r"\bABSTRACT\s*OF\s*JUDG", re.I),          "JUD"),
    (re.compile(r"\bJUDG", re.I),                          "JUD"),
    (re.compile(r"\bCCJ\b|\bDRJUD\b", re.I),               "JUD"),
    (re.compile(r"\bPROBATE\b|\bLETTERS\s*TESTAMENTARY\b|\bHEIRSHIP\b", re.I), "PRO"),
    (re.compile(r"\bNOTICE\s*OF\s*COMMENCEMENT\b|\bNOC\b", re.I),  "NOC"),
]

CAT_TO_LABEL = {c["cat"]: c["label"] for c in LEAD_CATEGORIES}

# Money regex — picks the largest-looking $-amount in a record.
_AMOUNT_RE = re.compile(r"\$?\s*([0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]{1,2})?|[0-9]+(?:\.[0-9]{1,2})?)")

# Configure logging.
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("nueces-leads")


# --------------------------------------------------------------------------- #
# Data classes
# --------------------------------------------------------------------------- #

@dataclass
class ClerkRecord:
    doc_num: str
    doc_type: str
    filed: str           # ISO date string YYYY-MM-DD
    cat: str
    cat_label: str
    owner: str           # grantor (likely seller)
    grantee: str
    amount: Optional[float]
    legal: str
    prop_address: str = ""
    prop_city: str = ""
    prop_state: str = "TX"
    prop_zip: str = ""
    mail_address: str = ""
    mail_city: str = ""
    mail_state: str = ""
    mail_zip: str = ""
    clerk_url: str = ""
    flags: List[str] = field(default_factory=list)
    score: int = 0


# --------------------------------------------------------------------------- #
# Retry helper
# --------------------------------------------------------------------------- #

def with_retries(fn, *args, attempts: int = 3, base_delay: float = 1.5, **kwargs):
    """Run `fn` up to `attempts` times with exponential backoff.

    Returns whatever `fn` returns on success, or re-raises the last exception.
    """
    last_exc: Optional[BaseException] = None
    for attempt in range(1, attempts + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - we deliberately catch all
            last_exc = exc
            wait = base_delay * (2 ** (attempt - 1))
            log.warning("attempt %d/%d failed for %s: %s (sleep %.1fs)",
                        attempt, attempts, getattr(fn, "__name__", str(fn)),
                        exc, wait)
            time.sleep(wait)
    assert last_exc is not None
    raise last_exc


async def with_retries_async(coro_factory, attempts: int = 3, base_delay: float = 1.5):
    """Async equivalent of `with_retries`. `coro_factory` is a zero-arg callable
    that returns a fresh coroutine each call (since coroutines are one-shot).
    """
    last_exc: Optional[BaseException] = None
    for attempt in range(1, attempts + 1):
        try:
            return await coro_factory()
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            wait = base_delay * (2 ** (attempt - 1))
            log.warning("async attempt %d/%d failed: %s (sleep %.1fs)",
                        attempt, attempts, exc, wait)
            await asyncio.sleep(wait)
    assert last_exc is not None
    raise last_exc


# --------------------------------------------------------------------------- #
# HTTP helpers — with Playwright fallback for WAF-protected sites
# --------------------------------------------------------------------------- #
#
# NCAD's WordPress site sits behind a WAF that 403s requests it doesn't think
# look "browser-like enough". The first attempt uses `requests` with a full
# Chrome header set (works in many environments). If that still 403s, we fall
# back to a real headless Chromium fetch.

def _http_get_text(url: str, timeout: int = 60) -> str:
    """GET → text. Tries requests first, then Playwright on 403/4xx."""
    try:
        resp = with_retries(
            requests.get, url,
            headers=BROWSER_HEADERS, timeout=timeout, attempts=2,
        )
        if resp.status_code == 200:
            return resp.text
        log.warning("requests got HTTP %d for %s, falling back to Playwright",
                    resp.status_code, url)
    except Exception as exc:
        log.warning("requests failed for %s: %s — falling back to Playwright",
                    url, exc)

    return asyncio.run(_pw_get_text(url, timeout=timeout))


def _http_get_bytes(url: str, timeout: int = 300) -> bytes:
    """GET → bytes. Same fallback strategy as `_http_get_text`."""
    try:
        resp = with_retries(
            requests.get, url,
            headers=BROWSER_HEADERS, timeout=timeout, stream=True, attempts=2,
        )
        if resp.status_code == 200:
            return resp.content
        log.warning("requests got HTTP %d for binary %s, falling back to Playwright",
                    resp.status_code, url)
    except Exception as exc:
        log.warning("requests failed for binary %s: %s — falling back to Playwright",
                    url, exc)

    return asyncio.run(_pw_get_bytes(url, timeout=timeout))


async def _pw_get_text(url: str, timeout: int = 60) -> str:
    from playwright.async_api import async_playwright  # type: ignore
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
        context = await browser.new_context(user_agent=USER_AGENT)
        page = await context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded",
                            timeout=timeout * 1000)
            html = await page.content()
            return html
        finally:
            await context.close()
            await browser.close()


async def _pw_get_bytes(url: str, timeout: int = 300) -> bytes:
    """Use Playwright's request context to fetch a binary URL with realistic
    TLS / browser fingerprint."""
    from playwright.async_api import async_playwright  # type: ignore
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
        context = await browser.new_context(user_agent=USER_AGENT)
        try:
            resp = await context.request.get(url, timeout=timeout * 1000)
            if resp.status != 200:
                raise RuntimeError(f"playwright HTTP {resp.status} for {url}")
            return await resp.body()
        finally:
            await context.close()
            await browser.close()


# --------------------------------------------------------------------------- #
# Clerk portal scraper (Playwright)
# --------------------------------------------------------------------------- #
#
# The Nueces clerk portal is a single-page React app powered by Neumo /
# PublicSearch. It loads results from an internal JSON endpoint (the exact
# path varies per deployment, but the response body is JSON containing the
# `searchResults` array). Rather than reverse-engineer the API surface and
# break on schema changes, we drive Chromium with Playwright, navigate to the
# advanced search URL, and *intercept* every JSON response that looks like a
# search-results payload. This is far more resilient than DOM scraping.
#
# Fallback: if zero JSON responses are intercepted (e.g. site changed), we
# still try to scrape rows out of the rendered DOM table.

ADVANCED_SEARCH_PATH = "/search/advanced"


def _build_clerk_search_url(query: str, start_iso: str, end_iso: str) -> str:
    """Build the deep-link URL for the advanced-search results page.

    Neumo's URL format (observed in the wild) is roughly:
        /results?department=RP&searchType=quickSearch&searchValue=<q>
                &recordedDateRange=YYYYMMDD,YYYYMMDD
    The exact parameter names have evolved over time; we send a superset
    so that whichever version the backend currently expects will pick up
    the right values.
    """
    start_compact = start_iso.replace("-", "")
    end_compact = end_iso.replace("-", "")
    params = {
        "department": "RP",                      # Real Property
        "limit": 50,
        "offset": 0,
        "recordedDateRange": f"{start_compact},{end_compact}",
        "searchOcr": "false",
        "searchType": "quickSearch",
        "searchValue": query,
    }
    return f"{CLERK_BASE}/results?{urlencode(params)}"


async def fetch_clerk_records(start_iso: str, end_iso: str) -> List[ClerkRecord]:
    """Drive the clerk portal once per category-query and aggregate results."""
    try:
        from playwright.async_api import async_playwright  # type: ignore
    except ImportError:
        log.error("playwright not installed; clerk portal will be skipped")
        return []

    seen: Dict[str, ClerkRecord] = {}
    debug_dir = ROOT_DIR / "debug"
    debug_dir.mkdir(parents=True, exist_ok=True)
    diagnostics_saved = False  # only save one snapshot, for the first query

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
        context = await browser.new_context(user_agent=USER_AGENT)
        page = await context.new_page()

        # Buffer for JSON responses caught on the wire.
        # Each entry: {"url": str, "body": Any, "ts": float}
        captured_payloads: List[Dict[str, Any]] = []

        async def on_response(resp):
            try:
                ctype = (resp.headers or {}).get("content-type", "")
                if "json" not in ctype.lower():
                    return
                url = resp.url
                # Anything that smells like a search/results/API endpoint.
                if not any(tok in url.lower() for tok in
                           ("/results", "/search", "/api/", "/document", "/record")):
                    return
                try:
                    body = await resp.json()
                except Exception:
                    return
                captured_payloads.append({
                    "url": url, "body": body, "ts": time.time(),
                })
            except Exception as exc:  # pragma: no cover
                log.debug("response handler error: %s", exc)

        page.on("response", lambda r: asyncio.create_task(on_response(r)))

        # Hard wall-clock budget — even with retries, we never spend more
        # than this on the whole clerk-portal phase. Workflow timeout is
        # set to 60 minutes; leaving a generous margin for NCAD download.
        deadline = time.time() + 25 * 60   # 25 minutes max for clerk

        for cat_def in LEAD_CATEGORIES:
            if time.time() > deadline:
                log.warning("clerk-portal time budget exhausted; stopping early")
                break
            category_found_any = False
            for q in cat_def["queries"]:
                if time.time() > deadline:
                    break
                # If this category already found rows under a previous query
                # alias, skip the remaining aliases — they're fallbacks, not
                # additive. Cuts ~50% of queries when the primary term works.
                if category_found_any:
                    log.info("  (skipping fallback q=%r — category already populated)", q)
                    break

                url = _build_clerk_search_url(q, start_iso, end_iso)
                log.info("clerk search: cat=%s q=%r", cat_def["cat"], q)
                captured_payloads.clear()
                t_nav_start = time.time()

                try:
                    async def _go():
                        await page.goto(url, wait_until="domcontentloaded",
                                        timeout=25_000)
                        # The portal renders results as a server-rendered
                        # HTML table. Wait for either: at least one row
                        # in tbody, OR a "No Results Found" indicator.
                        # The redux store stays `isLoading: true` even
                        # after the table is populated, so we don't trust
                        # it — we trust what we can see.
                        try:
                            await page.wait_for_function(
                                """() => {
                                    // Has results: tbody has at least one tr
                                    // with a non-empty col-7 (doc number) cell.
                                    const rows = document.querySelectorAll(
                                        'table tbody tr');
                                    for (const r of rows) {
                                        const docCell = r.querySelector('.col-7');
                                        if (docCell && docCell.textContent.trim())
                                            return true;
                                    }
                                    // Or: explicitly says no results.
                                    const txt = document.body.innerText || '';
                                    if (txt.includes('No Results Found') ||
                                        txt.includes('returned no results'))
                                        return true;
                                    return false;
                                }""",
                                timeout=15_000,
                            )
                        except Exception:
                            pass
                        await page.wait_for_timeout(400)
                    await _go()
                except Exception as exc:
                    log.error("nav failed for q=%r: %s", q, exc)
                    continue

                # Only consider payloads captured AFTER nav started — this
                # filters out caches/initial-page-load data from prior queries.
                fresh = [p for p in captured_payloads if p["ts"] >= t_nav_start]

                # PRIMARY SOURCE: extract rows from the rendered HTML table.
                # The Neumo portal server-renders search results into a
                # standard <table> with column-class markers (col-3 = grantor,
                # col-7 = doc number, etc.). The Redux state is unreliable
                # here — `isLoading` stays true even after the table renders.
                html = ""
                try:
                    html = await page.content()
                except Exception as exc:
                    log.warning("could not get page html: %s", exc)

                rows = _extract_clerk_table_rows(html)

                # Save diagnostics for the first query.
                if not diagnostics_saved:
                    try:
                        (debug_dir / "first_query.html").write_text(
                            html, encoding="utf-8")
                        with (debug_dir / "first_query_payloads.json").open(
                                "w", encoding="utf-8") as fh:
                            json.dump(
                                [{"url": p["url"],
                                  "preview": str(p["body"])[:2000]}
                                 for p in fresh],
                                fh, indent=2, default=str,
                            )
                        with (debug_dir / "first_query_table_rows.json").open(
                                "w", encoding="utf-8") as fh:
                            json.dump(rows[:5], fh, indent=2, default=str)
                        log.info(
                            "diagnostics saved (%d xhr payloads, %d table rows)",
                            len(fresh), len(rows),
                        )
                        diagnostics_saved = True
                    except Exception as exc:
                        log.debug("could not save diagnostics: %s", exc)

                # Fallback: redux state (covers any future portal versions
                # that hydrate the store correctly).
                if not rows:
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

                # Last fallback: legacy XHR-based and generic-DOM scrapers.
                if not rows:
                    rows = _extract_rows_from_payloads(fresh)
                    if not rows and html:
                        rows = _extract_rows_from_html(html)

                source_label = (
                    "table" if rows and "doc_number" in (rows[0] if rows else {})
                    else ("redux" if rows else "none")
                )
                log.info("  → %d raw rows (source=%s, q=%r)",
                         len(rows), source_label, q)

                kept = 0
                for raw in rows:
                    try:
                        rec = _normalize_clerk_row(raw, default_cat=cat_def["cat"])
                        if rec is None:
                            continue
                        # Date filter — but only if the row HAS a parseable
                        # filed date. Drop rows older than the window. Don't
                        # drop rows with empty `filed` (some payloads omit it).
                        if rec.filed:
                            if rec.filed < start_iso or rec.filed > end_iso:
                                continue
                        seen[rec.doc_num] = rec  # dedupe by doc_num
                        kept += 1
                    except Exception as exc:
                        log.warning("bad row skipped: %s", exc)
                        continue
                if rows and kept == 0:
                    log.info("    (all %d rows fell outside date window or "
                             "failed to normalize)", len(rows))
                if kept > 0:
                    category_found_any = True

        await context.close()
        await browser.close()

    log.info("clerk: %d unique docs in window %s..%s",
             len(seen), start_iso, end_iso)
    return list(seen.values())


def _extract_rows_from_payloads(payloads: List[Dict]) -> List[Dict]:
    """Walk every captured JSON body and yank anything that looks like a
    results array. Neumo wraps results under several different keys
    (`searchResults`, `results`, `documents`, `hits`) depending on version.

    A row qualifies only if it has at least one document-specific field —
    matching on a bare `id` was producing false positives where unrelated
    config/menu data was being grabbed.
    """
    # Strong identifiers that genuinely indicate "this is a recorded document".
    DOC_KEYS = {
        "docnumber", "documentnumber", "instrumentnumber", "doc_num",
        "doc#", "instnumber",
    }
    # Supporting fields — at least one of these must accompany an id-like key.
    DOC_HINTS = {
        "doctype", "documenttype", "doc_type", "type", "instrumenttype",
        "grantor", "grantee", "grantors", "grantees",
        "recordeddate", "fileddate", "filedate", "recordingdate",
        "consideration", "considerationamount", "legal", "legaldescription",
    }
    # Reject configuration-style entries — table column definitions, menu
    # items, etc. Real documents don't have a top-level 'label' field that
    # describes them; column configs do.
    CONFIG_TELLS = {"label", "key"}
    rows: List[Dict] = []
    for entry in payloads:
        body = entry.get("body")
        if body is None:
            continue
        for hits in _walk_for_lists(body):
            for item in hits:
                if not isinstance(item, dict):
                    continue
                keys = {k.lower() for k in item.keys()}
                # If it looks like a column/config descriptor, skip it.
                # Heuristic: small dict (≤3 keys) AND contains both a
                # 'key' and a 'label'/'name' but no doc-supporting fields.
                if (len(keys) <= 3
                    and "key" in keys
                    and (keys & {"label", "name", "title"})
                    and not (keys & DOC_HINTS)):
                    continue
                has_doc_key = bool(keys & DOC_KEYS)
                has_id = "id" in keys or "documentid" in keys
                has_hint = bool(keys & DOC_HINTS)
                # Accept if it has a doc-number-like key AND at least one
                # supporting field, OR if it has an id AND a hint.
                if (has_doc_key and has_hint) or (has_id and has_hint):
                    rows.append(item)
    return rows


def _walk_for_lists(obj: Any) -> Iterable[List]:
    """Yield every list-of-dicts found anywhere inside a JSON tree."""
    if isinstance(obj, list):
        if obj and all(isinstance(x, dict) for x in obj):
            yield obj
        for child in obj:
            yield from _walk_for_lists(child)
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _walk_for_lists(v)


def _extract_rows_from_html(html: str) -> List[Dict]:
    """Last-resort DOM scraper for the clerk results table."""
    rows: List[Dict] = []
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")

    # Pass A: the Neumo SPA hydrates a `window.__data` Redux blob into the
    # rendered HTML. Documents end up at:
    #     window.__data.documents.workspaces.<id>.data.byHash
    # That's the most authoritative source — read it directly.
    redux = _parse_window_data(html)
    if redux:
        try:
            ws = redux.get("documents", {}).get("workspaces", {}) or {}
            for ws_id, ws_data in ws.items():
                by_hash = (ws_data or {}).get("data", {}).get("byHash", {}) or {}
                for k, doc in by_hash.items():
                    if isinstance(doc, dict):
                        rows.append(doc)
                # byOrder is a list of IDs that point into byHash
        except Exception as exc:
            log.debug("redux state parse failed: %s", exc)
    if rows:
        return rows

    # Pass B: plain table fallback (used when SPA hasn't hydrated, e.g.
    # server-rendered preview pages). No regex-on-script-tags pass — that
    # was firing on table column-definition objects that happen to contain
    # `"docNumber"` as a string and producing junk rows.
    table = soup.find("table")
    if not table:
        return rows
    headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
    for tr in table.find_all("tr")[1:]:
        cells = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
        if not cells:
            continue
        rows.append(dict(zip(headers, cells)))
    return rows


def _parse_window_data(html: str) -> Optional[Dict]:
    """Extract and parse the `window.__data` Redux blob from an SPA page.

    Returns the parsed dict, or None if not found / unparseable. The blob
    is JS-literal (not strict JSON — it can contain `undefined`), so we
    do balanced-brace extraction and replace `undefined` with `null`.
    """
    m = re.search(r"window\.__data\s*=\s*\{", html)
    if not m:
        return None
    start = m.end() - 1
    depth = 0
    in_str = False
    quote = None
    esc = False
    end = -1
    for i in range(start, len(html)):
        c = html[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == quote:
                in_str = False
        else:
            if c in ('"', "'"):
                in_str = True
                quote = c
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
    if end < 0:
        return None
    raw = html[start:end]
    cleaned = re.sub(r":\s*undefined\b", ": null", raw)
    cleaned = re.sub(r"\bundefined\b", "null", cleaned)
    try:
        return json.loads(cleaned)
    except Exception:
        return None


def _extract_clerk_table_rows(html: str) -> List[Dict[str, str]]:
    """Extract document rows from the Neumo portal's rendered HTML table.

    The table uses class-based column markers — `col-3` through `col-11` —
    that map to specific fields:

        col-3  = Grantor
        col-4  = Grantee
        col-5  = Doc Type
        col-6  = Recorded Date
        col-7  = Doc Number
        col-8  = Book/Volume/Page
        col-9  = Legal Description
        col-10 = Lot
        col-11 = Block

    These class names are stable across Neumo deployments because they're
    used by the React SearchTable component for column-resize/sort behavior.
    """
    if not html:
        return []
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")

    table = soup.find("table")
    if not table:
        return []

    # Build a column-class → header-name map from <thead>. This is more
    # robust than hardcoding indexes — if Neumo reorders columns, we
    # still match correctly.
    header_by_col: Dict[str, str] = {}
    thead = table.find("thead")
    if thead:
        for th in thead.find_all("th"):
            classes = th.get("class") or []
            label = th.get_text(" ", strip=True)
            for c in classes:
                if c.startswith("col") and label:
                    header_by_col[c.replace("-", "")] = label
    # Fallback if headers aren't readable: hardcode known mappings.
    if len(header_by_col) < 5:
        header_by_col = {
            "col0": "", "col1": "", "col2": "",
            "col3": "Grantor", "col4": "Grantee", "col5": "Doc Type",
            "col6": "Recorded Date", "col7": "Doc Number",
            "col8": "Book/Volume/Page", "col9": "Legal Description",
            "col10": "Lot", "col11": "Block",
        }

    # Map the human-readable header names to our normalized field names.
    HEADER_TO_FIELD = {
        "grantor": "grantor",
        "grantors": "grantor",
        "grantee": "grantee",
        "grantees": "grantee",
        "doc type": "doc_type",
        "document type": "doc_type",
        "recorded date": "recorded_date",
        "filed date": "recorded_date",
        "doc number": "doc_number",
        "document number": "doc_number",
        "instrument number": "doc_number",
        "book/volume/page": "book_volume_page",
        "legal description": "legal",
        "lot": "lot",
        "block": "block",
    }

    rows: List[Dict[str, str]] = []
    tbody = table.find("tbody") or table
    for tr in tbody.find_all("tr"):
        # Skip the header row if it accidentally lives inside tbody.
        if tr.find("th") and not tr.find("td"):
            continue
        record: Dict[str, str] = {}
        link_href = ""
        for td in tr.find_all("td"):
            classes = td.get("class") or []
            text = td.get_text(" ", strip=True)
            if not text:
                continue
            # Capture any link to a /doc/<id> page for clerk_url.
            a = td.find("a", href=True)
            if a and "/doc/" in a["href"]:
                link_href = a["href"]
            # Find the col-N class.
            col_class = next(
                (c.replace("-", "") for c in classes if re.match(r"col-?\d+$", c)),
                None,
            )
            if not col_class:
                continue
            header = (header_by_col.get(col_class) or "").strip().lower()
            field = HEADER_TO_FIELD.get(header)
            if not field:
                continue
            # Collapse "--/--/--" placeholders to empty.
            if text in ("--/--/--", "N/A", "n/a", "-"):
                text = ""
            record[field] = text
        # Only keep rows that have at least a doc number.
        if record.get("doc_number"):
            if link_href:
                record["clerk_url"] = link_href
            rows.append(record)
    return rows


def _normalize_clerk_row(raw: Dict[str, Any], default_cat: str) -> Optional[ClerkRecord]:
    """Convert a raw clerk JSON/HTML row into a ClerkRecord.

    Tolerates wildly different schemas (camelCase, snake_case, lowercase, etc.).
    Returns None if the row is too malformed to be useful.
    """
    g = lambda *names: _first_present(raw, *names)

    doc_num = _stringy(g(
        "docNumber", "doc_number", "documentNumber", "instrumentNumber",
        "doc_num", "documentId", "id", "instrument number", "doc#", "doc #"
    ))
    if not doc_num:
        return None

    doc_type_raw = _stringy(g(
        "docType", "doc_type", "documentType", "type", "document type"
    ))

    filed = _coerce_date(g(
        "recordedDate", "recorded_date", "filedDate", "fileDate", "filed",
        "filedate", "recorded date", "recorded"
    ))

    grantor = _stringy(g(
        "grantor", "grantorName", "grantor_name", "grantor 1", "grantor1"
    ))
    grantee = _stringy(g(
        "grantee", "granteeName", "grantee_name", "grantee 1", "grantee1"
    ))

    # Some payloads expose grantors/grantees as a list of objects/strings.
    if not grantor:
        grantor = _join_names(g("grantors", "grantor_list"))
    if not grantee:
        grantee = _join_names(g("grantees", "grantee_list"))

    legal = _stringy(g(
        "legalDescription", "legal_description", "legal", "description"
    ))

    # Lot/block fields from the rendered table — append to legal if present.
    lot = _stringy(g("lot"))
    block = _stringy(g("block"))
    if lot or block:
        suffix_parts = []
        if lot:   suffix_parts.append(f"Lot {lot}")
        if block: suffix_parts.append(f"Block {block}")
        suffix = ", ".join(suffix_parts)
        legal = f"{legal} ({suffix})" if legal else suffix

    # Map to category code.
    cat = _classify(doc_type_raw) or default_cat
    cat_label = CAT_TO_LABEL.get(cat, default_cat)

    # OWNER SEMANTICS: for most documents (deeds, mortgages, lis pendens),
    # the grantor is the seller/property owner — that's who we want to
    # market to. But for adversarial documents (tax liens, judgments,
    # probate), the indexed-against party (defendant/debtor/decedent) is
    # actually the *grantee* — the party "receiving" the lien/judgment.
    # Examples from real Nueces data:
    #   - "USA INTERNAL REVENUE → JOHN DOE" (we want JOHN DOE)
    #   - "ABC BANK vs ROBERT SCHAFER" indexed grantor=ABC, grantee=ROBERT
    #   - "ESTATE OF JANE SMITH" → grantee is the heir
    # When that's the case, swap the names so `owner` always means
    # "person we want to contact".
    DEFENDANT_IS_GRANTEE_CATS = {"LNFED", "JUD", "MEDLN", "PRO", "LN"}
    grantor_looks_institutional = bool(
        grantor and re.search(
            r"\b(USA|UNITED\s*STATES|INTERNAL\s*REVENUE|IRS|STATE\s*OF\s*\w+|"
            r"COUNTY\s*OF|CITY\s*OF|DEPARTMENT\s*OF|"
            r"DISTRICT\s*COURT|COMPTROLLER|MEDICAID|MEDICARE|"
            r"BANK|CREDIT\s*UNION|MORTGAGE|FINANCIAL|HOA|"
            r"ASSOCIATION|HOMEOWNERS)\b",
            grantor, re.IGNORECASE,
        )
    )
    if cat in DEFENDANT_IS_GRANTEE_CATS and grantor_looks_institutional and grantee:
        grantor, grantee = grantee, grantor
    # Mechanic liens: contractor (grantor) files against property owner
    # (grantee). Same swap rule applies.
    if cat == "LN" and re.search(r"MECH", doc_type_raw, re.I) and grantee:
        grantor, grantee = grantee, grantor

    amount_raw = g("considerationAmount", "amount", "amount_due",
                   "totalAmount", "total")
    # Try the explicit amount field first; only fall through to legal/doctype
    # text if no explicit field was present. This avoids picking up doc
    # numbers, account numbers, etc. that look big but aren't money.
    amount = _coerce_amount(amount_raw)
    if amount is None:
        amount = _coerce_amount(legal)
    if amount is None:
        amount = _coerce_amount(doc_type_raw)

    # Build the deep-link URL back to the document detail page.
    clerk_url = ""
    # Clerk URL: prefer an explicit href captured from the row, then a doc-id
    # link, then fall back to a search URL by document number.
    clerk_url = _stringy(g("clerk_url", "url", "documentUrl"))
    if clerk_url and not clerk_url.startswith("http"):
        # Relative path like "/doc/abc" — make it absolute.
        clerk_url = CLERK_BASE + (clerk_url if clerk_url.startswith("/")
                                  else "/" + clerk_url)
    if not clerk_url:
        doc_id = _stringy(g("documentId", "id", "docId"))
        if doc_id:
            clerk_url = f"{CLERK_DOC_URL}/{doc_id}"
        else:
            clerk_url = (f"{CLERK_BASE}/results?"
                         + urlencode({"department": "RP",
                                      "searchValue": doc_num}))

    return ClerkRecord(
        doc_num=doc_num,
        doc_type=doc_type_raw or cat_label,
        filed=filed or "",
        cat=cat,
        cat_label=cat_label,
        owner=grantor,
        grantee=grantee,
        amount=amount,
        legal=legal,
        clerk_url=clerk_url,
    )


def _first_present(d: Dict[str, Any], *names: str) -> Any:
    """Case-insensitive multi-key getter."""
    if not isinstance(d, dict):
        return None
    lowered = {k.lower(): v for k, v in d.items()}
    for n in names:
        v = lowered.get(n.lower())
        if v not in (None, ""):
            return v
    return None


def _stringy(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        return ", ".join(_stringy(x) for x in v if x).strip(", ")
    if isinstance(v, dict):
        # Pull out something name-shaped.
        for k in ("name", "fullName", "displayName", "value"):
            if k in v:
                return _stringy(v[k])
        return ", ".join(f"{k}={_stringy(val)}" for k, val in v.items())
    return str(v)


def _join_names(v: Any) -> str:
    if not isinstance(v, list):
        return _stringy(v)
    parts: List[str] = []
    for item in v:
        s = _stringy(item)
        if s:
            parts.append(s)
    return "; ".join(parts)


def _coerce_date(v: Any) -> str:
    """Parse various date encodings → ISO YYYY-MM-DD; '' on failure."""
    if v is None or v == "":
        return ""
    if isinstance(v, (int, float)):
        # Epoch millis or seconds heuristic.
        try:
            ts = float(v)
            if ts > 1e11:  # millis
                ts /= 1000.0
            return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        except Exception:
            return ""
    s = str(v).strip()
    # Strip fractional seconds and Z.
    s = re.sub(r"\.\d+", "", s)
    s = s.replace("Z", "+00:00")
    fmts = [
        "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S%z",
        "%m/%d/%Y", "%m/%d/%y", "%Y%m%d",
    ]
    for fmt in fmts:
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    # Last-ditch: extract a YYYY-MM-DD substring.
    m = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", s)
    if m:
        try:
            y, mo, d = (int(x) for x in m.groups())
            return f"{y:04d}-{mo:02d}-{d:02d}"
        except Exception:
            pass
    return ""


def _coerce_amount(v: Any) -> Optional[float]:
    """Pull a dollar-amount out of an arbitrary value.

    Strict: requires an explicit money cue (`$`, decimal cents, "amount",
    "due", "consideration") near the number — otherwise we'll pick up
    doc numbers, account numbers, ZIP codes, cause numbers, etc. as
    "amounts" and produce ridiculous values.
    """
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        try:
            f = float(v)
            return f if f > 0 else None
        except Exception:
            return None
    s = str(v)
    candidates: List[float] = []

    # Pattern A: number prefixed by $ (with optional whitespace).
    for m in re.finditer(r"\$\s*([0-9]{1,3}(?:,[0-9]{3})+(?:\.\d{1,2})?|[0-9]+(?:\.\d{1,2})?)", s):
        try:
            candidates.append(float(m.group(1).replace(",", "")))
        except ValueError:
            pass

    # Pattern B: number with cents and thousands-separators (very money-shaped).
    # E.g. "12,345.67" — but NOT bare integers like "2026011900".
    for m in re.finditer(r"\b([0-9]{1,3}(?:,[0-9]{3})+\.[0-9]{2})\b", s):
        try:
            candidates.append(float(m.group(1).replace(",", "")))
        except ValueError:
            pass

    # Pattern C: number preceded by a money keyword (within ~20 chars).
    for m in re.finditer(
        r"(?:amount|amt|due|consideration|principal|balance|debt|paid|owed|sum|"
        r"total|judgment\s*for)\s*[:.]?\s*\$?\s*"
        r"([0-9]{1,3}(?:,[0-9]{3})+(?:\.\d{1,2})?|[0-9]+(?:\.\d{1,2})?)",
        s, re.IGNORECASE,
    ):
        try:
            candidates.append(float(m.group(1).replace(",", "")))
        except ValueError:
            pass

    if not candidates:
        return None
    # Return the largest plausible value. We've already filtered by money
    # cues, so this is safe — there's no way for a doc number to slip in.
    best = max(candidates)
    if best < 1.0:
        return None
    # Sanity cap: nothing in real-property documents is over $1B. Caps
    # protect against any remaining edge cases (e.g. concatenated numbers).
    if best > 1_000_000_000:
        return None
    return best


def _classify(doc_type_raw: str) -> Optional[str]:
    if not doc_type_raw:
        return None
    for pat, cat in DOC_TYPE_TO_CAT:
        if pat.search(doc_type_raw):
            return cat
    return None


# --------------------------------------------------------------------------- #
# Property Appraiser bulk parcel loader
# --------------------------------------------------------------------------- #

def fetch_ncad_parcels() -> Dict[str, Dict[str, str]]:
    """Download the latest NCAD bulk parcel export and build an
    owner-name → parcel-info lookup table.

    Returns: dict[normalized_owner_name] -> {
        site_addr, site_city, site_state, site_zip,
        mail_addr, mail_city, mail_state, mail_zip,
    }

    Each owner name is registered in three normalization variants
    ("FIRST LAST", "LAST FIRST", "LAST, FIRST") so a wide range of
    grantor strings from the clerk side will still hit a match.

    The NCAD export format is a ZIP of pipe-delimited TXT files (Texas
    PTAD layout). We also support DBF if the ZIP happens to include any
    .dbf files (some legacy exports do). Column names vary across years,
    so we resolve them by candidate-name search.
    """
    try:
        zip_url = _discover_ncad_export_url()
    except Exception as exc:
        log.error("could not discover NCAD export URL: %s", exc)
        return {}

    log.info("downloading NCAD bulk export: %s", zip_url)
    try:
        content = _http_get_bytes(zip_url, timeout=300)
        log.info("NCAD export: %d MB", len(content) // (1024 * 1024))
    except Exception as exc:
        log.error("NCAD download failed: %s", exc)
        return {}

    try:
        zf = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as exc:
        log.error("NCAD download is not a valid zip: %s", exc)
        return {}

    # Log everything in the ZIP — invaluable for diagnosing schema layout.
    all_names = zf.namelist()
    log.info("NCAD ZIP contents: %d files", len(all_names))
    for n in all_names:
        log.info("  • %s", n)

    # Texas PTAD layout typically splits owner data across multiple files,
    # joined by an account/property ID. Build the lookup by joining the
    # APPRAISAL_INFO file (which contains property addresses) with whichever
    # file actually contains owner names.
    lookup = _build_owner_lookup_from_zip(zf, all_names)
    log.info("NCAD owner-lookup: %d distinct name variants", len(lookup))
    return lookup


def _build_owner_lookup_from_zip(zf: zipfile.ZipFile,
                                  names: List[str]) -> Dict[str, Dict[str, str]]:
    """Parse the NCAD export and build owner_name_variant → parcel_info.

    Strategy: read every text file once, indexing rows by account/property
    ID. Files that have OWNER fields contribute owner data; files that have
    SITE/MAIL address fields contribute address data. We then join on the
    shared ID columns to produce a single row per parcel.

    Note: NCAD's "Public Export" historically omits owner names for
    privacy reasons, in which case this function will log the available
    schema (so the operator can see what's there) and return an empty
    lookup. The fall-back path in `enrich_with_parcels` will still
    pull addresses from the legal-description field of clerk records.
    """
    parse_deadline = time.time() + 4 * 60   # 4 minutes

    # Substrings (uppercased) that identify columns by their semantic role.
    # We match by substring rather than exact name because Texas PTAD column
    # naming varies (OWNER, OWN1, FILE_AS_NAME, PY_OWNER_NAME, etc.).
    ID_TOKENS    = ("PROP_ID", "PROPID", "PROPERTY_ID", "ACCOUNT_NUM",
                    "ACCT_NUM", "PARCEL_ID", "PARCELID", "GEO_ID", "QUICK_REF")
    OWNER_TOKENS = ("OWNER", "FILE_AS_NAME", "PY_OWNER")
    SITE_TOKENS  = ("SITUS", "SITE_ADDR", "PROP_ADDR", "STREET")
    MAIL_TOKENS  = ("MAIL_ADDR", "MAILING_ADDR", "ADDR_1", "ADDR1",
                    "ADDR_LINE", "MAIL_LINE")
    CITY_TOKENS  = ("CITY",)
    STATE_TOKENS = ("STATE",)
    ZIP_TOKENS   = ("ZIP", "POSTAL")

    def find_col(headers: List[str], tokens: tuple, exclude: tuple = ()) -> str:
        """Return the first header whose name contains any token (case-
        insensitive) and none of the excludes."""
        for h in headers:
            up = h.upper()
            if any(s in up for s in exclude):
                continue
            if any(t in up for t in tokens):
                return h
        return ""

    owner_by_id: Dict[str, str] = {}
    addr_by_id: Dict[str, Dict[str, str]] = {}

    text_files = [n for n in names
                  if n.lower().endswith((".txt", ".csv", ".tsv"))]
    log.info("NCAD: %d text files to scan", len(text_files))

    for name in text_files:
        if time.time() > parse_deadline:
            log.warning("NCAD parse budget exhausted at %s", name)
            break
        try:
            with zf.open(name) as fh:
                raw = fh.read()
            text = _decode_loose(raw)
            if not text.strip():
                continue
            delim = _sniff_delimiter(text)
            reader = csv.DictReader(io.StringIO(text), delimiter=delim)
            headers = [(h or "").strip() for h in (reader.fieldnames or [])]

            # Find which columns play which role.
            id_col    = find_col(headers, ID_TOKENS)
            owner_col = find_col(headers, OWNER_TOKENS)
            site_col  = find_col(headers, SITE_TOKENS, exclude=("CITY", "ZIP", "STATE"))
            mail_col  = find_col(headers, MAIL_TOKENS, exclude=("CITY", "ZIP", "STATE"))
            site_city  = find_col(headers, CITY_TOKENS) if site_col else ""
            site_state = find_col(headers, STATE_TOKENS)
            site_zip   = find_col(headers, ZIP_TOKENS)

            # Log a header sample so we can see schema in the log.
            log.info("  %s: cols=%d delim=%r", name, len(headers), delim)
            log.info("    headers (first 25): %s", headers[:25])
            log.info("    matched: id=%r owner=%r site=%r mail=%r",
                     id_col, owner_col, site_col, mail_col)

            row_count = 0
            owner_added = 0
            addr_added = 0
            for row in reader:
                row_count += 1
                clean = _clean_row(row)
                if not id_col:
                    continue
                pid = clean.get(id_col.upper(), "")
                if not pid:
                    continue
                if owner_col:
                    name_val = clean.get(owner_col.upper(), "")
                    if name_val and pid not in owner_by_id:
                        owner_by_id[pid] = name_val
                        owner_added += 1
                site_val = clean.get(site_col.upper(), "") if site_col else ""
                mail_val = clean.get(mail_col.upper(), "") if mail_col else ""
                if (site_val or mail_val) and pid not in addr_by_id:
                    addr_by_id[pid] = {
                        "site_addr": site_val,
                        "site_city": clean.get(site_city.upper(), "")
                                     if site_city else "",
                        "site_state": clean.get(site_state.upper(), "TX")
                                      if site_state else "TX",
                        "site_zip": clean.get(site_zip.upper(), "")
                                    if site_zip else "",
                        "mail_addr": mail_val,
                        "mail_city": clean.get(site_city.upper(), "")
                                     if site_city else "",
                        "mail_state": clean.get(site_state.upper(), "TX")
                                      if site_state else "TX",
                        "mail_zip": clean.get(site_zip.upper(), "")
                                    if site_zip else "",
                    }
                    addr_added += 1

            log.info("    %d rows (+%d owner, +%d addr)",
                     row_count, owner_added, addr_added)
        except Exception as exc:
            log.warning("text parse failed for %s: %s", name, exc)
            continue

    log.info("NCAD: %d unique owner records, %d unique address records",
             len(owner_by_id), len(addr_by_id))

    # Join owner ↔ address on the property ID.
    lookup: Dict[str, Dict[str, str]] = {}
    for pid, owner_name in owner_by_id.items():
        info = addr_by_id.get(pid)
        if not info:
            info = {"site_addr": "", "site_city": "", "site_state": "TX",
                    "site_zip": "", "mail_addr": "", "mail_city": "",
                    "mail_state": "", "mail_zip": ""}
        for variant in _owner_name_variants(owner_name):
            if variant and variant not in lookup:
                lookup[variant] = info
    return lookup


def _clean_row(row: Dict[str, Any]) -> Dict[str, str]:
    """Coerce a csv.DictReader row to a clean upper-cased str→str dict.
    Handles the case where DictReader returns a list value (overflow when
    a row has more fields than the header — common in PTAD pipe files).
    """
    clean: Dict[str, str] = {}
    for k, v in row.items():
        key = (str(k) if k is not None else "").strip().upper()
        if isinstance(v, list):
            v = " ".join(str(x) for x in v if x is not None)
        elif v is None:
            v = ""
        else:
            v = str(v)
        clean[key] = v.strip()
    return clean


def _discover_ncad_export_url() -> str:
    """Scrape https://nuecescad.net/downloads-reports/ for the most
    recent 'Public Export' ZIP link, with a known-good fallback if the
    page can't be parsed (WAF, layout change, etc.).
    """
    # Last-known-good URL — used as a fallback if discovery fails. Update
    # this once a year when NCAD posts the new preliminary roll.
    KNOWN_GOOD = (
        "https://nuecescad.net/wp-content/uploads/2026/04/"
        "2026-Preliminary-Public-Export-20260402.zip"
    )

    try:
        html = _http_get_text(NCAD_DOWNLOADS_PAGE)
    except Exception as exc:
        log.warning("NCAD page fetch failed (%s); using known-good URL", exc)
        return KNOWN_GOOD

    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")

    candidates: List[Tuple[str, str]] = []  # (year-key, url)
    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = a.get_text(" ", strip=True).lower()
        if not href.lower().endswith(".zip"):
            continue
        # Skip GIS shapefiles and parcel-only exports — we want the full
        # appraisal roll, which contains owner + situs + mailing addresses.
        if "shapefile" in text or "ncad_parcels" in href.lower():
            continue
        # Match anything that looks like an appraisal-roll export.
        href_low = href.lower()
        if not any(tok in href_low for tok in (
            "public-export", "public_export", "publicexport",
            "appraisal-roll", "appraisal_roll", "certified-roll",
            "preliminary-public", "preliminary_public",
        )):
            continue
        # Extract a year-ish sort key (prefer the most recent).
        m = re.search(r"(20\d{2})", href)
        year = m.group(1) if m else "0000"
        candidates.append((year, href))

    if not candidates:
        log.warning("no NCAD export link found on downloads page; "
                    "using known-good URL")
        return KNOWN_GOOD
    candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return candidates[0][1]


def _iter_parcel_rows(zf: zipfile.ZipFile) -> Iterable[Dict[str, str]]:
    """Yield row dicts from any plausible data file inside the NCAD ZIP."""
    names = zf.namelist()

    # Pass 1: any DBF files (rare, but supported per spec).
    for name in names:
        if name.lower().endswith(".dbf") and _HAS_DBFREAD:
            try:
                tmp_path = CACHE_DIR / Path(name).name
                CACHE_DIR.mkdir(parents=True, exist_ok=True)
                with zf.open(name) as src, open(tmp_path, "wb") as dst:
                    dst.write(src.read())
                table = DBF(str(tmp_path), load=False, ignore_missing_memofile=True,
                            encoding="latin-1")
                for rec in table:
                    yield {k: ("" if v is None else str(v)).strip()
                           for k, v in rec.items()}
            except Exception as exc:
                log.warning("dbf parse failed for %s: %s", name, exc)

    # Pass 2: pipe-delimited or comma-delimited text/CSV files.
    # NCAD's export ships ~30 files; we want the one that ties owners
    # to addresses (typically APPRAISAL_INFO.TXT or PROPERTY_INFO.TXT).
    text_candidates = [
        n for n in names
        if n.lower().endswith((".txt", ".csv", ".tsv"))
        and not n.lower().endswith(".pdf.txt")
    ]
    # Heuristic ordering — most informative files first.
    def rank(n: str) -> int:
        ln = n.lower()
        score = 0
        for kw, w in [
            ("appraisal_info", 100), ("appraisalinfo", 100),
            ("property_info", 95),   ("propertyinfo", 95),
            ("property", 80), ("prop", 60),
            ("owner", 50), ("parcel", 40),
        ]:
            if kw in ln:
                score = max(score, w)
        # Skip files we know don't carry address/owner info.
        for skip_kw in ("appraisal_agent", "deed_history", "land",
                        "improvement", "exemption", "abatement",
                        "entity", "arb_", "audit"):
            if skip_kw in ln:
                return -1
        return score
    text_candidates = [(n, rank(n)) for n in text_candidates]
    text_candidates = [(n, r) for n, r in text_candidates if r >= 0]
    text_candidates.sort(key=lambda nr: nr[1], reverse=True)

    # Hard wall-clock budget for the parse phase — protects against
    # pathological files. NCAD parsing should take < 2 minutes total.
    parse_deadline = time.time() + 4 * 60   # 4 minutes
    files_with_data = 0

    for name, _ in text_candidates:
        if time.time() > parse_deadline:
            log.warning("NCAD parse time budget exhausted; stopping at %s", name)
            break
        try:
            with zf.open(name) as fh:
                raw = fh.read()
            text = _decode_loose(raw)
            if not text.strip():
                continue
            delim = _sniff_delimiter(text)
            reader = csv.DictReader(io.StringIO(text), delimiter=delim)
            row_count = 0
            owner_rows = 0
            for row in reader:
                row_count += 1
                # Be defensive: DictReader can return a list as a value when
                # there are duplicate column headers (which Texas PTAD files
                # sometimes have). Coerce everything to str safely.
                clean = {}
                for k, v in row.items():
                    key = (str(k) if k is not None else "").strip().upper()
                    if isinstance(v, list):
                        v = " ".join(str(x) for x in v if x is not None)
                    elif v is None:
                        v = ""
                    else:
                        v = str(v)
                    clean[key] = v.strip()
                # Only yield rows that look like they contain owner/address
                # data — otherwise we're mixing schemas from many files.
                if any(c in clean for c in (
                    "OWNER", "OWN1", "OWNER1", "OWNER_NAME",
                    "PYOWNER", "PRIMARY_OWNER",
                )):
                    owner_rows += 1
                    yield clean
            log.info("  parsed %s: %d rows (%d with owner), delim=%r",
                     name, row_count, owner_rows, delim)
            if owner_rows > 0:
                files_with_data += 1
                # Once we've found owner data in a file, that's almost
                # certainly the primary owner file. Don't keep parsing
                # other files — they may have conflicting schemas.
                if files_with_data >= 1 and owner_rows > 100:
                    break
        except Exception as exc:
            log.warning("text parse failed for %s: %s", name, exc)
            continue


def _decode_loose(raw: bytes) -> str:
    for enc in ("utf-8", "utf-8-sig", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", errors="replace")


def _sniff_delimiter(text: str) -> str:
    sample = text[:8192]
    counts = {d: sample.count(d) for d in ("|", "\t", ",", ";")}
    return max(counts, key=counts.get)


# Column-name candidates per logical field.
_COL_CANDIDATES = {
    "owner":      ["OWNER", "OWN1", "OWNER1", "OWNER_NAME", "PYOWNER", "PRIMARY_OWNER"],
    "site_addr":  ["SITE_ADDR", "SITEADDR", "SITUS_ADDR", "SITUS", "PROP_ADDR", "PROPADDR", "SITE_ADDRESS"],
    "site_city":  ["SITE_CITY", "SITUS_CITY", "PROP_CITY"],
    "site_state": ["SITE_STATE", "SITUS_STATE", "PROP_STATE"],
    "site_zip":   ["SITE_ZIP", "SITUS_ZIP", "PROP_ZIP"],
    "mail_addr":  ["MAIL_ADDR", "MAILADR1", "ADDR_1", "ADDR1", "MAIL_ADDRESS_1", "MAILING_ADDR"],
    "mail_city":  ["MAIL_CITY", "MAILCITY", "CITY"],
    "mail_state": ["MAIL_STATE", "STATE"],
    "mail_zip":   ["MAIL_ZIP", "MAILZIP", "ZIP", "ZIPCODE"],
}


def _normalize_parcel_row(row: Dict[str, str]) -> Optional[Dict[str, str]]:
    if not row:
        return None
    upper = {(k or "").upper(): (v or "") for k, v in row.items()}

    def pick(key: str) -> str:
        for cand in _COL_CANDIDATES[key]:
            if cand in upper and upper[cand]:
                return upper[cand].strip()
        return ""

    owner = pick("owner")
    if not owner:
        return None

    return {
        "_owner_raw":  owner,
        "site_addr":  pick("site_addr"),
        "site_city":  pick("site_city") or "CORPUS CHRISTI",
        "site_state": pick("site_state") or "TX",
        "site_zip":   pick("site_zip"),
        "mail_addr":  pick("mail_addr"),
        "mail_city":  pick("mail_city"),
        "mail_state": pick("mail_state") or "TX",
        "mail_zip":   pick("mail_zip"),
    }


def _owner_name_variants(name: str) -> List[str]:
    """Generate normalized variants of an owner name for lookup matching."""
    if not name:
        return []
    n = re.sub(r"\s+", " ", name.upper().strip())
    # Strip trailing entity tags.
    n = re.sub(r"\b(ETAL|ET\s*AL|ET\s*UX|JR|SR|II|III|IV|TRUSTEE|TR|EST(ATE)?)\b", "", n).strip()
    # Strip punctuation except commas (we use commas to detect "LAST, FIRST").
    cleaned = re.sub(r"[^A-Z0-9, ]", " ", n)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,")

    out: set[str] = {cleaned}

    if "," in cleaned:
        # "LAST, FIRST [MIDDLE]" → also produce "FIRST LAST" and "LAST FIRST".
        last, _, rest = cleaned.partition(",")
        last = last.strip()
        rest = rest.strip()
        if last and rest:
            out.add(f"{rest} {last}")           # FIRST LAST
            out.add(f"{last} {rest}")           # LAST FIRST
            # Also strip middle names: "FIRST LAST"
            first_only = rest.split()[0] if rest.split() else ""
            if first_only:
                out.add(f"{first_only} {last}")
                out.add(f"{last} {first_only}")
    else:
        # No comma — guess. Texas appraisal data typically encodes individuals
        # as "LAST FIRST [MIDDLE]" without punctuation. Generate the swap.
        parts = cleaned.split()
        if 2 <= len(parts) <= 4:
            last = parts[0]
            rest = " ".join(parts[1:])
            out.add(f"{last}, {rest}")          # LAST, FIRST
            out.add(f"{rest} {last}")           # FIRST LAST

    return [v for v in out if v]


# --------------------------------------------------------------------------- #
# Enrichment + scoring
# --------------------------------------------------------------------------- #

def enrich_with_parcels(records: List[ClerkRecord],
                        owner_lookup: Dict[str, Dict[str, str]]) -> None:
    """Mutates `records` in place, filling in property + mailing address
    fields. First tries the NCAD owner→parcel lookup. As a fallback,
    extracts a Texas-shaped property address directly from the legal
    description field — which the Nueces clerk often uses for the
    site address on Lis Pendens, Foreclosure, and Mechanic Lien records.
    """
    matched_ncad = 0
    extracted_legal = 0

    for rec in records:
        # Strategy A: NCAD owner-name match.
        if owner_lookup and rec.owner:
            for variant in _owner_name_variants(rec.owner):
                info = owner_lookup.get(variant)
                if info:
                    rec.prop_address = info.get("site_addr", "")
                    rec.prop_city    = info.get("site_city", "")
                    rec.prop_state   = info.get("site_state", "TX")
                    rec.prop_zip     = info.get("site_zip", "")
                    rec.mail_address = info.get("mail_addr", "")
                    rec.mail_city    = info.get("mail_city", "")
                    rec.mail_state   = info.get("mail_state", "")
                    rec.mail_zip     = info.get("mail_zip", "")
                    matched_ncad += 1
                    break

        # Strategy B: extract address from legal description.
        # The Nueces clerk often records the property's street address in
        # the legal-description column for Lis Pendens, Foreclosures, and
        # Mechanic Liens. Pull it if the property fields are still empty.
        if not rec.prop_address and rec.legal:
            addr = _extract_tx_address(rec.legal)
            if addr:
                rec.prop_address = addr["street"]
                rec.prop_city    = addr["city"] or "CORPUS CHRISTI"
                rec.prop_state   = addr["state"] or "TX"
                rec.prop_zip     = addr["zip"]
                extracted_legal += 1

    log.info("address enrichment: NCAD=%d, legal-extract=%d / %d total (%d%% have address)",
             matched_ncad, extracted_legal, len(records),
             int(100 * (matched_ncad + extracted_legal) / max(1, len(records))))


# Texas street types we recognize when sniffing addresses out of legal text.
_TX_STREET_TYPES = (
    "ST", "STREET", "AVE", "AVENUE", "BLVD", "BOULEVARD",
    "DR", "DRIVE", "RD", "ROAD", "LN", "LANE", "CT", "COURT",
    "PL", "PLACE", "WAY", "TRL", "TRAIL", "PKWY", "PARKWAY",
    "CIR", "CIRCLE", "TER", "TERRACE", "HWY", "HIGHWAY", "LOOP",
    "BAY", "RUN", "ROW", "PATH", "PASS", "CROSS",
)


def _extract_tx_address(text: str) -> Optional[Dict[str, str]]:
    """Pull a Texas property address out of unstructured text.

    Matches patterns like:
      "226 BUSHICK PL CORPUS CHRISTI TX 78402"
      "1234 MAIN ST, CORPUS CHRISTI TX 78415"
      "5678 N OAK STREET CORPUS CHRISTI, TX 78404-1234"

    Returns {street, city, state, zip} or None if no clean match.
    """
    if not text:
        return None
    t = text.upper().strip()
    # Strip trailing parentheticals and Lot/Block suffixes — those aren't
    # part of the address but often follow it.
    t = re.sub(r"\s*\([^)]*\)\s*$", "", t).strip()
    t = re.sub(r"\s*\b(LOT|BLK|BLOCK|UNIT|APT|SUITE|STE)\s+[\w-]+(\s+[\w-]+)?\s*$",
               "", t, flags=re.IGNORECASE).strip()
    # Must start with a number (the street number).
    if not re.match(r"^\s*\d+", t):
        return None
    # Build the regex capturing street-num, name, type, then optional city/state/zip.
    types = "|".join(_TX_STREET_TYPES)
    pattern = re.compile(
        rf"^\s*(?P<num>\d+(?:[-/]\d+)?)\s+"
        rf"(?P<name>[A-Z0-9 ]+?)\s+"
        rf"(?P<type>{types})\b"
        rf"(?P<rest>.*)$",
        re.IGNORECASE,
    )
    m = pattern.match(t)
    if not m:
        return None
    street = f"{m.group('num')} {m.group('name').strip()} {m.group('type')}"
    rest = (m.group("rest") or "").strip(" ,")
    city, state, zip_code = "", "", ""
    if rest:
        # Pull off ZIP first (last 5 digits, optionally with -4 extension).
        zm = re.search(r"(\d{5})(?:-\d{4})?\s*$", rest)
        if zm:
            zip_code = zm.group(1)
            rest = rest[:zm.start()].strip(" ,")
        # State (2-letter at the end of remaining).
        sm = re.search(r"\b([A-Z]{2})\s*$", rest)
        if sm:
            state = sm.group(1)
            rest = rest[:sm.start()].strip(" ,")
        city = rest.strip(" ,")
    return {
        "street": street.strip(),
        "city": city,
        "state": state,
        "zip": zip_code,
    }


def compute_flags_and_score(rec: ClerkRecord, today_iso: str,
                            owner_doc_count: Dict[str, int]) -> None:
    """Set rec.flags and rec.score per the spec.

    Score: base 30
         + 10 per flag
         + 20 if both LP and FC apply to this owner
         + 15 if amount > 100k
         + 10 if amount > 50k
         +  5 if filed within 7 days
         +  5 if we have a property/site address
    """
    flags: List[str] = []

    if rec.cat == "LP":
        flags.append("Lis pendens")
    if rec.cat == "NOFC":
        flags.append("Pre-foreclosure")
    if rec.cat == "JUD":
        flags.append("Judgment lien")
    if rec.cat == "LNFED":
        flags.append("Tax lien")
    if rec.cat == "LN" and re.search(r"MECH", rec.doc_type, re.I):
        flags.append("Mechanic lien")
    if rec.cat == "PRO":
        flags.append("Probate / estate")
    if rec.cat == "MEDLN":
        flags.append("Medicaid lien")

    if rec.owner and re.search(r"\b(LLC|INC|CORP|LP|LLP|LTD|TRUST|HOLDINGS?)\b",
                               rec.owner, re.I):
        flags.append("LLC / corp owner")

    # "New this week" — within 7 days of today_iso.
    if rec.filed:
        try:
            d = datetime.fromisoformat(rec.filed).date()
            if (datetime.fromisoformat(today_iso).date() - d).days <= 7:
                flags.append("New this week")
        except Exception:
            pass

    # LP+FC combo lookup: owner has BOTH a Lis Pendens and a Foreclosure.
    has_combo = False
    if rec.owner:
        owner_norm = rec.owner.upper().strip()
        cats_for_owner = owner_doc_count.get(owner_norm, set())
        if "LP" in cats_for_owner and "NOFC" in cats_for_owner:
            has_combo = True

    score = 30
    score += 10 * len(flags)
    if has_combo:
        score += 20
    if rec.amount:
        if rec.amount > 100_000:
            score += 15
        elif rec.amount > 50_000:
            score += 10
    if "New this week" in flags:
        score += 5
    if rec.prop_address:
        score += 5

    rec.flags = flags
    rec.score = max(0, min(100, score))


def build_owner_cat_index(records: List[ClerkRecord]) -> Dict[str, set]:
    idx: Dict[str, set] = {}
    for r in records:
        if not r.owner:
            continue
        key = r.owner.upper().strip()
        idx.setdefault(key, set()).add(r.cat)
    return idx


# --------------------------------------------------------------------------- #
# Output: JSON + GHL CSV
# --------------------------------------------------------------------------- #

def write_outputs(records: List[ClerkRecord], start_iso: str, end_iso: str) -> None:
    DASHBOARD_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    payload = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "source": "Nueces County Clerk + NCAD",
        "date_range": {"start": start_iso, "end": end_iso},
        "total": len(records),
        "with_address": sum(1 for r in records if r.prop_address),
        "records": [asdict(r) for r in records],
    }

    for path in (DASHBOARD_DIR / "records.json", DATA_DIR / "records.json"):
        with path.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, default=str)
        log.info("wrote %s (%d records)", path, len(records))

    csv_path = DATA_DIR / "leads_for_ghl.csv"
    write_ghl_csv(records, csv_path)
    log.info("wrote %s", csv_path)


def write_ghl_csv(records: List[ClerkRecord], path: Path) -> None:
    """GoHighLevel-importable CSV with the columns specified in the brief."""
    header = [
        "First Name", "Last Name",
        "Mailing Address", "Mailing City", "Mailing State", "Mailing Zip",
        "Property Address", "Property City", "Property State", "Property Zip",
        "Lead Type", "Document Type", "Date Filed", "Document Number",
        "Amount/Debt Owed", "Seller Score", "Motivated Seller Flags",
        "Source", "Public Records URL",
    ]
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        for r in records:
            first, last = _split_owner_name(r.owner)
            w.writerow([
                first, last,
                r.mail_address, r.mail_city, r.mail_state, r.mail_zip,
                r.prop_address, r.prop_city, r.prop_state, r.prop_zip,
                r.cat_label, r.doc_type, r.filed, r.doc_num,
                f"{r.amount:.2f}" if r.amount else "",
                r.score, " | ".join(r.flags),
                "Nueces County Clerk", r.clerk_url,
            ])


def _split_owner_name(owner: str) -> Tuple[str, str]:
    """Best-effort split into (first, last) for individual owners.
    Entities (LLC, INC, etc.) go into Last with First left blank.
    """
    if not owner:
        return "", ""
    o = owner.strip()
    if re.search(r"\b(LLC|INC|CORP|LP|LLP|LTD|TRUST|COMPANY|CO|HOLDINGS?)\b", o, re.I):
        return "", o
    if "," in o:
        last, _, rest = o.partition(",")
        first = rest.strip().split()[0] if rest.strip() else ""
        return first.title(), last.strip().title()
    parts = o.split()
    if len(parts) == 1:
        return "", parts[0].title()
    # Texas appraisal data: "LAST FIRST" is common. Heuristic — if first
    # token looks like a surname (all caps in the source, rare given name),
    # keep the swap; otherwise treat as "FIRST LAST".
    return parts[-1].title(), " ".join(parts[:-1]).title()


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main() -> int:
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=LOOKBACK_DAYS)
    start_iso = start.isoformat()
    end_iso = today.isoformat()
    log.info("=== Nueces lead scrape: %s .. %s ===", start_iso, end_iso)

    # 1) Clerk portal.
    try:
        clerk_records = asyncio.run(fetch_clerk_records(start_iso, end_iso))
    except Exception as exc:
        log.error("clerk fetch failed entirely: %s\n%s",
                  exc, traceback.format_exc())
        clerk_records = []

    # 2) Property appraiser bulk export (best-effort).
    try:
        owner_lookup = fetch_ncad_parcels()
    except Exception as exc:
        log.error("NCAD fetch failed: %s\n%s", exc, traceback.format_exc())
        owner_lookup = {}

    # 3) Enrich.
    enrich_with_parcels(clerk_records, owner_lookup)

    # 4) Score.
    owner_idx = build_owner_cat_index(clerk_records)
    for rec in clerk_records:
        try:
            compute_flags_and_score(rec, end_iso, owner_idx)
        except Exception as exc:
            log.warning("scoring failed for %s: %s", rec.doc_num, exc)
            rec.flags = rec.flags or []
            rec.score = rec.score or 30

    # 5) Sort by score desc, then by filed desc.
    clerk_records.sort(key=lambda r: (-(r.score or 0), r.filed or ""), reverse=False)
    clerk_records.sort(key=lambda r: r.score or 0, reverse=True)

    # 6) Write outputs.
    write_outputs(clerk_records, start_iso, end_iso)

    log.info("=== done: %d records ===", len(clerk_records))
    return 0


if __name__ == "__main__":
    sys.exit(main())
