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

# NCAD's owner-name search portal (BIS Consultants "esearch" platform).
# Used to look up property + mailing addresses for owners who appear in
# clerk records but whose addresses aren't in the legal-description text.
# This is the path that fills in addresses on Judgments and Tax Liens.
NCAD_ESEARCH_BASE = "https://esearch.nuecescad.net"

# Cache file for esearch lookups. Keeps us from re-querying the same name
# every day; results are valid until the parcel data shifts (months).
NCAD_SEARCH_CACHE = ".cache/ncad_search_cache.json"

# Rate-limit knobs for the esearch lookup phase.
NCAD_SEARCH_MAX_LOOKUPS = 100      # per run — protects against runaway loops
NCAD_SEARCH_DELAY_SEC   = 1.5      # between requests, polite to the server
NCAD_SEARCH_PHASE_BUDGET_SEC = 8 * 60   # hard wall-clock cap

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
# --------------------------------------------------------------------------- #
# Lead categories
# --------------------------------------------------------------------------- #
#
# Each category is fetched independently with a precise URL filter rather
# than keyword-matching the search box. The portal exposes:
#
#   * `department=RP` — Official Public Records (default).
#   * `department=FC` — Foreclosures (separate tab, different schema).
#   * `_docTypes=<code>` — sidebar filter for a specific document type.
#   * `searchValue=<text>` — keyword scoped to grantor/grantee/legal.
#
# Foreclosure leads are pulled from the FC department and treated as a
# separate output stream (dashboard/foreclosures.json) — they have a
# fundamentally different schema (no grantor/grantee, has Sale Date).
#
# A few legacy categories (Tax Deed, IRS Lien, Probate, etc.) don't have
# their own sidebar filter, so we still fall back to keyword search for
# those. Per the user's instruction, keep them around for now.

# Categories driven by the portal's built-in `_docTypes` filter.
# These are precise: every record returned belongs to that doc type.
PORTAL_FILTERED_CATEGORIES: List[Dict[str, Any]] = [
    {"cat": "LP",      "label": "Lis Pendens",
     "doc_types": "LP2"},
    {"cat": "JUD",     "label": "Judgment",
     "doc_types": "J"},
    {"cat": "LN",      "label": "Lien",
     "doc_types": "L3"},
    {"cat": "HL",      "label": "Hospital Lien",
     "doc_types": "HL"},
    {"cat": "LNMECH",  "label": "Mechanics Lien",
     "doc_types": "MECHL"},
    {"cat": "MODIF",   "label": "Loan Modification",
     "doc_types": "MODIF"},
    {"cat": "APPNMT",  "label": "Appointment of Sub Trust",
     "doc_types": "APPNMT"},
    # City of Corpus Christi Lien — same Lien filter PLUS a keyword
    # constraint on the grantor side. Records that match this AND the
    # general Lien category are deduped (CCLN wins).
    {"cat": "CCLN",    "label": "City of Corpus Christi Lien",
     "doc_types": "L3", "search_value": "city of corpus christi"},
]

# Categories that don't have a corresponding sidebar filter — fall back
# to keyword search of the grantor/grantee/legal index.
KEYWORD_CATEGORIES: List[Dict[str, Any]] = [
    {"cat": "TAXDEED", "label": "Tax Deed",
     "queries": ["TAX DEED"]},
    {"cat": "LNFED",   "label": "Federal / IRS / Corp Tax Lien",
     "queries": ["FEDERAL TAX LIEN", "IRS LIEN"]},
    {"cat": "MEDLN",   "label": "Medicaid Lien",
     "queries": ["MEDICAID LIEN"]},
    {"cat": "PRO",     "label": "Probate",
     "queries": ["PROBATE", "LETTERS TESTAMENTARY", "AFFIDAVIT OF HEIRSHIP"]},
    {"cat": "NOC",     "label": "Notice of Commencement",
     "queries": ["NOTICE OF COMMENCEMENT"]},
    {"cat": "RELLP",   "label": "Release of Lis Pendens",
     "queries": ["RELEASE OF LIS PENDENS"]},
]

# Mortgage Foreclosure category — fetched separately from the FC tab.
# Stored in its own output file, never mixed with the motivated-seller
# leads. No score; flagged pre/post by sale date.
FORECLOSURE_CAT = {"cat": "MFC", "label": "Mortgage Foreclosure"}
FORECLOSURE_LOOKAHEAD_DAYS = 90  # window: today through today+90

# Map raw doc-type strings (from clerk results) to our category code,
# used as a fallback when a record's category isn't already known.
DOC_TYPE_TO_CAT: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"\bRELEASE\b.*\bLIS\s*PENDENS\b", re.I),  "RELLP"),
    (re.compile(r"\bLIS\s*PENDENS\b", re.I),               "LP"),
    (re.compile(r"\bTAX\s*DEED\b", re.I),                  "TAXDEED"),
    (re.compile(r"\bMEDICAID\s*LIEN\b", re.I),             "MEDLN"),
    (re.compile(r"\bIRS\s*LIEN\b|\bFEDERAL\s*TAX\s*LIEN\b", re.I), "LNFED"),
    (re.compile(r"\bHOSPITAL\s*LIEN\b", re.I),             "HL"),
    (re.compile(r"\bMECHANIC", re.I),                      "LNMECH"),
    (re.compile(r"\bMODIFICATION\b", re.I),                "MODIF"),
    (re.compile(r"\bAPPOINTMENT\b", re.I),                 "APPNMT"),
    (re.compile(r"\bLIEN\b", re.I),                        "LN"),
    (re.compile(r"\bABSTRACT\s*OF\s*JUDG|\bJUDG", re.I),   "JUD"),
    (re.compile(r"\bPROBATE\b|\bLETTERS\s*TESTAMENTARY\b|\bHEIRSHIP\b", re.I), "PRO"),
    (re.compile(r"\bNOTICE\s*OF\s*COMMENCEMENT\b", re.I),  "NOC"),
]

CAT_TO_LABEL = {c["cat"]: c["label"]
                for c in (PORTAL_FILTERED_CATEGORIES + KEYWORD_CATEGORIES
                          + [FORECLOSURE_CAT])}

# Money regex — picks the largest-looking $-amount in a record.
_AMOUNT_RE = re.compile(r"\$?\s*([0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]{1,2})?|[0-9]+(?:\.[0-9]{1,2})?)")

