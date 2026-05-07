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

LOOKBACK_DAYS = 7

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

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
        context = await browser.new_context(user_agent=USER_AGENT)
        page = await context.new_page()

        # Buffer for JSON responses caught on the wire.
        captured_payloads: List[Any] = []

        async def on_response(resp):
            try:
                ctype = resp.headers.get("content-type", "")
                if "json" not in ctype:
                    return
                url = resp.url
                # The exact path varies; match anything plausible.
                if not any(tok in url for tok in ("/results", "/search", "/api/")):
                    return
                # Skip our own page-navigation HTML responses.
                try:
                    body = await resp.json()
                except Exception:
                    return
                captured_payloads.append({"url": url, "body": body})
            except Exception as exc:  # pragma: no cover
                log.debug("response handler error: %s", exc)

        page.on("response", lambda r: asyncio.create_task(on_response(r)))

        for cat_def in LEAD_CATEGORIES:
            for q in cat_def["queries"]:
                url = _build_clerk_search_url(q, start_iso, end_iso)
                log.info("clerk search: cat=%s q=%r url=%s",
                         cat_def["cat"], q, url)
                captured_payloads.clear()

                try:
                    async def _go():
                        await page.goto(url, wait_until="networkidle", timeout=45_000)
                        # Give the SPA a chance to settle / load lazy data.
                        await page.wait_for_timeout(1500)
                    await with_retries_async(_go, attempts=3, base_delay=2.0)
                except Exception as exc:
                    log.error("failed to load clerk page for q=%r: %s", q, exc)
                    continue

                # Try JSON-on-the-wire first.
                rows = _extract_rows_from_payloads(captured_payloads)

                # Fallback: scrape DOM table.
                if not rows:
                    try:
                        html = await page.content()
                        rows = _extract_rows_from_html(html)
                    except Exception as exc:
                        log.warning("DOM fallback failed for q=%r: %s", q, exc)
                        rows = []

                log.info("  → %d raw rows (q=%r)", len(rows), q)

                for raw in rows:
                    try:
                        rec = _normalize_clerk_row(raw, default_cat=cat_def["cat"])
                        if rec is None:
                            continue
                        # Date filter — the API can occasionally return rows
                        # outside the requested window; trim them client-side.
                        if rec.filed and (rec.filed < start_iso or rec.filed > end_iso):
                            continue
                        seen[rec.doc_num] = rec  # dedupe by doc_num
                    except Exception as exc:
                        log.warning("bad row skipped: %s\n%s", exc, raw)
                        continue

        await context.close()
        await browser.close()

    log.info("clerk: %d unique docs in window %s..%s",
             len(seen), start_iso, end_iso)
    return list(seen.values())


def _extract_rows_from_payloads(payloads: List[Dict]) -> List[Dict]:
    """Walk every captured JSON body and yank anything that looks like a
    results array. Neumo wraps results under several different keys
    (`searchResults`, `results`, `documents`, `hits`) depending on version.
    """
    rows: List[Dict] = []
    for entry in payloads:
        body = entry.get("body")
        if body is None:
            continue
        for hits in _walk_for_lists(body):
            # Heuristic: a row is a dict with at least one of these keys.
            for item in hits:
                if not isinstance(item, dict):
                    continue
                keys = {k.lower() for k in item.keys()}
                if keys & {
                    "docnumber", "documentnumber", "instrumentnumber",
                    "doc_num", "documentid", "id"
                }:
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

    # Look for an embedded JSON blob first (common React-app pattern).
    for script in soup.find_all("script"):
        text = script.string or ""
        if "searchResults" in text or '"docNumber"' in text:
            for match in re.finditer(r"\{[^{}]*\"docNumber\"[^{}]*\}", text):
                try:
                    rows.append(json.loads(match.group(0)))
                except Exception:
                    pass

    if rows:
        return rows

    # Plain table fallback.
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


