"""
TFC NCAD Enrichment — Address-Based Parcel Lookup
=================================================

Standalone enrichment pass for tax foreclosure records. Reads
`dashboard/tfc.json`, matches each record's property address against
the NCAD bulk parcel export, and writes back the matching parcel's
owner, mailing address, and NCAD property ID.

Phase 1 (this file) — bulk parcel lookup only
---------------------------------------------
What we add to each TFC record (when an address match is found):
  * owner          — NCAD-reported primary owner name
  * mail_address   — mailing address street line
  * mail_city
  * mail_state
  * mail_zip
  * ncad_prop_id   — NCAD property ID (powers the dashboard ↗ link)
  * ncad_year      — tax-roll year (defaults to current year)

What we do NOT add yet (Phase 2 — separate detail-page fetch):
  * legal          — legal description (from /Property/View/{pid} HTML)
  * market_value   — appraised market value (from same detail page)

Why two phases? The bulk parcel zip is fast (one download, parsed in
memory, no per-record HTTP calls) but does not include legal or
market value. Detail-page enrichment requires one Playwright fetch
per matched parcel — slower, rate-limit-sensitive, and worth doing
in its own pass.

Why a separate file from fetch.py?
----------------------------------
The MFC scraper (fetch.py) has its own NCAD logic optimized for
owner-name-driven lookup, deeply intertwined with the ClerkRecord
dataclass. To avoid risking the MFC pipeline, we duplicate the
parcel-parsing patterns here and let TFC stand on its own. If the
NCAD zip schema ever changes, both files need updating — but the
two pipelines remain independent.

Output paths (same convention as scrape_tfc.py):
  dashboard/tfc.json   — for the live dashboard
  data/tfc.json        — mirror for archival

Designed to run in GitHub Actions on a manual or daily trigger.
Idempotent: re-running enriches new records without re-doing the
ones that already have ncad_prop_id (unless --force is passed).
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
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests

try:
    from bs4 import BeautifulSoup
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "BeautifulSoup4 is required (pip install beautifulsoup4 lxml)"
    ) from exc

# ------------------------------------------------------------------
# Configuration
# ------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
TFC_OUTPUTS = [
    REPO_ROOT / "dashboard" / "tfc.json",
    REPO_ROOT / "data" / "tfc.json",
]
CACHE_DIR = REPO_ROOT / ".cache"
PARCEL_ZIP_CACHE = CACHE_DIR / "ncad_parcels_tfc.zip"
ADDR_INDEX_CACHE = CACHE_DIR / "ncad_addr_index_tfc.json"
ENRICHMENT_LOG = REPO_ROOT / "data" / "enrich_tfc_ncad_log.json"

# NCAD endpoints — same as fetch.py uses. Kept in sync by convention,
# not by import, to keep this module independent.
NCAD_DOWNLOADS_PAGE = "https://nuecescad.net/downloads-reports/"
NCAD_BASE = "https://esearch.nuecescad.net"

# Known-good fallback URL when the downloads page can't be parsed
# (WAF, layout change, etc.). Update once a year when NCAD posts
# the new preliminary roll.
NCAD_KNOWN_GOOD_ZIP_URL = (
    "https://nuecescad.net/wp-content/uploads/2026/04/"
    "2026-Preliminary-Public-Export-20260402.zip"
)

# Default NCAD year — the tax roll currently being enriched. Used as
# the `year=` URL parameter in property-detail links from the dashboard.
NCAD_YEAR = "2026"

# Cache freshness — bulk parcel zip is large (~80 MB), so we cache
# the download for a week. Re-downloads automatically when stale.
PARCEL_CACHE_TTL_SECONDS = 7 * 24 * 60 * 60   # 7 days
# Same TTL for the parsed address index, which only depends on the
# zip's contents — rebuilds automatically when the zip refreshes.
ADDR_INDEX_TTL_SECONDS = 7 * 24 * 60 * 60

# Network / parsing safety
REQUEST_TIMEOUT_SEC = 300                # zip download can be slow
# Retry strategy for the parcel zip download via plain `requests`.
# Kept short because NCAD's CDN often fingerprints GitHub Actions IPs
# and 429s every request regardless — when that happens we want to
# fall through to the Playwright path quickly rather than waste
# minutes on retries that can't possibly succeed.
ZIP_DOWNLOAD_RETRIES = 2
ZIP_DOWNLOAD_BACKOFF = (5, 10)  # seconds between attempts
PARSE_DEADLINE_SECONDS = 120             # cap on total zip-parse time
PER_FILE_TIMEOUT_SECONDS = 30            # cap per inner text file
MAX_INNER_FILE_BYTES = 80 * 1024 * 1024  # skip > 80 MB inner files

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Apply mode: by default the script is DRY-RUN and writes a log
# describing what WOULD change but does not modify tfc.json. Set
# ENRICH_TFC_APPLY=1 to actually write the enriched JSON. This is
# the same convention recorroborate_ncad.py uses — gives the operator
# a chance to inspect proposed changes before committing them.
APPLY = os.getenv("ENRICH_TFC_APPLY", "0") == "1"
# Force mode: re-enrich every record even if it already has
# ncad_prop_id from a prior run. Useful after parcel-zip refresh
# when matching logic has changed.
FORCE = os.getenv("ENRICH_TFC_FORCE", "0") == "1"

# Logging — INFO to stdout so it shows in GitHub Actions output.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("enrich-tfc-ncad")


# ==================================================================
# Step 1 — Discover and download the NCAD bulk parcel zip
# ==================================================================

def _http_get_bytes(url: str, timeout: int = REQUEST_TIMEOUT_SEC,
                     referer: str = "",
                     retries: int = 1) -> bytes:
    """Fetch a URL as raw bytes with browser-like headers and optional
    retries with exponential backoff. Raises on final failure.

    The Referer header is critical for NCAD's CDN — they 429 requests
    that don't look like they came from clicking a link on the
    downloads page. Default retries=1 (no retry) keeps the call cheap
    for non-zip fetches; the zip-download path passes retries=N.
    """
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
    }
    if referer:
        headers["Referer"] = referer

    last_err: Optional[Exception] = None
    for attempt in range(retries):
        try:
            resp = requests.get(url, headers=headers, timeout=timeout)
            # 429 / 503 are the rate-limit / overload responses we
            # specifically want to retry. 4xx (other than 429) is
            # permanent — fail fast.
            if resp.status_code in (429, 503):
                raise requests.HTTPError(
                    f"{resp.status_code} from {url}",
                    response=resp,
                )
            resp.raise_for_status()
            return resp.content
        except (requests.RequestException, requests.HTTPError) as exc:
            last_err = exc
            if attempt < retries - 1:
                # Pick backoff seconds — use the schedule when within
                # range, hold at the last value beyond.
                idx = min(attempt, len(ZIP_DOWNLOAD_BACKOFF) - 1)
                wait = ZIP_DOWNLOAD_BACKOFF[idx]
                log.warning(
                    "GET %s failed (attempt %d/%d): %s — retrying in %ds",
                    url, attempt + 1, retries, exc, wait,
                )
                time.sleep(wait)
            else:
                log.error(
                    "GET %s failed after %d attempts: %s",
                    url, retries, exc,
                )
    raise last_err if last_err else RuntimeError("download failed")


async def _pw_get_bytes(url: str, referer: str = "",
                         timeout: int = REQUEST_TIMEOUT_SEC) -> bytes:
    """Playwright fallback for binary downloads when plain requests
    gets blocked (typically by CDN WAFs that fingerprint GitHub
    Actions IPs and reject the request before headers even matter).

    A real Chromium browser sends a TLS fingerprint + cookies +
    full header set that CDNs accept as legitimate, where `requests`
    looks like a bot to them. Modeled on fetch.py:_pw_get_bytes —
    same pattern that MFC uses to successfully download this same
    parcel zip.

    Optional `referer` first navigates to that page so the request
    chain looks like a real user clicking through. NCAD's CDN
    especially cares about this — direct CDN URL hits get 429'd
    while clickthrough requests pass.
    """
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True, args=["--no-sandbox"],
        )
        context = await browser.new_context(user_agent=USER_AGENT)
        try:
            # First "visit" the downloads page so the CDN sees us as
            # a normal visitor with a referrer + a cookie jar.
            if referer:
                try:
                    page = await context.new_page()
                    await page.goto(
                        referer, wait_until="domcontentloaded",
                        timeout=30_000,
                    )
                    # Give the page a moment to set any cookies / run
                    # any WAF challenge JS. Most CDN protections that
                    # use JS challenges (Cloudflare etc.) only block
                    # the FIRST request; subsequent ones with the
                    # cookie pass through.
                    await page.wait_for_timeout(2000)
                    await page.close()
                except Exception as exc:
                    log.warning("playwright referer-visit failed: %s "
                                "(continuing with direct fetch)", exc)
            # Now fetch the binary URL within the same browser context
            # — cookies + TLS fingerprint carry over.
            resp = await context.request.get(url, timeout=timeout * 1000)
            if resp.status != 200:
                raise RuntimeError(
                    f"playwright HTTP {resp.status} for {url}"
                )
            return await resp.body()
        finally:
            await context.close()
            await browser.close()


def _http_get_text(url: str, timeout: int = 60) -> str:
    """Fetch a URL as text with a browser-like UA. Raises on error."""
    resp = requests.get(
        url,
        headers={"User-Agent": USER_AGENT},
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.text


def _discover_ncad_export_url() -> str:
    """Scrape NCAD's downloads page for the most recent appraisal-roll
    ZIP link. Returns the known-good fallback if discovery fails.

    Mirrors fetch.py:_discover_ncad_export_url — kept as a local copy
    so this module has no dependency on the MFC scraper.
    """
    try:
        html = _http_get_text(NCAD_DOWNLOADS_PAGE)
    except Exception as exc:
        log.warning("NCAD downloads page fetch failed (%s); "
                    "using known-good URL", exc)
        return NCAD_KNOWN_GOOD_ZIP_URL

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
        # Skip GIS shapefiles and parcel-only exports — we want the
        # full appraisal roll, which contains owner + situs + mailing.
        if "shapefile" in text or "ncad_parcels" in href.lower():
            continue
        href_low = href.lower()
        if not any(tok in href_low for tok in (
            "public-export", "public_export", "publicexport",
            "appraisal-roll", "appraisal_roll", "certified-roll",
            "preliminary-public", "preliminary_public",
        )):
            continue
        m = re.search(r"(20\d{2})", href)
        year = m.group(1) if m else "0000"
        candidates.append((year, href))

    if not candidates:
        log.warning("no NCAD export link found on downloads page; "
                    "using known-good URL")
        return NCAD_KNOWN_GOOD_ZIP_URL
    candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return candidates[0][1]


def _load_or_download_parcel_zip() -> Optional[bytes]:
    """Return the parcel-zip bytes, loading from cache if fresh.

    Cache logic: if PARCEL_ZIP_CACHE exists and is younger than
    PARCEL_CACHE_TTL_SECONDS, reuse it. Otherwise fetch the latest
    URL and save. On fetch failure with a present cache, fall back
    to the cached copy with a warning.

    Manual URL override: set the env var NCAD_ZIP_URL to bypass URL
    discovery entirely. Useful when NCAD's downloads page is down or
    you want to test against a specific export version.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if PARCEL_ZIP_CACHE.exists():
        age = time.time() - PARCEL_ZIP_CACHE.stat().st_mtime
        if age < PARCEL_CACHE_TTL_SECONDS:
            log.info("parcel zip cache fresh (%.1f h old), using cached copy",
                     age / 3600)
            return PARCEL_ZIP_CACHE.read_bytes()

    # Manual override wins over discovery. Operator can paste a known
    # working URL via env var when NCAD's site is misbehaving.
    manual_url = os.getenv("NCAD_ZIP_URL", "").strip()
    if manual_url:
        zip_url = manual_url
        log.info("using manual NCAD_ZIP_URL override")
    else:
        try:
            zip_url = _discover_ncad_export_url()
        except Exception as exc:
            log.error("could not discover NCAD export URL: %s", exc)
            if PARCEL_ZIP_CACHE.exists():
                log.warning("falling back to stale parcel cache")
                return PARCEL_ZIP_CACHE.read_bytes()
            return None

    log.info("downloading NCAD parcel zip: %s", zip_url)
    log.info("(if NCAD rate-limits us, we'll retry with backoff up to %d times)",
             ZIP_DOWNLOAD_RETRIES)

    # Two-tier download strategy:
    # 1. Try plain `requests` with browser headers + Referer + retries.
    #    Fast and cheap when it works.
    # 2. If `requests` exhausts retries (typically because NCAD's CDN
    #    fingerprints GitHub Actions IPs and 429s every request before
    #    headers even matter), fall back to Playwright. A real Chromium
    #    browser sends a TLS fingerprint + cookies + full header set
    #    that CDNs treat as legitimate.
    #
    # This is the same pattern fetch.py uses, which is why MFC can
    # download this same zip successfully.
    content: Optional[bytes] = None
    try:
        content = _http_get_bytes(
            zip_url,
            referer=NCAD_DOWNLOADS_PAGE,
            retries=ZIP_DOWNLOAD_RETRIES,
        )
        log.info("parcel zip via requests: %d MB", len(content) // (1024 * 1024))
    except Exception as exc:
        log.warning("plain requests failed (%s) — falling back to Playwright",
                    exc)
        try:
            content = asyncio.run(_pw_get_bytes(
                zip_url, referer=NCAD_DOWNLOADS_PAGE,
            ))
            log.info("parcel zip via Playwright: %d MB",
                     len(content) // (1024 * 1024))
        except Exception as pw_exc:
            log.error("Playwright fallback also failed: %s", pw_exc)
            if PARCEL_ZIP_CACHE.exists():
                log.warning("falling back to stale parcel cache")
                return PARCEL_ZIP_CACHE.read_bytes()
            return None

    if not content:
        log.error("no content from either download path")
        if PARCEL_ZIP_CACHE.exists():
            log.warning("falling back to stale parcel cache")
            return PARCEL_ZIP_CACHE.read_bytes()
        return None

    try:
        PARCEL_ZIP_CACHE.write_bytes(content)
        log.info("saved parcel zip to %s", PARCEL_ZIP_CACHE)
    except Exception as exc:
        log.warning("could not save parcel zip cache: %s", exc)
    return content


# ==================================================================
# Step 2 — Parse the zip and build address → parcel_info index
# ==================================================================

def _decode_loose(raw: bytes) -> str:
    """Decode bytes with permissive fallback. Mirrors fetch.py."""
    for enc in ("utf-8", "utf-8-sig", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", errors="replace")


def _sniff_delimiter(text: str) -> str:
    """Pick the most-frequent delimiter from the first 8 KB. Mirrors fetch.py."""
    sample = text[:8192]
    counts = {d: sample.count(d) for d in ("|", "\t", ",", ";")}
    return max(counts, key=counts.get)


# Column-name candidates per logical field. Same role-tokens as
# fetch.py's _build_owner_lookup_from_zip — kept as a local copy
# so we don't import from the MFC scraper.
ID_TOKENS    = ("PROP_ID", "PROPID", "PROPERTY_ID", "ACCOUNT_NUM",
                "ACCT_NUM", "PARCEL_ID", "PARCELID", "GEO_ID", "QUICK_REF")
OWNER_TOKENS = ("OWNER", "FILE_AS_NAME", "PY_OWNER")
SITE_TOKENS  = ("SITUS", "SITE_ADDR", "PROP_ADDR", "STREET")
MAIL_TOKENS  = ("MAIL_ADDR", "MAILING_ADDR", "ADDR_1", "ADDR1",
                "ADDR_LINE", "MAIL_LINE")
CITY_TOKENS  = ("CITY",)
STATE_TOKENS = ("STATE",)
ZIP_TOKENS   = ("ZIP", "POSTAL")


def _find_col(headers: List[str], tokens: tuple,
              exclude: tuple = ()) -> str:
    """Return first header containing any token (case-insensitive) and
    none of the excludes."""
    for h in headers:
        up = h.upper()
        if any(s in up for s in exclude):
            continue
        if any(t in up for t in tokens):
            return h
    return ""


def _clean_row(row: Dict[str, Any]) -> Dict[str, str]:
    """Coerce a csv.DictReader row to upper-cased str→str."""
    out: Dict[str, str] = {}
    for k, v in row.items():
        if k is None:
            continue
        key = k.upper().strip()
        if isinstance(v, list):
            val = " ".join(str(x) for x in v if x).strip()
        else:
            val = str(v or "").strip()
        out[key] = val
    return out


def _normalize_address(addr: str) -> str:
    """Canonical form for address matching.

    Goals:
      * Case-insensitive
      * Collapse whitespace
      * Remove punctuation that varies between sources (commas, periods)
      * Normalize common street-type abbreviations to a single form
        (AVENUE/AVE → AVE, DRIVE/DR → DR, etc.)
      * Strip apartment/unit suffixes (LGBS and NCAD disagree on
        whether to include them)

    Examples:
      "1058 BEECHCRAFT AVE." → "1058 BEECHCRAFT AVE"
      "1058 Beechcraft Avenue" → "1058 BEECHCRAFT AVE"
      "1058 BEECHCRAFT AVE, UNIT 2" → "1058 BEECHCRAFT AVE"
    """
    if not addr:
        return ""
    s = addr.upper().strip()
    # Drop apartment / unit / suite tokens and everything after
    s = re.split(
        r"\b(?:APT|UNIT|STE|SUITE|#)\b",
        s, maxsplit=1
    )[0]
    # Drop trailing zip / city / state segment after a comma
    s = s.split(",")[0]
    # Strip punctuation we don't care about
    s = re.sub(r"[.,'\"]", " ", s)
    # Normalize street-type abbreviations (long → short)
    abbrev_map = {
        r"\bSTREET\b":   "ST",
        r"\bAVENUE\b":   "AVE",
        r"\bBOULEVARD\b":"BLVD",
        r"\bDRIVE\b":    "DR",
        r"\bROAD\b":     "RD",
        r"\bLANE\b":     "LN",
        r"\bCOURT\b":    "CT",
        r"\bPLACE\b":    "PL",
        r"\bTRAIL\b":    "TRL",
        r"\bPARKWAY\b":  "PKWY",
        r"\bCIRCLE\b":   "CIR",
        r"\bTERRACE\b":  "TER",
        r"\bHIGHWAY\b":  "HWY",
    }
    for pattern, replacement in abbrev_map.items():
        s = re.sub(pattern, replacement, s)
    # Collapse whitespace
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _normalize_zip(z: str) -> str:
    """5-digit zip — strip +4 extension and any non-digits.
    NCAD typically stores 5-digit; LGBS sometimes has 5+4."""
    if not z:
        return ""
    m = re.match(r"(\d{5})", z)
    return m.group(1) if m else ""


def _build_address_index(zip_bytes: bytes
                         ) -> Dict[str, Dict[str, str]]:
    """Parse the NCAD parcel ZIP and build:
        normalized_site_address → {
            ncad_prop_id, owner,
            site_addr, site_city, site_state, site_zip,
            mail_addr, mail_city, mail_state, mail_zip
        }

    This is the INVERSE of fetch.py's owner-keyed lookup. We index by
    address so a TFC record (which has address but no owner) can find
    its NCAD parcel and pull owner + mail address from there.

    Strategy: scan every text file inside the zip; collect owner-by-id
    and addr-by-id; join on the property ID. The final lookup table
    re-keys by normalized site address. When two parcels share an
    address (rare — typically apartment buildings) we keep the FIRST
    seen and skip the rest, since we have no good way to disambiguate
    without unit numbers.
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile as exc:
        log.error("parcel zip is invalid: %s", exc)
        return {}

    names = zf.namelist()
    log.info("parcel ZIP contents: %d files", len(names))
    for n in names:
        log.info("  • %s", n)

    deadline = time.time() + PARSE_DEADLINE_SECONDS
    owner_by_id: Dict[str, str] = {}
    addr_by_id: Dict[str, Dict[str, str]] = {}

    text_files = [n for n in names
                  if n.lower().endswith((".txt", ".csv", ".tsv"))]
    log.info("scanning %d text files", len(text_files))

    for name in text_files:
        if time.time() > deadline:
            log.warning("overall parse budget exhausted at %s", name)
            break
        try:
            try:
                file_size = zf.getinfo(name).file_size
            except KeyError:
                file_size = 0
            if file_size > MAX_INNER_FILE_BYTES:
                log.info("  skipping %s (%.0f MB > cap)",
                         name, file_size / 1024 / 1024)
                continue

            file_deadline = time.time() + PER_FILE_TIMEOUT_SECONDS
            with zf.open(name) as fh:
                raw = fh.read()
            text = _decode_loose(raw)
            if not text.strip():
                continue
            delim = _sniff_delimiter(text)
            reader = csv.DictReader(io.StringIO(text), delimiter=delim)
            headers = [(h or "").strip() for h in (reader.fieldnames or [])]

            # Skip headerless / numeric-only files (PTAD entity lists).
            looks_like_data = (
                not headers
                or len(headers) == 1
                or all(re.match(r"^[\d\s\-_/]+$", h)
                       for h in headers if h)
            )
            if looks_like_data:
                log.info("  %s: no recognizable header — skipping", name)
                continue

            id_col    = _find_col(headers, ID_TOKENS)
            owner_col = _find_col(headers, OWNER_TOKENS)
            site_col  = _find_col(headers, SITE_TOKENS,
                                  exclude=("CITY", "ZIP", "STATE"))
            mail_col  = _find_col(headers, MAIL_TOKENS,
                                  exclude=("CITY", "ZIP", "STATE"))
            site_city  = _find_col(headers, CITY_TOKENS) if site_col else ""
            site_state = _find_col(headers, STATE_TOKENS)
            site_zip   = _find_col(headers, ZIP_TOKENS)

            log.info("  %s: cols=%d size=%.1fMB delim=%r",
                     name, len(headers), file_size / 1024 / 1024, delim)
            log.info("    matched: id=%r owner=%r site=%r mail=%r",
                     id_col, owner_col, site_col, mail_col)

            if not id_col:
                log.info("    (no ID column — skipping rows)")
                continue
            if not owner_col and not site_col and not mail_col:
                log.info("    (no owner/address columns — skipping rows)")
                continue

            row_count = 0
            owner_added = 0
            addr_added = 0
            for row in reader:
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
                        "site_addr":  site_val,
                        "site_city":  clean.get(site_city.upper(), "")
                                       if site_city else "",
                        "site_state": clean.get(site_state.upper(), "TX")
                                       if site_state else "TX",
                        "site_zip":   clean.get(site_zip.upper(), "")
                                       if site_zip else "",
                        "mail_addr":  mail_val,
                        "mail_city":  clean.get(site_city.upper(), "")
                                       if site_city else "",
                        "mail_state": clean.get(site_state.upper(), "TX")
                                       if site_state else "TX",
                        "mail_zip":   clean.get(site_zip.upper(), "")
                                       if site_zip else "",
                    }
                    addr_added += 1
            log.info("    %d rows (+%d owner, +%d addr)",
                     row_count, owner_added, addr_added)
        except Exception as exc:
            log.warning("text parse failed for %s: %s", name, exc)
            continue

    log.info("collected: %d owner records, %d address records",
             len(owner_by_id), len(addr_by_id))

    # ----- Join + invert -----
    # Build the final addr-keyed index. Where both owner and address
    # are present for a pid, store the merged record. Where only
    # address is present, still store it (we can attach prop_id even
    # without owner — caller logs the gap).
    index: Dict[str, Dict[str, str]] = {}
    dupes_skipped = 0
    no_addr_skipped = 0

    # Build set of all pids that have an address (we only care about
    # parcels with situs addresses since that's what TFC matches against).
    all_pids = set(addr_by_id.keys()) | set(owner_by_id.keys())
    for pid in all_pids:
        info = addr_by_id.get(pid) or {}
        owner = owner_by_id.get(pid, "")
        site_addr = info.get("site_addr", "")
        if not site_addr:
            no_addr_skipped += 1
            continue
        norm = _normalize_address(site_addr)
        if not norm:
            continue
        if norm in index:
            dupes_skipped += 1
            continue
        index[norm] = {
            "ncad_prop_id": pid,
            "owner":        owner,
            "site_addr":    site_addr,
            "site_city":    info.get("site_city", ""),
            "site_state":   info.get("site_state", "TX"),
            "site_zip":     info.get("site_zip", ""),
            "mail_addr":    info.get("mail_addr", ""),
            "mail_city":    info.get("mail_city", ""),
            "mail_state":   info.get("mail_state", "TX"),
            "mail_zip":     info.get("mail_zip", ""),
        }

    log.info("address index built: %d unique addresses "
             "(%d duplicates collapsed, %d no-addr parcels skipped)",
             len(index), dupes_skipped, no_addr_skipped)
    return index


def _load_or_build_address_index() -> Dict[str, Dict[str, str]]:
    """Cache-aware loader. Returns the address index, either from disk
    or freshly built. Cache invalidates on TTL OR when the parcel zip
    is newer than the index."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    # Cache freshness — same TTL as parcel zip + must be newer than zip
    # so a zip refresh invalidates the index even within TTL.
    if ADDR_INDEX_CACHE.exists():
        idx_age = time.time() - ADDR_INDEX_CACHE.stat().st_mtime
        zip_newer = (
            PARCEL_ZIP_CACHE.exists() and
            PARCEL_ZIP_CACHE.stat().st_mtime > ADDR_INDEX_CACHE.stat().st_mtime
        )
        if idx_age < ADDR_INDEX_TTL_SECONDS and not zip_newer:
            try:
                with ADDR_INDEX_CACHE.open("r", encoding="utf-8") as fh:
                    cached = json.load(fh)
                log.info("address index cache fresh (%.1f h, %d addrs)",
                         idx_age / 3600, len(cached))
                return cached
            except Exception as exc:
                log.warning("could not load address index cache: %s", exc)

    zip_bytes = _load_or_download_parcel_zip()
    if not zip_bytes:
        log.error("no parcel zip available — returning empty index")
        return {}
    index = _build_address_index(zip_bytes)

    try:
        with ADDR_INDEX_CACHE.open("w", encoding="utf-8") as fh:
            json.dump(index, fh, ensure_ascii=False)
        log.info("address index saved to %s", ADDR_INDEX_CACHE)
    except Exception as exc:
        log.warning("could not save address index cache: %s", exc)
    return index


# ==================================================================
# Step 3 — Match TFC records against the index
# ==================================================================

def _match_record(record: Dict[str, Any],
                  index: Dict[str, Dict[str, str]]
                  ) -> Optional[Dict[str, str]]:
    """Find the best NCAD parcel for a TFC record. Returns the parcel
    info dict (from index) or None if no match.

    Matching strategy — strict for safety, additive for coverage:
      1. Exact normalized match on prop_address_street (strongest)
      2. Exact normalized match on full prop_address with city/zip
         stripped (covers cases where prop_address_street is missing)
      3. Zip-corroborated relaxed match: if both sides have a 5-digit
         zip and the zip matches, allow a slightly looser address
         comparison (street number + first street word). This catches
         cases where one source has "AVE" and the other has nothing.

    No fuzzy matching beyond #3 — we'd rather miss than mis-attach.
    """
    street = record.get("prop_address_street") or ""
    full   = record.get("prop_address") or ""
    rec_zip = _normalize_zip(record.get("prop_zipcode") or "")

    # Strategy 1: street-only normalized match.
    key1 = _normalize_address(street)
    if key1 and key1 in index:
        return index[key1]

    # Strategy 2: full address with comma-tail stripped.
    key2 = _normalize_address(full)
    if key2 and key2 in index and key2 != key1:
        return index[key2]

    # Strategy 3: zip-corroborated relaxed match. We only do this if
    # there's a zip on the record AND the candidate key shares it.
    # Format: extract leading number + first alphabetic word
    # ("1058 BEECHCRAFT") and find any index entry whose normalized
    # address contains the same prefix AND whose site_zip matches.
    if rec_zip and key1:
        m = re.match(r"^(\d+)\s+([A-Z]+)", key1)
        if m:
            num, first_word = m.group(1), m.group(2)
            prefix = f"{num} {first_word}"
            for cand_key, cand_info in index.items():
                if cand_key.startswith(prefix) and \
                   _normalize_zip(cand_info.get("site_zip", "")) == rec_zip:
                    return cand_info

    return None


def _apply_enrichment(record: Dict[str, Any],
                       parcel: Dict[str, str]) -> Dict[str, Any]:
    """Apply parcel fields to a TFC record. Returns the diff dict for
    logging. Does NOT overwrite non-empty existing values for fields
    the TFC scrape originally populated (prop_address, etc.) — only
    fills in fields TFC doesn't normally own (owner, mail_*, ncad_*).
    """
    additions: Dict[str, Any] = {}
    fields_to_set = (
        ("owner",         parcel.get("owner", "")),
        ("mail_address",  parcel.get("mail_addr", "")),
        ("mail_city",     parcel.get("mail_city", "")),
        ("mail_state",    parcel.get("mail_state", "TX")),
        ("mail_zip",      _normalize_zip(parcel.get("mail_zip", ""))),
        ("ncad_prop_id",  parcel.get("ncad_prop_id", "")),
        ("ncad_year",     NCAD_YEAR),
    )
    for key, value in fields_to_set:
        if not value:
            continue
        # ncad_year is "set always", others "set if empty or different".
        existing = record.get(key) or ""
        if key == "ncad_year":
            additions[key] = value
        elif existing != value:
            additions[key] = value
    return additions


# ==================================================================
# Step 4 — Load and write TFC JSON
# ==================================================================

def _load_tfc_records() -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Load tfc.json. Returns (payload_envelope, records_list).
    Prefer dashboard/tfc.json; fall back to data/tfc.json if missing.
    Raises if neither exists."""
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
    """Write the updated payload to all TFC output paths."""
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
    """Write the run log for human review."""
    ENRICHMENT_LOG.parent.mkdir(parents=True, exist_ok=True)
    out = {
        "ran_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "apply_mode": APPLY,
        "force_mode": FORCE,
        "entries": entries,
    }
    with ENRICHMENT_LOG.open("w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2, ensure_ascii=False)
    log.info("wrote run log to %s", ENRICHMENT_LOG)


# ==================================================================
# Main
# ==================================================================

def main() -> int:
    log.info("=== TFC NCAD Enrichment — apply=%s force=%s ===",
             APPLY, FORCE)

    # Load TFC records.
    payload, records = _load_tfc_records()

    # Build the NCAD address index (cached).
    index = _load_or_build_address_index()
    if not index:
        log.error("address index is empty — cannot enrich. Exiting.")
        return 1

    # Match + apply.
    log_entries: List[Dict[str, Any]] = []
    matched = 0
    skipped_already = 0
    no_match = 0
    for rec in records:
        uid = rec.get("uid", "")
        addr_for_log = rec.get("prop_address", "")

        # Skip records that already have ncad_prop_id, unless FORCE.
        if rec.get("ncad_prop_id") and not FORCE:
            skipped_already += 1
            continue

        parcel = _match_record(rec, index)
        if not parcel:
            no_match += 1
            log_entries.append({
                "uid": uid,
                "prop_address": addr_for_log,
                "result": "no-match",
            })
            continue

        diff = _apply_enrichment(rec, parcel)
        if not diff:
            log_entries.append({
                "uid": uid,
                "prop_address": addr_for_log,
                "result": "match-no-changes",
                "ncad_prop_id": parcel.get("ncad_prop_id"),
            })
            continue

        if APPLY:
            for key, value in diff.items():
                rec[key] = value
        matched += 1
        log_entries.append({
            "uid": uid,
            "prop_address": addr_for_log,
            "result": "matched",
            "ncad_prop_id": parcel.get("ncad_prop_id"),
            "ncad_owner": parcel.get("owner"),
            "additions": diff,
        })

    log.info("summary: matched=%d, no-match=%d, already-enriched=%d, total=%d",
             matched, no_match, skipped_already, len(records))

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