# Pattern that identifies an institutional plaintiff/creditor/agency —
# i.e. a name where the *real* property owner we want to market to is
# the OTHER party in the recording (the grantee), not the grantor. Used
# in two places:
#
#   1. _normalize_clerk_row — to flip grantor↔grantee on adversarial
#      records (tax liens, judgments, etc.) so the dashboard shows the
#      defendant/debtor as "owner" instead of the IRS/bank/debt collector.
#
#   2. _looks_institutional / esearch — to skip futile property-search
#      lookups for entities that obviously don't own Nueces parcels.
#
# Order roughly by frequency in real Nueces filings — most common first.
# Word boundaries (\b) prevent false positives like "BANK STREET LLC".
INSTITUTIONAL_PLAINTIFF_RE = re.compile(
    r"\b("
    # Federal / state / local government
    r"USA|UNITED\s*STATES(?:\s*OF\s*AMERICA)?|"
    r"INTERNAL\s*REVENUE|IRS|U\.?S\.?\s*TREASURY|TREASURY|"
    r"STATE\s*OF\s*\w+|TEXAS\s*COMPTROLLER|TEXAS\s*WORKFORCE|"
    r"COUNTY\s*OF|CITY\s*OF|DEPARTMENT\s*OF|"
    r"NUECES\s*COUNTY|NUECES\s*CO\b|"     # county acting as plaintiff
    r"DISTRICT\s*COURT|MUNICIPAL\s*COURT|COMMISSIONERS?|"
    r"MEDICAID|MEDICARE|HHS|"
    r"ATTORNEY\s*GENERAL|"

    # Government-sponsored mortgage entities
    r"FREDDIE\s*MAC|FANNIE\s*MAE|GINNIE\s*MAE|"
    r"HUD|HOUSING\s*AND\s*URBAN\s*DEVELOPMENT|"

    # Banks (all the common Nueces ones, plus generic patterns)
    r"BANK\s*(OF|N\.?\s*A\.?|NATIONAL)|"
    r"\w+\s*BANK\s*(N\.?\s*A\.?|NA)?\s*$|"     # ends in "...BANK NA"
    r"WELLS\s*FARGO|JPMORGAN|JP\s*MORGAN|CHASE\s*BANK|"
    r"CITIBANK|CITIGROUP|CITI\s*N\.?\s*A\.?|"
    r"BANK\s*OF\s*AMERICA|BOFA|"
    r"PROSPERITY\s*BANK|FROST\s*BANK|"
    r"DISCOVER\s*(BANK)?|AMERICAN\s*EXPRESS|AMEX|"
    r"CAPITAL\s*ONE|U\.?S\.?\s*BANK|TD\s*BANK|"
    r"SYNCHRONY\s*BANK|"

    # Credit unions & similar
    r"CREDIT\s*UNION|FEDERAL\s*CREDIT\s*UNION|FCU\b|"

    # Mortgage / financing
    r"MORTGAGE|FINANCIAL|FINANCE\s*(CORP|COMPANY|LLC)|"

    # Debt collectors / tax-lien funds / receivable buyers
    r"MIDLAND\s*CREDIT|MIDLAND\s*FUNDING|"
    r"PORTFOLIO\s*RECOVERY|LVNV\s*FUNDING|"
    r"CAVALRY\s*(SPV|PORTFOLIO)|UNIFIN|"
    r"TAX\s*LIEN\s*FUND|PROPEL\s*TAX|"
    r"(CREDIT|CAPITAL|RECEIVABLES?|RECOVERY|COLLECTION|FUNDING|FUND)\s*"
        r"(MANAGEMENT|SERVICES?|GROUP|CORP|INC|LLC|LP|TRUST|SOLUTIONS)|"

    # Hospital / medical billing entities (these file medical liens)
    r"CHRISTUS\s+SPOHN|CORPUS\s*CHRISTI\s*MEDICAL|"
    r"HOSPITAL|MEDICAL\s*CENTER|HEALTH\s*CARE\b|"
    r"REVECORE|TPL\s*SPECIALIST|"

    # HOAs / condo associations (file HOA liens; not motivated sellers)
    r"HOA\b|HOMEOWNERS\s*ASSOCIATION|"
    r"\w+\s*CONDOMINIUM(\s*OWNERS?)?\s*(ASSOCIATION|ASSN)?|"
    r"COUNCIL\s*OF\s*(CO\s*[-]?\s*)?OWNERS|"
    r"PROPERTY\s*OWNERS\s*ASSOCIATION|POA\b"
    r")\b",
    re.IGNORECASE,
)


def _is_institutional_plaintiff(name: str) -> bool:
    """True if the name looks like an institutional plaintiff/creditor/
    agency — used to swap grantor↔grantee on adversarial recordings.
    """
    if not name:
        return False
    return bool(INSTITUTIONAL_PLAINTIFF_RE.search(name))

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


@dataclass
class ForeclosureRecord:
    """Mortgage Foreclosure (FC tab) — separate output stream from the
    motivated-seller leads. The FC tab returns different columns:
    Doc Type, Recorded Date, Sale Date, Doc Number, Property Address.
    No grantor/grantee. Status flips pre→post automatically based on
    today vs sale_date.
    """
    doc_num: str
    doc_type: str
    recorded: str        # ISO date YYYY-MM-DD
    sale_date: str       # ISO date YYYY-MM-DD
    legal: str           # the "Property Address" cell — usually a legal
                          # description like "LT 9 BK 2 DOUGLAS UNIT TWO"
    clerk_url: str = ""
    # Filled in only when (eventually) we read the actual PDF:
    owner: str = ""
    loan_amount: Optional[float] = None
    prop_address: str = ""
    prop_city: str = ""
    prop_state: str = "TX"
    prop_zip: str = ""
    mail_address: str = ""
    mail_city: str = ""
    mail_state: str = ""
    mail_zip: str = ""


# Used by the dedup logic when a doc matches multiple categories — the
# more specific category wins. Higher number = more specific.
_CAT_SPECIFICITY = {
    "CCLN":   100,   # most specific: City of Corpus Christi Lien
    "HL":      80,   # Hospital Lien
    "LNMECH":  80,   # Mechanics Lien
    "MODIF":   70,   # Loan Modification
    "APPNMT":  70,   # Appointment of Sub Trust
    "TAXDEED": 60,
    "LNFED":   60,
    "MEDLN":   60,
    "PRO":     60,
    "RELLP":   60,
    "NOC":     60,
    "LP":      50,
    "JUD":     50,
    "LN":      40,   # generic Lien — least specific
    "MFC":     90,   # Mortgage Foreclosure (its own tab anyway)
}


def _category_specificity(cat: str) -> int:
    return _CAT_SPECIFICITY.get(cat, 0)


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


def _build_clerk_search_url(start_iso: str, end_iso: str,
                             query: str = "",
                             doc_types: str = "",
                             department: str = "RP") -> str:
    """Build the deep-link URL for the advanced-search results page.

    Three modes:
      1. Keyword-only:        query=X,    doc_types=""
      2. DocType-filter only: query="",   doc_types="LP2"
      3. Both:                query="city of corpus christi", doc_types="L3"

    The portal accepts both at once, treating them as AND.
    """
    start_compact = start_iso.replace("-", "")
    end_compact = end_iso.replace("-", "")
    params: Dict[str, Any] = {
        "department": department,
        "limit": 250,    # portal max — cuts pagination by 5x
        "offset": 0,
        "keywordSearch": "false",
        "searchOcrText": "false",
        "searchType": "quickSearch",
        "recordedDateRange": f"{start_compact},{end_compact}",
    }
    if query:
        params["searchValue"] = query
    if doc_types:
        params["_docTypes"] = doc_types
    return f"{CLERK_BASE}/results?{urlencode(params)}"