def _normalize_clerk_row(raw: Dict[str, Any], default_cat: str) -> Optional[ClerkRecord]:
    """Convert a raw clerk JSON/HTML row into a ClerkRecord.

    Tolerates wildly different schemas (camelCase, snake_case, lowercase, etc.).
    Returns None if the row is too malformed to be useful.
    """
    g = lambda *names: _first_present(raw, *names)

    doc_num = _stringy(g(
        "docNumber", "documentNumber", "instrumentNumber", "doc_num",
        "documentId", "id", "instrument number", "doc#", "doc #"
    ))
    if not doc_num:
        return None

    doc_type_raw = _stringy(g(
        "docType", "documentType", "doc_type", "type", "document type"
    ))

    filed = _coerce_date(g(
        "recordedDate", "filedDate", "fileDate", "filed", "filedate",
        "recorded date", "recorded"
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

    amount_raw = g("considerationAmount", "amount", "amount_due",
                   "totalAmount", "total")
    amount = _coerce_amount(amount_raw) or _coerce_amount(legal) or _coerce_amount(doc_type_raw)

    # Map to category code.
    cat = _classify(doc_type_raw) or default_cat
    cat_label = CAT_TO_LABEL.get(cat, default_cat)

    # Build the deep-link URL back to the document detail page.
    clerk_url = ""
    doc_id = _stringy(g("documentId", "id", "docId"))
    if doc_id:
        clerk_url = f"{CLERK_DOC_URL}/{doc_id}"
    else:
        clerk_url = (f"{CLERK_BASE}/results?"
                     + urlencode({"department": "RP", "searchValue": doc_num}))

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
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        try:
            f = float(v)
            return f if f > 0 else None
        except Exception:
            return None
    s = str(v)
    best: Optional[float] = None
    for m in _AMOUNT_RE.finditer(s):
        try:
            f = float(m.group(1).replace(",", ""))
        except ValueError:
            continue
        # Skip obviously-not-money small values that are probably page numbers.
        if f < 1.0:
            continue
        if best is None or f > best:
            best = f
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

    rows = list(_iter_parcel_rows(zf))
    log.info("NCAD parsed: %d rows", len(rows))

    lookup: Dict[str, Dict[str, str]] = {}
    for row in rows:
        try:
            info = _normalize_parcel_row(row)
            if not info:
                continue
            owner = info.pop("_owner_raw")
            for variant in _owner_name_variants(owner):
                if variant and variant not in lookup:
                    lookup[variant] = info
        except Exception as exc:
            log.debug("bad parcel row skipped: %s", exc)
            continue

    log.info("NCAD owner-lookup: %d distinct name variants", len(lookup))
    return lookup


def _discover_ncad_export_url() -> str:
    """Scrape https://nuecescad.net/downloads-reports/ for the most
    recent 'Public Export' ZIP link.
    """
    html = _http_get_text(NCAD_DOWNLOADS_PAGE)
    soup = BeautifulSoup(html, "lxml")

    candidates: List[Tuple[str, str]] = []  # (year-key, url)
    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = a.get_text(" ", strip=True).lower()
        if not href.lower().endswith(".zip"):
            continue
        # We want the *appraisal roll* export, not GIS / shapefiles.
        if "parcel" in href.lower() or "shapefile" in text:
            continue
        if not (("public-export" in href.lower())
                or ("appraisal-roll" in href.lower())
                or ("public_export" in href.lower())):
            continue
        # Extract a year-ish sort key (prefer the most recent).
        m = re.search(r"(20\d{2})", href)
        year = m.group(1) if m else "0000"
        candidates.append((year, href))

    if not candidates:
        raise RuntimeError("no NCAD export link found on downloads page")
    # Sort by year desc, then by URL (stable).
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
    # Look for the "appraisal" / "property" / "parcel" / "owner" file —
    # NCAD's export ships ~30 files; we want the one that ties owners
    # to addresses, which has names like "PROP.txt", "APPRAISAL_INFO.txt",
    # or just "Property.csv" depending on the year.
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
            ("property", 80), ("prop", 60),
            ("owner", 50), ("parcel", 40),
        ]:
            if kw in ln:
                score = max(score, w)
        return score
    text_candidates.sort(key=rank, reverse=True)

    for name in text_candidates:
        try:
            with zf.open(name) as fh:
                raw = fh.read()
            text = _decode_loose(raw)
            if not text.strip():
                continue
            delim = _sniff_delimiter(text)
            reader = csv.DictReader(io.StringIO(text), delimiter=delim)
            row_count = 0
            for row in reader:
                # Heuristic: skip files that obviously don't contain
                # owner/address info.
                row_count += 1
                yield {(k or "").strip().upper(): (v or "").strip()
                       for k, v in row.items()}
            log.debug("parsed %s (%d rows, delim=%r)", name, row_count, delim)
            if row_count > 1000:
                # Found a populated file — most CAD exports ship the owner
                # info in a single primary file. Yield from later files too,
                # but cap total work by breaking after a couple of hits.
                pass
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
    fields from the NCAD owner lookup whenever we can find a match.
    """
    if not owner_lookup:
        log.info("no owner lookup available; skipping address enrichment")
        return

    matched = 0
    for rec in records:
        if not rec.owner:
            continue
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
                matched += 1
                break
    log.info("address enrichment: %d/%d records matched (%d%%)",
             matched, len(records),
             int(100 * matched / max(1, len(records))))


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