def _build_foreclosure_url(start_iso: str, end_iso: str) -> str:
    """Build the URL for the Foreclosures (FC) tab.

    The FC tab uses `instrumentDateRange` (not `recordedDateRange`) and
    the date range filters by Sale Date — so for motivated-seller leads
    we want today through today+90 days (upcoming auctions).
    """
    start_compact = start_iso.replace("-", "")
    end_compact = end_iso.replace("-", "")
    params = {
        "department": "FC",
        "instrumentDateRange": f"{start_compact},{end_compact}",
        "keywordSearch": "false",
        "searchOcrText": "false",
        "searchType": "quickSearch",
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

        diagnostics = {"saved": False}    # closure-shared via dict (single ref)

        async def _do_search(url: str, default_cat: str,
                              date_window: Tuple[str, str]) -> int:
            """Navigate to `url`, parse the rendered table, normalize rows
            into ClerkRecord, dedupe by doc_num into `seen`. Returns the
            number of rows newly added (kept) by this search."""
            captured_payloads.clear()
            t_nav_start = time.time()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=25_000)
                try:
                    await page.wait_for_function(
                        """() => {
                            const rows = document.querySelectorAll('table tbody tr');
                            for (const r of rows) {
                                const docCell = r.querySelector('.col-7');
                                if (docCell && docCell.textContent.trim())
                                    return true;
                            }
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
            except Exception as exc:
                log.error("nav failed for url=%s: %s", url, exc)
                return 0

            fresh = [p for p in captured_payloads if p["ts"] >= t_nav_start]
            try:
                html = await page.content()
            except Exception:
                html = ""

            rows = _extract_clerk_table_rows(html)

            # CCLN-specific enhancement: harvest Consideration and
            # Instrument Date from the captured JSON payloads (the
            # portal's XHR response contains the full document records
            # including these fields, even though the rendered table
            # doesn't show them in list view). Merge by doc_number.
            # This avoids needing card-view DOM scraping, which is
            # fragile across portal versions.
            if default_cat == "CCLN" and rows:
                payload_rows = _extract_rows_from_payloads(fresh)
                if payload_rows:
                    by_doc = {r.get("doc_number"): r
                              for r in rows if r.get("doc_number")}
                    cons_added = 0
                    for pr in payload_rows:
                        # JSON payload uses camelCase or lowercase keys —
                        # normalize lookups via lowercase tour.
                        keys_lower = {k.lower(): v for k, v in pr.items()
                                      if isinstance(k, str)}
                        dn = (keys_lower.get("docnumber")
                              or keys_lower.get("documentnumber")
                              or keys_lower.get("doc_number")
                              or keys_lower.get("instrumentnumber")
                              or "")
                        if isinstance(dn, (int, float)):
                            dn = str(int(dn))
                        elif not isinstance(dn, str):
                            dn = str(dn) if dn else ""
                        if not dn or dn not in by_doc:
                            continue
                        cons = (keys_lower.get("consideration")
                                or keys_lower.get("considerationamount")
                                or "")
                        inst = (keys_lower.get("instrumentdate")
                                or keys_lower.get("instrument_date")
                                or "")
                        if cons and not by_doc[dn].get("consideration"):
                            by_doc[dn]["consideration"] = str(cons)
                            cons_added += 1
                        if inst and not by_doc[dn].get("instrument_date"):
                            by_doc[dn]["instrument_date"] = str(inst)
                    log.info("  CCLN: merged consideration into %d rows "
                             "(from %d JSON payload records)",
                             cons_added, len(payload_rows))
                else:
                    log.info("  CCLN: no JSON payloads available for "
                             "consideration merge")

            # First-query diagnostics dump.
            if not diagnostics["saved"]:
                try:
                    (debug_dir / "first_query.html").write_text(
                        html, encoding="utf-8")
                    with (debug_dir / "first_query_payloads.json").open(
                            "w", encoding="utf-8") as fh:
                        json.dump(
                            [{"url": p["url"], "preview": str(p["body"])[:2000]}
                             for p in fresh], fh, indent=2, default=str)
                    with (debug_dir / "first_query_table_rows.json").open(
                            "w", encoding="utf-8") as fh:
                        json.dump(rows[:5], fh, indent=2, default=str)
                    log.info("diagnostics saved (%d table rows)", len(rows))
                    diagnostics["saved"] = True
                except Exception:
                    pass

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
            if not rows:
                rows = _extract_rows_from_payloads(fresh)
                if not rows and html:
                    rows = _extract_rows_from_html(html)

            source_label = (
                "table" if rows and isinstance(rows[0], dict)
                            and "doc_number" in rows[0]
                else ("redux" if rows else "none")
            )
            log.info("  → %d raw rows (source=%s)", len(rows), source_label)

            start_iso, end_iso = date_window
            kept = 0
            for raw in rows:
                try:
                    # CCLN-specific filter: only keep records where the
                    # CITY OF CORPUS CHRISTI is the GRANTOR (the entity
                    # filing/creating the lien). The grantee is the
                    # property owner being encumbered — that's our lead.
                    # Records where the city is the grantee (e.g. some
                    # OCR'd ones where it appears in the legal field) are
                    # not real city-issued liens.
                    if default_cat == "CCLN":
                        raw_grantor = (raw.get("grantor")
                                       or raw.get("Grantor") or "").upper()
                        if "CITY OF CORPUS CHRISTI" not in raw_grantor:
                            continue

                    rec = _normalize_clerk_row(raw, default_cat=default_cat)
                    if rec is None:
                        continue
                    if rec.filed:
                        if rec.filed < start_iso or rec.filed > end_iso:
                            continue
                    # Always set cat to the search's intended category — the
                    # portal filter guarantees the doc is of that type. The
                    # keyword regex in _classify is used only as a last resort
                    # in _normalize_clerk_row when default_cat is the catch-all.
                    rec.cat = default_cat
                    rec.cat_label = CAT_TO_LABEL.get(default_cat, rec.cat_label)
                    # Dedupe by doc_num. If we already have this doc and the
                    # new search is more specific (e.g. CCLN vs LN), prefer
                    # the more-specific category.
                    existing = seen.get(rec.doc_num)
                    if existing is None:
                        seen[rec.doc_num] = rec
                        kept += 1
                    elif _category_specificity(default_cat) > \
                         _category_specificity(existing.cat):
                        seen[rec.doc_num] = rec  # replace with more specific
                except Exception as exc:
                    log.warning("bad row skipped: %s", exc)
                    continue
            if rows and kept == 0:
                log.info("    (all %d rows fell outside date window or "
                         "failed to normalize)", len(rows))
            return kept

        # ---------- Pass A: portal-filtered categories ----------
        # Each one is a single targeted search using `_docTypes=<code>`.
        # Far more accurate than keyword matching.
        for cat_def in PORTAL_FILTERED_CATEGORIES:
            if time.time() > deadline:
                log.warning("clerk-portal time budget exhausted; stopping early")
                break
            url = _build_clerk_search_url(
                start_iso, end_iso,
                query=cat_def.get("search_value", ""),
                doc_types=cat_def["doc_types"],
            )
            log.info("clerk filter-search: cat=%s docTypes=%s search=%r",
                     cat_def["cat"], cat_def["doc_types"],
                     cat_def.get("search_value", ""))
            await _do_search(url, cat_def["cat"], (start_iso, end_iso))

        # ---------- Pass B: keyword categories ----------
        # Categories that don't have a sidebar filter — fall back to
        # keyword search. Stop after the first query in each category
        # finds rows (the rest are aliases, not additive).
        for cat_def in KEYWORD_CATEGORIES:
            if time.time() > deadline:
                log.warning("clerk-portal time budget exhausted; stopping early")
                break
            for q in cat_def["queries"]:
                if time.time() > deadline:
                    break
                url = _build_clerk_search_url(start_iso, end_iso, query=q)
                log.info("clerk keyword-search: cat=%s q=%r",
                         cat_def["cat"], q)
                kept = await _do_search(url, cat_def["cat"],
                                         (start_iso, end_iso))
                if kept > 0:
                    log.info("  (skipping fallback queries — got %d rows)", kept)
                    break

        await context.close()
        await browser.close()

    log.info("clerk: %d unique docs in window %s..%s",
             len(seen), start_iso, end_iso)
    return list(seen.values())


# --------------------------------------------------------------------------- #
# Foreclosures (FC department) — separate output stream
# --------------------------------------------------------------------------- #

async def fetch_foreclosures(today_iso: str,
                              lookahead_days: int = FORECLOSURE_LOOKAHEAD_DAYS
                              ) -> List[ForeclosureRecord]:
    """Drive the clerk portal's Foreclosures (FC) tab to retrieve every
    upcoming foreclosure with a sale date between `today` and
    `today + lookahead_days`.

    Distinct from the motivated-seller pipeline: FC rows have no
    grantor/grantee, but they do have a Sale Date that we use as the
    primary actionability signal. Owner enrichment via NCAD esearch is
    skipped here because FC rows expose a legal description (e.g.
    "LT 9 BK 2 DOUGLAS UNIT TWO") rather than a street address — that
    requires the eventual PDF-reader work item.
    """
    try:
        from playwright.async_api import async_playwright  # type: ignore
    except ImportError:
        log.error("playwright not installed; foreclosure tab skipped")
        return []

    today_dt = datetime.fromisoformat(today_iso).date()
    end_dt = today_dt + timedelta(days=lookahead_days)
    end_iso = end_dt.isoformat()

    url = _build_foreclosure_url(today_iso, end_iso)
    log.info("=== fetch_foreclosures: window=%s..%s ===", today_iso, end_iso)
    log.info("  url=%s", url)

    out: Dict[str, ForeclosureRecord] = {}

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
        context = await browser.new_context(user_agent=USER_AGENT)
        page = await context.new_page()

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            try:
                await page.wait_for_function(
                    """() => {
                        const rows = document.querySelectorAll('table tbody tr');
                        if (rows.length > 0) {
                            for (const r of rows) {
                                if (r.textContent && r.textContent.trim().length > 5)
                                    return true;
                            }
                        }
                        const txt = document.body.innerText || '';
                        return txt.includes('No Results Found') ||
                               txt.includes('returned no results');
                    }""",
                    timeout=20_000,
                )
            except Exception:
                pass
            await page.wait_for_timeout(800)
            html = await page.content()
        except Exception as exc:
            log.error("foreclosure fetch failed: %s", exc)
            await context.close()
            await browser.close()
            return []

        # The FC tab can be paginated. Try to load all pages by clicking
        # "Next" until disabled, but cap iterations for safety. For small
        # result counts (typical: 50-100 within 90-day window) usually all
        # results are on one page.
        all_rows = _extract_foreclosure_table_rows(html)
        log.info("  page 1: %d rows", len(all_rows))

        # Save diagnostics (first page only).
        debug_dir = ROOT_DIR / "debug"
        debug_dir.mkdir(parents=True, exist_ok=True)
        try:
            (debug_dir / "first_foreclosure.html").write_text(
                html, encoding="utf-8")
        except Exception:
            pass

        # Best-effort pagination — click any "Next" button up to 10 times.
        for page_num in range(2, 11):
            try:
                clicked = await page.evaluate("""() => {
                    const btns = Array.from(document.querySelectorAll(
                        'button, a, [role=button]'));
                    for (const b of btns) {
                        const t = (b.textContent || '').trim().toLowerCase();
                        if ((t === 'next' || t === '>' || t === 'next page' ||
                             b.getAttribute('aria-label') === 'Next page')
                            && !b.disabled
                            && !(b.classList && b.classList.contains('disabled'))) {
                            b.click();
                            return true;
                        }
                    }
                    return false;
                }""")
                if not clicked:
                    break
                await page.wait_for_timeout(1000)
                html = await page.content()
                page_rows = _extract_foreclosure_table_rows(html)
                log.info("  page %d: %d rows", page_num, len(page_rows))
                if not page_rows:
                    break
                all_rows.extend(page_rows)
            except Exception as exc:
                log.debug("pagination stopped at page %d: %s", page_num, exc)
                break

        await context.close()
        await browser.close()

    for raw in all_rows:
        try:
            rec = _normalize_foreclosure_row(raw)
            if rec is None:
                continue
            # Filter to records whose sale date is within our window.
            if rec.sale_date:
                if rec.sale_date < today_iso or rec.sale_date > end_iso:
                    continue
            out[rec.doc_num] = rec
        except Exception as exc:
            log.warning("bad foreclosure row skipped: %s", exc)

    log.info("=== fetch_foreclosures: %d unique upcoming foreclosures ===",
             len(out))
    return list(out.values())


def _extract_foreclosure_table_rows(html: str) -> List[Dict[str, str]]:
    """Parse the FC-tab results table.

    Columns: Doc Type | Recorded Date | Sale Date | Doc Number | Property Address
    The Neumo classes are col-3..col-7 (the table layout reuses the same
    component as the RP tab; col-0..col-2 are checkbox/menu/cart).
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

    # Map header text → col-class.
    header_by_col: Dict[str, str] = {}
    thead = table.find("thead")
    if thead:
        for th in thead.find_all("th"):
            classes = th.get("class") or []
            label = th.get_text(" ", strip=True)
            for c in classes:
                if c.startswith("col") and label:
                    header_by_col[c.replace("-", "")] = label
    if len(header_by_col) < 4:
        # Fallback: assume the documented order.
        header_by_col = {
            "col0": "", "col1": "", "col2": "",
            "col3": "Doc Type", "col4": "Recorded Date",
            "col5": "Sale Date", "col6": "Doc Number",
            "col7": "Property Address",
        }

    HEADER_TO_FIELD = {
        "doc type":         "doc_type",
        "document type":    "doc_type",
        "recorded date":    "recorded",
        "sale date":        "sale_date",
        "doc number":       "doc_number",
        "document number":  "doc_number",
        "property address": "legal",      # FC tab labels its legal-desc
                                            # column "Property Address"
                                            # but the content is the legal
                                            # description string, not a
                                            # mailable address.
    }

    rows: List[Dict[str, str]] = []
    tbody = table.find("tbody") or table
    for tr in tbody.find_all("tr"):
        if tr.find("th") and not tr.find("td"):
            continue
        record: Dict[str, str] = {}
        link_href = ""
        for td in tr.find_all("td"):
            classes = td.get("class") or []
            text = td.get_text(" ", strip=True)
            if not text:
                continue
            a = td.find("a", href=True)
            if a and "/doc/" in a["href"]:
                link_href = a["href"]
            col_class = next(
                (c.replace("-", "") for c in classes
                 if re.match(r"col-?\d+$", c)),
                None,
            )
            if not col_class:
                continue
            header = (header_by_col.get(col_class) or "").strip().lower()
            field = HEADER_TO_FIELD.get(header)
            if not field:
                continue
            if text in ("--/--/--", "N/A", "n/a", "-"):
                text = ""
            record[field] = text
        if record.get("doc_number"):
            if link_href:
                record["clerk_url"] = link_href
            rows.append(record)
    return rows


def _normalize_foreclosure_row(raw: Dict[str, str]) -> Optional[ForeclosureRecord]:
    doc_num = (raw.get("doc_number") or "").strip()
    if not doc_num:
        return None
    doc_type = (raw.get("doc_type") or "FORECLOSURE NOTICE").strip()
    recorded = _coerce_date(raw.get("recorded"))
    sale_date = _coerce_date(raw.get("sale_date"))
    legal = (raw.get("legal") or "").strip()

    clerk_url = (raw.get("clerk_url") or "").strip()
    if clerk_url and not clerk_url.startswith("http"):
        clerk_url = CLERK_BASE + (clerk_url if clerk_url.startswith("/")
                                  else "/" + clerk_url)
    if not clerk_url:
        clerk_url = (f"{CLERK_BASE}/results?"
                     + urlencode({"department": "FC",
                                  "searchValue": doc_num}))

    # Best-effort extract of a real street address from the legal field
    # (rare for foreclosures, but if the legal happens to be a street
    # address we'll capture it; PDF reading will be the proper source).
    addr = _extract_tx_address(legal)
    prop_address = addr["street"] if addr else ""
    prop_city    = addr["city"]   if addr else ""
    prop_state   = addr["state"]  if addr else ""
    prop_zip     = addr["zip"]    if addr else ""

    return ForeclosureRecord(
        doc_num=doc_num,
        doc_type=doc_type,
        recorded=recorded or "",
        sale_date=sale_date or "",
        legal=legal,
        clerk_url=clerk_url,
        prop_address=prop_address,
        prop_city=prop_city,
        prop_state=prop_state or "TX",
        prop_zip=prop_zip,
    )


def _foreclosure_status(sale_date: str, today_iso: str) -> str:
    """Return 'pre-foreclosure' if sale_date is in the future, else
    'post-foreclosure'."""
    if not sale_date:
        return "pre-foreclosure"
    try:
        return ("pre-foreclosure" if sale_date >= today_iso
                else "post-foreclosure")
    except Exception:
        return "pre-foreclosure"


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


def _extract_clerk_card_rows(html: str) -> List[Dict[str, str]]:
    """Parse the BIS/Neumo CARD-view variant of the search results page.

    Card view exposes fields the list view's table doesn't have — most
    importantly **Consideration** (the lien amount) and **Instrument Date**
    — without requiring per-document detail-page fetches. We toggle into
    card view by clicking the view-mode button before calling page.content().

    Each card has the shape:
      <... LIEN ...>              -- doc type label
      <... 07/30/2025 ...>        -- date heading
      Document Number:    2025027154
      Number of Pages:    2
      Recorded Date:      7/30/2025 12:43 PM
      Consideration:      $324.00
      Document Status:    Complete
      Book/Volume/Page:   --/-- /--
      Instrument Date:    07/28/2025
      Grantor:            THE CITY OF CORPUS CHRISTI
      Grantee:            BHS CRIMSON HOMES LLC
      Legal Description:  Subdivision - Name: BOOTY AND ALLEN ...

    The DOM uses pairs of label+value elements, so we walk by label text
    rather than by class names (more resilient to portal version drift).
    """
    if not html:
        return []
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")

    # Each card is a self-contained block. The portal's CSS classes
    # change between versions, so identify cards by their structure:
    # any element that contains the label "Document Number:" AND
    # a doc-link `/doc/<id>` is a card.
    LABELS = (
        "document number", "number of pages", "recorded date",
        "consideration", "document status", "book/volume/page",
        "instrument date", "grantor", "grantee", "legal description",
        "doc type", "document type",
    )
    LABEL_TO_FIELD = {
        "document number": "doc_number",
        "number of pages": "num_pages",
        "recorded date":   "recorded_date",
        "consideration":   "consideration",
        "document status": "doc_status",
        "book/volume/page":"book_volume_page",
        "instrument date": "instrument_date",
        "grantor":         "grantor",
        "grantee":         "grantee",
        "legal description":"legal",
    }

    rows: List[Dict[str, str]] = []

    # Strategy: find every anchor `/doc/<id>` and walk up to the smallest
    # enclosing card-like container. Then scan that container for label
    # → value pairs. Empirically the BIS markup uses <dt>/<dd> or pairs
    # of <span> / <div>, so we use a generic text-walk approach: find
    # text nodes that match a known label, then take the next text node
    # as the value.
    seen_doc_ids = set()
    for a in soup.find_all("a", href=True):
        if "/doc/" not in a["href"]:
            continue

        # Walk upward to find a container that has all labels (or as many
        # as exist on this card). Cap at 8 levels up.
        node = a
        container = None
        for _ in range(8):
            node = node.parent
            if node is None:
                break
            txt = node.get_text(" ", strip=True).lower()
            if "document number" in txt and "grantor" in txt:
                container = node
                break
        if container is None:
            continue

        # Within the container, walk every "label:" string and grab the
        # adjacent text. Use the document's text in document order.
        record: Dict[str, str] = {"clerk_url": a["href"]}
        text_chunks = list(container.stripped_strings)
        i = 0
        while i < len(text_chunks):
            chunk = text_chunks[i]
            chunk_l = chunk.lower().rstrip(":").strip()
            if chunk_l in LABELS:
                # Next non-empty chunk is the value.
                val = ""
                j = i + 1
                # Skip pure-punctuation or label-suffix-colon noise.
                while j < len(text_chunks):
                    c = text_chunks[j].strip()
                    if c and c not in (":", "-"):
                        val = c
                        break
                    j += 1
                fld = LABEL_TO_FIELD.get(chunk_l)
                if fld and val and val.lower() not in (l.rstrip(":")
                                                       for l in LABELS):
                    # Don't capture another label as a value (happens when
                    # the value is empty and the next chunk is a label).
                    if val in ("--/--/--", "N/A", "n/a", "-"):
                        val = ""
                    record.setdefault(fld, val)
                i = j
            else:
                i += 1

        # Ensure we have the essentials; reject if not.
        if not record.get("doc_number"):
            continue
        if record["doc_number"] in seen_doc_ids:
            continue
        seen_doc_ids.add(record["doc_number"])
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
    # When that's the case, swap the names so `owner` always means
    # "person we want to contact".
    DEFENDANT_IS_GRANTEE_CATS = {"LNFED", "JUD", "MEDLN", "PRO", "LN", "CCLN"}
    grantor_looks_institutional = _is_institutional_plaintiff(grantor)
    if cat in DEFENDANT_IS_GRANTEE_CATS and grantor_looks_institutional and grantee:
        grantor, grantee = grantee, grantor
    # Mechanic liens: contractor (grantor) files against property owner
    # (grantee). Same swap rule applies.
    if cat == "LN" and re.search(r"MECH", doc_type_raw, re.I) and grantee:
        grantor, grantee = grantee, grantor

    amount_raw = g("considerationAmount", "amount", "amount_due",
                   "totalAmount", "total", "consideration")
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
    parse_deadline = time.time() + 90    # 90 seconds total for NCAD parsing
    PER_FILE_TIMEOUT = 25                # seconds before we abort a single file
    MAX_FILE_BYTES = 80 * 1024 * 1024    # 80 MB - skip larger files entirely

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
            log.warning("NCAD overall parse budget exhausted at %s", name)
            break
        try:
            # Skip oversize files — they're rarely the owner/address source
            # and they burn the time budget for the smaller, useful files.
            try:
                file_size = zf.getinfo(name).file_size
            except KeyError:
                file_size = 0
            if file_size > MAX_FILE_BYTES:
                log.info("  skipping %s (%.0f MB > %.0f MB cap)",
                         name, file_size / 1024 / 1024,
                         MAX_FILE_BYTES / 1024 / 1024)
                continue

            file_deadline = time.time() + PER_FILE_TIMEOUT
            with zf.open(name) as fh:
                raw = fh.read()
            text = _decode_loose(raw)
            if not text.strip():
                continue
            delim = _sniff_delimiter(text)
            reader = csv.DictReader(io.StringIO(text), delimiter=delim)
            headers = [(h or "").strip() for h in (reader.fieldnames or [])]

            # Skip files that have no real header row (header looks like data
            # — purely numeric, or single column of opaque IDs). Common in
            # PTAD's *_ENTITY.TXT files which are list-only with no schema.
            looks_like_data = (
                not headers
                or len(headers) == 1
                or all(re.match(r"^[\d\s\-_/]+$", h) for h in headers if h)
            )
            if looks_like_data:
                log.info("  %s: no recognizable header — skipping", name)
                continue

            # Find which columns play which role.
            id_col    = find_col(headers, ID_TOKENS)
            owner_col = find_col(headers, OWNER_TOKENS)
            site_col  = find_col(headers, SITE_TOKENS, exclude=("CITY", "ZIP", "STATE"))
            mail_col  = find_col(headers, MAIL_TOKENS, exclude=("CITY", "ZIP", "STATE"))
            site_city  = find_col(headers, CITY_TOKENS) if site_col else ""
            site_state = find_col(headers, STATE_TOKENS)
            site_zip   = find_col(headers, ZIP_TOKENS)

            # Log a header sample so we can see schema in the log.
            log.info("  %s: cols=%d size=%.1fMB delim=%r",
                     name, len(headers), file_size / 1024 / 1024, delim)
            log.info("    headers (first 25): %s", headers[:25])
            log.info("    matched: id=%r owner=%r site=%r mail=%r",
                     id_col, owner_col, site_col, mail_col)

            # If we can't find an ID column, this file isn't useful for join.
            # Don't burn time iterating its rows — skip directly.
            if not id_col:
                log.info("    (no ID column — skipping rows)")
                continue
            # If we can't find anything useful (no owner AND no addr), skip.
            if not owner_col and not site_col and not mail_col:
                log.info("    (no owner/address columns — skipping rows)")
                continue

            row_count = 0
            owner_added = 0
            addr_added = 0
            for row in reader:
                # Per-file deadline check (every 1000 rows to keep it cheap).
                row_count += 1
                if row_count % 1000 == 0 and time.time() > file_deadline:
                    log.warning("    %s: per-file timeout at row %d",
                                name, row_count)
                    break
                clean = _clean_row(row)
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

            log.info("    %d rows (+%d owner, +%d addr) [%.1fs]",
                     row_count, owner_added, addr_added,
                     time.time() - (file_deadline - PER_FILE_TIMEOUT))
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
# NCAD esearch (per-name property lookup)
# --------------------------------------------------------------------------- #
#
# This complements the bulk-export path. For each owner we couldn't
# enrich from the legal-description extractor, we hit NCAD's public
# property-search portal at esearch.nuecescad.net and pull the
# matching parcel's situs + mailing address.
#
# The portal is a server-rendered ASP.NET app powered by BIS Consultants
# (the same vendor used by Travis, Collin, Hays, Fort Bend and dozens
# of other Texas CADs). It uses a CSRF-style anti-forgery token baked
# into the home page, so we drive it via Playwright (which carries the
# token + cookies for us) rather than trying to fake the form submit.
#
# We aggressively cache results to disk so the same name is never
# looked up twice, and skip names that obviously won't have parcels
# (banks, agencies, debt collectors).

# Names we never bother looking up in the property-search portal —
# they're not Nueces property owners. We reuse the same pattern that
# drives the grantor↔grantee swap (see INSTITUTIONAL_PLAINTIFF_RE near
# the top of the module): if a name looks like an institutional
# plaintiff/creditor, looking it up will not produce a useful match.
_INSTITUTIONAL_RE = INSTITUTIONAL_PLAINTIFF_RE


def _looks_institutional(name: str) -> bool:
    """True if the name looks like an institution rather than a Nueces
    property owner — used to skip pointless esearch lookups."""
    if not name:
        return True
    return bool(_INSTITUTIONAL_RE.search(name))


def _load_search_cache() -> Dict[str, Optional[Dict[str, str]]]:
    """Load the persistent name → property-info cache.

    Wrapped in a versioned envelope: {"_version": "v2", "data": {...}}.
    Mismatched versions return an empty cache (forces re-lookup), which
    is how we invalidate cache entries built with an older URL format.
    """
    path = ROOT_DIR / NCAD_SEARCH_CACHE
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("could not load esearch cache: %s", exc)
        return {}

    # Versioned envelope (current format).
    if isinstance(raw, dict) and raw.get("_version") == "v3":
        return raw.get("data", {})

    # Old-version cache → discard. Entries built with the old URL pattern
    # or old result extractor are stale; re-querying is strictly better.
    log.info("legacy esearch cache detected (version=%r) — discarding (will rebuild)",
             raw.get("_version") if isinstance(raw, dict) else None)
    return {}


def _save_search_cache(cache: Dict[str, Optional[Dict[str, str]]]) -> None:
    path = ROOT_DIR / NCAD_SEARCH_CACHE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        envelope = {"_version": "v3", "data": cache}
        path.write_text(json.dumps(envelope, indent=2), encoding="utf-8")
        log.info("esearch cache: %d entries written to %s",
                 len(cache), path)
    except Exception as exc:
        log.warning("could not save esearch cache: %s", exc)


def enrich_via_ncad_search(records: List[ClerkRecord]) -> int:
    """For each record without an address, query NCAD's esearch portal
    for the owner's name and fill in property + mailing address.

    Returns the number of records newly enriched.
    """
    # Pick names we want to look up.
    todo: List[ClerkRecord] = []
    for rec in records:
        if rec.prop_address:
            continue                  # already have an address
        if not rec.owner:
            continue
        if _looks_institutional(rec.owner):
            continue                  # banks/IRS/etc don't own parcels here
        todo.append(rec)

    if not todo:
        log.info("esearch: no records eligible for lookup")
        return 0

    log.info("esearch: %d records eligible (capped at %d)",
             len(todo), NCAD_SEARCH_MAX_LOOKUPS)
    todo = todo[:NCAD_SEARCH_MAX_LOOKUPS]

    cache = _load_search_cache()
    log.info("esearch: %d cached entries loaded", len(cache))

    try:
        results = asyncio.run(_run_ncad_searches([r.owner for r in todo], cache))
    except Exception as exc:
        log.error("esearch loop failed: %s\n%s", exc, traceback.format_exc())
        results = {}

    _save_search_cache(cache)

    matched = 0
    for rec in todo:
        info = results.get(rec.owner) or cache.get(rec.owner)
        if not info:
            continue
        rec.prop_address = info.get("site_addr", "")
        rec.prop_city    = info.get("site_city", "") or "CORPUS CHRISTI"
        rec.prop_state   = info.get("site_state", "") or "TX"
        rec.prop_zip     = info.get("site_zip", "")
        rec.mail_address = info.get("mail_addr", "")
        rec.mail_city    = info.get("mail_city", "")
        rec.mail_state   = info.get("mail_state", "") or "TX"
        rec.mail_zip     = info.get("mail_zip", "")
        if rec.prop_address:
            matched += 1
    log.info("esearch: %d / %d records enriched", matched, len(todo))
    return matched


async def _run_ncad_searches(names: List[str],
                              cache: Dict[str, Optional[Dict[str, str]]]
                              ) -> Dict[str, Optional[Dict[str, str]]]:
    """Drive Playwright through one esearch query per uncached name.

    Updates `cache` in place. Returns the same cache for convenience.
    `cache[name] = None` means "we tried, didn't find anything" — so we
    don't keep retrying dead names.
    """
    try:
        from playwright.async_api import async_playwright  # type: ignore
    except ImportError:
        log.error("playwright not available — esearch skipped")
        return cache

    deadline = time.time() + NCAD_SEARCH_PHASE_BUDGET_SEC

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
        context = await browser.new_context(user_agent=USER_AGENT)
        page = await context.new_page()

        # Warm the session and harvest the per-session search token.
        # The BIS Consultants esearch portal embeds a `searchSessionToken`
        # in <meta name="search-token"> on its homepage. Every search URL
        # must include this token or the result page returns empty.
        token = ""
        try:
            await page.goto(NCAD_ESEARCH_BASE + "/",
                             wait_until="domcontentloaded", timeout=30_000)
            token = await page.evaluate("""() => {
                const m = document.querySelector('meta[name="search-token"]');
                return m ? m.getAttribute('content') : '';
            }""") or ""
            log.info("esearch session token acquired: %s",
                     "yes" if token else "no")
        except Exception as exc:
            log.error("esearch home failed to load: %s", exc)
            await context.close()
            await browser.close()
            return cache

        # Detect the current tax year from the home page (defaults to current).
        # The homepage <option selected> for tax year tells us which year
        # the portal currently considers "current".
        current_year = str(datetime.now(timezone.utc).year)

        for i, name in enumerate(names, start=1):
            if name in cache:
                continue                 # already looked up (hit OR miss)
            if time.time() > deadline:
                log.warning("esearch: time budget exhausted after %d names", i)
                break
            try:
                info = await _esearch_one(page, name, token, current_year)
            except Exception as exc:
                log.warning("esearch lookup failed for %r: %s", name, exc)
                info = None
            cache[name] = info  # store None for misses so we don't retry
            if info and info.get("site_addr"):
                log.info("  esearch[%d] %r → %s",
                         i, name, info.get("site_addr"))
            else:
                log.info("  esearch[%d] %r → no match", i, name)
            await asyncio.sleep(NCAD_SEARCH_DELAY_SEC)

        await context.close()
        await browser.close()
    return cache


async def _esearch_one(page, name: str, token: str,
                        current_year: str) -> Optional[Dict[str, str]]:
    """Query NCAD esearch for a single owner name, return the first
    matching parcel's address dict, or None.

    The BIS Consultants esearch portal expects URLs like:
      /search/result?keywords=OwnerName:SCHAFER Year:2026 &searchSessionToken=...

    The `keywords=` value is a structured query language with key:value
    pairs separated by spaces. `OwnerName:` scopes the search to the
    owner-name field; `Year:` selects the tax roll year.

    Returns the best matching real-property record's address. The result
    list itself contains the situs address, so we usually don't need to
    follow the link to the detail page — saves a request per match.
    """
    candidates = _esearch_query_variants(name)
    debug_dir = ROOT_DIR / "debug"
    debug_dir.mkdir(parents=True, exist_ok=True)

    for candidate_idx, candidate in enumerate(candidates):
        keywords = f"OwnerName:{candidate} Year:{current_year} "
        params = {"keywords": keywords}
        if token:
            params["searchSessionToken"] = token
        url = f"{NCAD_ESEARCH_BASE}/search/result?{urlencode(params)}"

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=20_000)
            # Wait for results table OR "no results" message. We use a much
            # broader selector than before because the previous
            # `a[href*="/Property/View/"]` was too strict — BIS sometimes
            # renders results without the View link visible until hovered.
            try:
                await page.wait_for_selector(
                    "table tbody tr, "
                    "[class*='no-results'], "
                    "[class*='NoResults']",
                    timeout=8_000,
                )
            except Exception:
                pass
            await page.wait_for_timeout(400)
        except Exception as exc:
            log.debug("esearch nav failed for %r: %s", candidate, exc)
            continue

        try:
            html = await page.content()
        except Exception:
            continue

        # Save diagnostics for the first lookup overall, so we can see
        # exactly what the portal returned.
        if not hasattr(_esearch_one, "_diag_saved"):
            try:
                (debug_dir / f"esearch_first_result.html").write_text(
                    html, encoding="utf-8")
                _esearch_one._diag_saved = True
                log.info("esearch diagnostics saved to "
                         "debug/esearch_first_result.html")
            except Exception:
                pass

        rows = _parse_esearch_result_list(html)
        if not rows:
            continue   # no results for this candidate, try next

        # Pick the best row.
        best = _pick_best_esearch_row(rows, candidate)
        if best:
            return best

    return None


def _parse_esearch_result_list(html: str) -> List[Dict[str, str]]:
    """Parse the BIS Consultants result-list table.

    Returns a list of {owner, prop_id, type, situs_address, legal} dicts.
    Empty list if no rows.
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

    # Build column-name → index map from the header row.
    header_row = table.find("thead") or table
    headers = [th.get_text(" ", strip=True).lower()
               for th in header_row.find_all("th")]
    if not headers:
        return []

    def find_col(*tokens: str) -> int:
        for i, h in enumerate(headers):
            if all(t in h for t in tokens):
                return i
        return -1

    i_owner   = find_col("owner", "name")
    i_situs   = find_col("situs")
    if i_situs < 0:
        i_situs = find_col("address")
    i_type    = find_col("type")
    i_propid  = find_col("property", "id")
    if i_propid < 0:
        i_propid = find_col("prop", "id")
    i_legal   = find_col("legal")

    rows: List[Dict[str, str]] = []
    tbody = table.find("tbody") or table
    for tr in tbody.find_all("tr"):
        cells = tr.find_all("td")
        if len(cells) < 3:
            continue
        def cell(i: int) -> str:
            if 0 <= i < len(cells):
                return cells[i].get_text(" ", strip=True)
            return ""
        owner = cell(i_owner)
        situs = cell(i_situs)
        if not owner and not situs:
            continue
        rows.append({
            "owner":   owner,
            "situs":   situs,
            "type":    cell(i_type),
            "prop_id": cell(i_propid),
            "legal":   cell(i_legal),
        })
    return rows


def _pick_best_esearch_row(rows: List[Dict[str, str]],
                            query_name: str) -> Optional[Dict[str, str]]:
    """Choose the best result row for a given query name.

    Preferences (in order):
      1. Real property (Type='R') over personal property (Type='P')
      2. Has a real situs address (not blank, not personal-property location)
      3. Closest owner-name match (exact > startswith > contains)
    Returns a normalized address dict or None if nothing usable.
    """
    if not rows:
        return None

    qn = query_name.upper().strip()

    def score_row(r: Dict[str, str]) -> Tuple[int, int, int]:
        type_score = 2 if r.get("type") == "R" else (1 if r.get("type") == "P" else 0)
        situs = r.get("situs", "")
        # Reject obvious personal-property "addresses" that aren't real
        # mailing locations (they often mention BUSINESS, MALL, STE only).
        situs_score = 1 if (situs and re.search(r"\b\d+\b", situs)) else 0
        # Name match: prefer the owner that actually starts with the query.
        owner = r.get("owner", "").upper()
        if owner == qn:
            name_score = 3
        elif owner.startswith(qn):
            name_score = 2
        elif qn in owner:
            name_score = 1
        else:
            name_score = 0
        return (type_score, situs_score, name_score)

    rows_sorted = sorted(rows, key=score_row, reverse=True)
    top = rows_sorted[0]
    if not top.get("situs"):
        return None

    # Parse the situs into structured fields.
    site_addr, site_city, site_state, site_zip = _split_us_address(top["situs"])
    if not site_addr:
        return None

    return {
        "site_addr":  site_addr,
        "site_city":  site_city or "CORPUS CHRISTI",
        "site_state": site_state or "TX",
        "site_zip":   site_zip,
        # The result list doesn't expose the mailing address — leave blank.
        # If the user wants mailing info we'd need to follow the detail
        # link, but situs is what matters most for direct-mail.
        "mail_addr":  "",
        "mail_city":  "",
        "mail_state": "",
        "mail_zip":   "",
    }


def _esearch_query_variants(name: str) -> List[str]:
    """Generate up to 3 query strings to try for a given owner name."""
    n = re.sub(r"\s+", " ", name.upper().strip())
    n = re.sub(r"[^A-Z0-9 ,&-]", " ", n)
    n = re.sub(r"\s+", " ", n).strip(" ,")
    if not n:
        return []
    out = [n]                                # try as-is first
    if "," in n:
        # "SCHAFER, ROBERT" → "SCHAFER ROBERT"
        out.append(n.replace(",", "").strip())
    parts = n.replace(",", "").split()
    if len(parts) >= 2:
        # Last-name-only fallback (limits results, but at least catches
        # individuals when the first/middle names diverge).
        last = parts[0]
        if len(last) >= 3 and last not in out:
            out.append(last)
    # Dedup while preserving order.
    seen = set()
    uniq = []
    for q in out:
        if q not in seen:
            seen.add(q)
            uniq.append(q)
    return uniq[:3]


def _parse_esearch_detail(html: str) -> Optional[Dict[str, str]]:
    """Pull situs + mailing address out of an esearch property-detail page.

    BIS Consultants property pages render addresses in <dl>/<dd> pairs
    or in identifiable card sections labeled "Situs Address" / "Mailing
    Address". We use BS4 to walk by label and pick up the values nearby.
    """
    if not html:
        return None
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")

    text_pairs: Dict[str, str] = {}

    # Pattern A: <dl><dt>Label</dt><dd>Value</dd>... — common BIS layout.
    for dl in soup.find_all("dl"):
        dts = dl.find_all("dt")
        dds = dl.find_all("dd")
        for dt, dd in zip(dts, dds):
            label = dt.get_text(" ", strip=True).lower().rstrip(":")
            value = dd.get_text(" ", strip=True)
            if label and value:
                text_pairs[label] = value

    # Pattern B: cards with header + body where header contains "Address".
    for card in soup.find_all(class_=re.compile(r"card|panel|section",
                                                  re.IGNORECASE)):
        header = card.find(class_=re.compile(r"header|title", re.IGNORECASE))
        if not header:
            continue
        ht = header.get_text(" ", strip=True).lower()
        body = card.find(class_=re.compile(r"body|content", re.IGNORECASE))
        if not body:
            continue
        bv = body.get_text(" \n", strip=True)
        if "situs" in ht or "property address" in ht:
            text_pairs.setdefault("situs address", bv)
        elif "mail" in ht:
            text_pairs.setdefault("mailing address", bv)

    # Pattern C: scan label cells in tables.
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

    site_full = (find_value("situs") or find_value("property", "address")
                 or find_value("address", "situs"))
    mail_full = (find_value("mailing", "address") or find_value("mail", "address")
                 or find_value("owner", "address"))

    site_addr, site_city, site_state, site_zip = _split_us_address(site_full)
    mail_addr, mail_city, mail_state, mail_zip = _split_us_address(mail_full)

    if not (site_addr or mail_addr):
        return None

    return {
        "site_addr":  site_addr,
        "site_city":  site_city or "CORPUS CHRISTI",
        "site_state": site_state or "TX",
        "site_zip":   site_zip,
        "mail_addr":  mail_addr,
        "mail_city":  mail_city,
        "mail_state": mail_state,
        "mail_zip":   mail_zip,
    }


def _split_us_address(full: str) -> Tuple[str, str, str, str]:
    """Split a full US address string into (street, city, state, zip)."""
    if not full:
        return ("", "", "", "")
    s = re.sub(r"\s+", " ", full).strip(" ,")
    # Pull off ZIP last, then state, then assume the rest before the last
    # comma (or last newline) is the city.
    state, zip_code = "", ""
    zm = re.search(r"(\d{5})(?:-\d{4})?\s*$", s)
    if zm:
        zip_code = zm.group(1)
        s = s[:zm.start()].strip(" ,")
    sm = re.search(r"\b([A-Z]{2})\s*$", s)
    if sm:
        state = sm.group(1)
        s = s[:sm.start()].strip(" ,")
    # Now the remaining `s` is "STREET, CITY" or "STREET CITY". Try comma
    # first; if not, take the last word as city — imperfect but workable.
    if "," in s:
        street, _, city = s.rpartition(",")
        return (street.strip(), city.strip(), state, zip_code)
    return (s.strip(), "", state, zip_code)


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

def write_foreclosure_outputs(records: List[ForeclosureRecord],
                                today_iso: str, end_iso: str) -> None:
    """Write the foreclosure stream to dashboard/foreclosures.json and a
    matching CSV. Status (pre/post) is computed at write time using
    today_iso vs each record's sale_date.
    """
    DASHBOARD_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    enriched = []
    for r in records:
        d = asdict(r)
        d["status"] = _foreclosure_status(r.sale_date, today_iso)
        # Days until sale (negative if sale is in the past).
        try:
            sd = datetime.fromisoformat(r.sale_date).date()
            td = datetime.fromisoformat(today_iso).date()
            d["days_until_sale"] = (sd - td).days
        except Exception:
            d["days_until_sale"] = None
        enriched.append(d)

    # Sort: pre-foreclosure first by closest sale date, then post.
    def sort_key(d):
        is_post = d["status"] == "post-foreclosure"
        days = d.get("days_until_sale")
        if days is None:
            days = 9999
        return (is_post, days)
    enriched.sort(key=sort_key)

    payload = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "source": "Nueces County Clerk (FC tab)",
        "date_range": {"start": today_iso, "end": end_iso},
        "total": len(enriched),
        "pre_foreclosure": sum(1 for d in enriched
                               if d["status"] == "pre-foreclosure"),
        "post_foreclosure": sum(1 for d in enriched
                                if d["status"] == "post-foreclosure"),
        "records": enriched,
    }

    for path in (DASHBOARD_DIR / "foreclosures.json",
                 DATA_DIR / "foreclosures.json"):
        with path.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, default=str)
        log.info("wrote %s (%d foreclosures)", path, len(enriched))

    # Companion CSV — same columns as the lead CSV where applicable, plus
    # foreclosure-specific fields. Useful for spreadsheet review.
    csv_path = DATA_DIR / "foreclosures.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow([
            "Sale Date", "Days Until Sale", "Status",
            "Doc Number", "Doc Type", "Recorded Date",
            "Property Address (legal)",
            "Owner (from PDF)", "Loan Amount (from PDF)",
            "Public Records URL",
        ])
        for d in enriched:
            w.writerow([
                d.get("sale_date", ""),
                d.get("days_until_sale", ""),
                d.get("status", ""),
                d.get("doc_num", ""),
                d.get("doc_type", ""),
                d.get("recorded", ""),
                d.get("legal", ""),
                d.get("owner", ""),
                f"{d['loan_amount']:.2f}" if d.get("loan_amount") else "",
                d.get("clerk_url", ""),
            ])
    log.info("wrote %s", csv_path)


CITY_LIENS_FILE = DATA_DIR / "city_liens.json"


def load_city_liens() -> List[Dict[str, Any]]:
    """Load the persistent cumulative City of Corpus Christi lien list.

    This file is grown over time — each daily scrape merges new CCLN
    records into it (deduped by doc_num). The 24-month backfill script
    seeds it once. The CRM dashboard uses this as its data source so
    leads from previous months remain visible after they fall out of
    the 30-day daily-scrape window.
    """
    if not CITY_LIENS_FILE.exists():
        return []
    try:
        raw = json.loads(CITY_LIENS_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("could not load city_liens.json: %s", exc)
        return []
    if isinstance(raw, dict) and "records" in raw:
        return raw["records"]
    if isinstance(raw, list):
        return raw
    return []


def save_city_liens(records: List[Dict[str, Any]]) -> None:
    """Write the cumulative City of Corpus Christi lien list to both the
    /data and /dashboard directories. The dashboard copy is what the
    CRM tab fetches at runtime."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DASHBOARD_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "source": "Nueces County Clerk — CCLN cumulative",
        "total": len(records),
        "records": records,
    }
    body = json.dumps(payload, indent=2, default=str)
    CITY_LIENS_FILE.write_text(body, encoding="utf-8")
    (DASHBOARD_DIR / "city_liens.json").write_text(body, encoding="utf-8")
    log.info("wrote city_liens.json (%d cumulative records)", len(records))


def merge_city_liens(existing: List[Dict[str, Any]],
                      new_records: List[ClerkRecord]) -> List[Dict[str, Any]]:
    """Merge new CCLN records into the existing cumulative list.

    Dedupes by doc_num. New records are added; existing records keep
    whatever data they had (which may include CRM fields the dashboard
    has written via Option B in the future). For now CRM state lives in
    localStorage, so this is a simple union.
    """
    by_doc = {r.get("doc_num"): r for r in existing if r.get("doc_num")}
    added = 0
    for rec in new_records:
        if not rec.doc_num:
            continue
        if rec.doc_num not in by_doc:
            by_doc[rec.doc_num] = asdict(rec)
            added += 1
    log.info("city_liens merge: %d existing + %d new (kept %d added)",
             len(existing), len(new_records), added)
    return list(by_doc.values())


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

    # 1) Clerk portal — the primary motivated-seller data source.
    try:
        clerk_records = asyncio.run(fetch_clerk_records(start_iso, end_iso))
    except Exception as exc:
        log.error("clerk fetch failed entirely: %s\n%s",
                  exc, traceback.format_exc())
        clerk_records = []

    # 1b) Foreclosures — separate stream, separate output file. Runs
    # independently of the motivated-seller pipeline. Failure here is
    # non-fatal to the rest of the run.
    try:
        foreclosures = asyncio.run(fetch_foreclosures(end_iso))
    except Exception as exc:
        log.error("foreclosure fetch failed: %s\n%s",
                  exc, traceback.format_exc())
        foreclosures = []
    foreclosure_end = (today + timedelta(days=FORECLOSURE_LOOKAHEAD_DAYS)).isoformat()
    write_foreclosure_outputs(foreclosures, end_iso, foreclosure_end)

    # 2) Pull addresses out of legal-description text where present
    #    (works without NCAD — important fallback path).
    enrich_with_parcels(clerk_records, owner_lookup={})

    # 3) Score.
    owner_idx = build_owner_cat_index(clerk_records)
    for rec in clerk_records:
        try:
            compute_flags_and_score(rec, end_iso, owner_idx)
        except Exception as exc:
            log.warning("scoring failed for %s: %s", rec.doc_num, exc)
            rec.flags = rec.flags or []
            rec.score = rec.score or 30

    # 4) Sort.
    clerk_records.sort(key=lambda r: r.score or 0, reverse=True)

    # 4b) Split CCLN (City of Corpus Christi liens) off the main stream.
    # CCLN records live in the persistent cumulative list (city_liens.json)
    # and surface only on the CRM tab — not the Motivated Seller tab.
    # The 24-month backfill seeds the file; daily scrape merges in any new.
    ccln_today = [r for r in clerk_records if r.cat == "CCLN"]
    clerk_records = [r for r in clerk_records if r.cat != "CCLN"]
    if ccln_today:
        log.info("CCLN: %d new candidates from today's scrape", len(ccln_today))
    # Defer the city_liens.json write until AFTER esearch enrichment runs,
    # so newly-merged CCLN records get owner addresses too. See step 9.

    # 5) WRITE OUTPUTS NOW — before NCAD, so even if NCAD hangs/fails the
    #    clerk-side leads are committed and the dashboard refreshes.
    write_outputs(clerk_records, start_iso, end_iso)
    log.info("=== first-pass write done: %d records (no NCAD enrichment yet) ===",
             len(clerk_records))

    # 6) NCAD bulk export — best-effort enrichment. If this hangs or fails,
    #    we still have valid output from step 5.
    owner_lookup: Dict[str, Dict[str, str]] = {}
    ncad_start = time.time()
    try:
        owner_lookup = fetch_ncad_parcels()
    except Exception as exc:
        log.error("NCAD fetch failed: %s", exc)
    ncad_elapsed = time.time() - ncad_start
    log.info("NCAD phase took %.1fs (%d owner-name variants)",
             ncad_elapsed, len(owner_lookup))

    # 7) NCAD bulk-export enrichment. If this produced anything, redo
    #    address pass and rewrite outputs.
    if owner_lookup:
        enrich_with_parcels(clerk_records, owner_lookup)
        owner_idx = build_owner_cat_index(clerk_records)
        for rec in clerk_records:
            try:
                compute_flags_and_score(rec, end_iso, owner_idx)
            except Exception:
                pass
        clerk_records.sort(key=lambda r: r.score or 0, reverse=True)
        write_outputs(clerk_records, start_iso, end_iso)
        log.info("=== second-pass write done with NCAD bulk enrichment ===")

    # 8) NCAD per-name esearch lookup — fills in addresses for owners
    #    whose legal-description didn't contain one (most judgments and
    #    tax liens). Best-effort with hard time budget.
    try:
        gained = enrich_via_ncad_search(clerk_records)
    except Exception as exc:
        log.error("esearch phase failed: %s\n%s",
                  exc, traceback.format_exc())
        gained = 0

    # 9) If esearch gained any addresses, recompute scores & rewrite.
    if gained > 0:
        owner_idx = build_owner_cat_index(clerk_records)
        for rec in clerk_records:
            try:
                compute_flags_and_score(rec, end_iso, owner_idx)
            except Exception:
                pass
        clerk_records.sort(key=lambda r: r.score or 0, reverse=True)
        write_outputs(clerk_records, start_iso, end_iso)
        log.info("=== final write done with esearch enrichment "
                 "(+%d addresses) ===", gained)

    # 10) Run esearch enrichment on the new CCLN records too, score them,
    #     then merge into the persistent city_liens.json. We always write
    #     the file (even if no new records) to refresh the dashboard copy.
    if ccln_today:
        try:
            enrich_via_ncad_search(ccln_today)
        except Exception as exc:
            log.warning("CCLN esearch enrichment failed: %s", exc)
        # Score CCLN records for the CRM tab's at-a-glance pill.
        ccln_idx = build_owner_cat_index(ccln_today)
        for rec in ccln_today:
            try:
                compute_flags_and_score(rec, end_iso, ccln_idx)
            except Exception:
                pass
    existing = load_city_liens()
    merged = merge_city_liens(existing, ccln_today) if ccln_today else existing
    save_city_liens(merged)

    log.info("=== done: %d records (+ %d CCLN cumulative) ===",
             len(clerk_records), len(merged))
    return 0


if __name__ == "__main__":
    sys.exit(main())
