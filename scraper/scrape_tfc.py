"""
Tax Foreclosure Scraper — Linebarger Goggan Blair & Sampson (LGBS)
==================================================================

Pulls upcoming tax foreclosure sales for Nueces County from the LGBS
REST API at https://taxsales.lgbs.com/api/property_sales/.

This is **Phase 1** — listing endpoint only. The fields available here
are address, sale date, cause number, adjudged value, minimum bid,
status, sale type, and lat/lon. Owner name and legal description are
NOT in this endpoint; they require the per-property detail endpoint,
which is planned for Phase 2.

The API is JSON, no auth required for the listing endpoint. The site's
SPA front-end opens a "Terms of Use" modal on first visit; we don't go
through the SPA at all so we never see that modal — we hit the JSON
endpoint directly.

Filter defaults:
  * county = NUECES COUNTY (hardcoded; we only work this county)
  * sale_type = SALE,FUTURE SALE (excludes RESALE properties that
    failed to sell once already, and STRUCK OFF properties that
    sold back to the taxing entity)
  * ordering = sale_date,sale_nbr (chronological)

Output:
  dashboard/tfc.json   — { records: [...], total: N, fetched_at: ISO,
                           source: "lgbs", ... }
  data/tfc.json        — same content, mirror for archival

Designed to run in GitHub Actions on a daily cron. Same robustness
rules as the MFC scraper: never crash on a single bad record, retry
network calls, emit a valid (empty) JSON if upstream is down.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import requests

# ------------------------------------------------------------------
# Configuration
# ------------------------------------------------------------------

API_BASE = "https://taxsales.lgbs.com/api/property_sales/"

# Filter we apply to every request. Override via env vars if needed
# in the future (e.g. to pull additional counties or sale types).
COUNTY = "NUECES COUNTY"
SALE_TYPES = "SALE,FUTURE SALE"
# API supports `ordering=field1,field2`. Chronological + within a day
# by sale number is the most useful default.
ORDERING = "sale_date,sale_nbr"
# Listing endpoint pages results — empirically the default is 10 per
# page. We pass an explicit limit so changes upstream don't surprise us.
PAGE_LIMIT = 100
# Safety cap on total pages we'll walk — Nueces typically has 30-60
# properties so 50 pages of 100 each (5000 records) is wildly more
# than we'd ever see, but caps runaway loops if the API misbehaves.
MAX_PAGES = 50

# Network behavior
REQUEST_TIMEOUT_SEC = 30
RETRY_ATTEMPTS = 3
RETRY_BACKOFF_SEC = (1, 3, 8)  # cumulative-ish backoff per attempt

# Pretend to be a real browser. LGBS hasn't (yet) blocked simple
# requests calls in testing, but a User-Agent makes the request look
# less robotic and reduces the chance of getting flagged.
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Output paths — same convention as the MFC scraper.
REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUTS = [
    REPO_ROOT / "dashboard" / "tfc.json",
    REPO_ROOT / "data" / "tfc.json",
]

# Logging — INFO to stdout so it shows up in GitHub Actions logs.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("tfc")


# ------------------------------------------------------------------
# HTTP helper with retries
# ------------------------------------------------------------------

def _get_json(url: str) -> Optional[Dict[str, Any]]:
    """Fetch a URL and parse JSON, with retries on transient failures.

    Returns None on permanent failure (so the caller can decide whether
    to keep going with partial results or bail out entirely).
    """
    last_err = None
    for attempt in range(RETRY_ATTEMPTS):
        try:
            resp = requests.get(
                url,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "application/json",
                },
                timeout=REQUEST_TIMEOUT_SEC,
            )
            # 4xx is permanent (bad request, blocked, etc.) — don't retry.
            if 400 <= resp.status_code < 500:
                log.error("HTTP %s on %s — not retrying", resp.status_code, url)
                return None
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError) as e:
            last_err = e
            log.warning(
                "GET %s failed (attempt %d/%d): %s",
                url, attempt + 1, RETRY_ATTEMPTS, e
            )
            if attempt < RETRY_ATTEMPTS - 1:
                time.sleep(RETRY_BACKOFF_SEC[attempt])
    log.error("Giving up on %s after %d attempts: %s",
              url, RETRY_ATTEMPTS, last_err)
    return None


# ------------------------------------------------------------------
# Field mapping — LGBS API → our normalized record shape
# ------------------------------------------------------------------

def _normalize_record(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Convert one LGBS property record into our normalized shape.

    Returns None if the record is too malformed to use (missing the
    fields we consider essential). Logs a warning so we know.
    """
    try:
        uid = raw.get("uid")
        if not uid:
            log.warning("Skipping record with no uid: %s",
                        json.dumps(raw)[:200])
            return None

        # Build a single-line property address from the components.
        # API gives us prop_address_one + prop_city + prop_state + prop_zipcode.
        addr_parts = [
            raw.get("prop_address_one", "").strip(),
            raw.get("prop_address_two", "").strip(),
        ]
        street = " ".join(p for p in addr_parts if p)
        city = (raw.get("prop_city") or "").strip()
        state = (raw.get("prop_state") or "").strip()
        zipc = (raw.get("prop_zipcode") or "").strip()
        # "1058 BEECHCRAFT AVE, CORPUS CHRISTI TX 78405-2802"
        full_addr_bits = []
        if street:
            full_addr_bits.append(street)
        if city or state or zipc:
            tail_bits = []
            if city:
                tail_bits.append(city)
            if state:
                tail_bits.append(state)
            if zipc:
                tail_bits.append(zipc)
            full_addr_bits.append(" ".join(tail_bits))
        full_addr = ", ".join(full_addr_bits)

        # Sale date arrives as "2026-06-02T10:00:00" — keep the ISO
        # date and also the time component since auction times matter.
        sale_dt = raw.get("sale_date") or ""
        sale_date_only = raw.get("sale_date_only") or sale_dt.split("T")[0] if sale_dt else ""

        # Geometry is GeoJSON Point: { type: "Point", coordinates: [lon, lat] }
        geom = raw.get("geometry") or {}
        coords = geom.get("coordinates") if isinstance(geom, dict) else None
        lat = lon = None
        if isinstance(coords, list) and len(coords) >= 2:
            lon, lat = coords[0], coords[1]

        # Numeric fields come as strings like "96019.00" — convert.
        def _to_float(v):
            try:
                return float(v) if v not in (None, "") else None
            except (TypeError, ValueError):
                return None

        return {
            # Identifiers
            "uid": str(uid),
            "sale_id": str(raw.get("sale_id") or ""),
            "cause_nbr": (raw.get("cause_nbr") or "").strip(),
            "account_nbr": (raw.get("account_nbr") or "").strip(),
            # Sale info
            "sale_date": sale_dt,
            "sale_date_only": sale_date_only,
            "sale_nbr": raw.get("sale_nbr"),
            "sale_type": (raw.get("sale_type") or "").strip(),
            "status": (raw.get("status") or "").strip(),
            "precinct": (raw.get("precinct") or "").strip(),
            # Property
            "prop_address": full_addr,
            "prop_address_street": street,
            "prop_city": city,
            "prop_state": state,
            "prop_zipcode": zipc,
            "lat": lat,
            "lon": lon,
            # Money
            "adjudged_value": _to_float(raw.get("value")),
            "minimum_bid": _to_float(raw.get("minimum_bid")),
            # Misc
            "has_photo": bool(raw.get("has_photo")),
            "google_view_available": (raw.get("google_view") or "").upper() == "Y",
            "sale_notes": (raw.get("sale_notes") or "").strip(),
            "property_loc": (raw.get("property_loc") or "").strip(),
            "county_sale_list": (raw.get("county_sale_list") or "").strip(),
            # Source tag — useful when we eventually add a second
            # tax-sale source (e.g. sheriffsaleauctions.com direct).
            "_source": "lgbs",
        }
    except Exception as e:
        log.warning("Failed to normalize record: %s  (raw=%s)",
                    e, json.dumps(raw)[:200])
        return None


# ------------------------------------------------------------------
# Pagination — walk the API until exhausted
# ------------------------------------------------------------------

def fetch_all_records() -> List[Dict[str, Any]]:
    """Walk the paginated LGBS API for our county+sale_type filters.

    Returns a list of normalized records. On total failure returns an
    empty list (still writes a valid empty JSON output downstream).
    """
    # Build the first URL with our filters. After the first hit, we
    # follow `next` from the response (which is the API's own
    # next-page URL — includes all filters automatically).
    initial_params = {
        "county": COUNTY,
        "sale_type": SALE_TYPES,
        "ordering": ORDERING,
        "offset": 0,
        "limit": PAGE_LIMIT,
    }
    next_url = API_BASE + "?" + urlencode(initial_params, safe=",")

    records: List[Dict[str, Any]] = []
    seen_uids = set()
    pages_walked = 0

    while next_url and pages_walked < MAX_PAGES:
        pages_walked += 1
        log.info("Fetching page %d: %s", pages_walked, next_url)
        data = _get_json(next_url)
        if data is None:
            log.error("Page %d failed permanently; returning %d records "
                      "collected so far", pages_walked, len(records))
            break

        results = data.get("results") or []
        if not isinstance(results, list):
            log.error("Page %d 'results' was not a list — skipping", pages_walked)
            break

        for raw in results:
            if not isinstance(raw, dict):
                continue
            norm = _normalize_record(raw)
            if norm is None:
                continue
            # Defensive dedupe — the API shouldn't return duplicates
            # across pages but if pagination glitches we don't want
            # double-entries.
            if norm["uid"] in seen_uids:
                continue
            seen_uids.add(norm["uid"])
            records.append(norm)

        # The API returns `next: null` on the last page.
        next_url = data.get("next") or None
        # API sometimes returns http:// in `next` URLs even though
        # the site is https — coerce to https for safety.
        if next_url and next_url.startswith("http://"):
            next_url = "https://" + next_url[len("http://"):]
        # Be polite — small delay between paginated requests so we're
        # not hammering their API.
        if next_url:
            time.sleep(0.5)

    if pages_walked >= MAX_PAGES:
        log.warning("Hit MAX_PAGES safety cap (%d) — there may be more "
                    "records upstream", MAX_PAGES)

    log.info("Fetched %d total records across %d pages",
             len(records), pages_walked)
    return records


# ------------------------------------------------------------------
# Output
# ------------------------------------------------------------------

def write_output(records: List[Dict[str, Any]]) -> None:
    """Write the records to all configured output paths."""
    payload = {
        "records": records,
        "total": len(records),
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "lgbs",
        "county": COUNTY,
        "sale_types_filter": SALE_TYPES.split(","),
        # Phase tag — helpful when we add the detail-endpoint pass.
        "phase": "1-listing-only",
    }
    for path in OUTPUTS:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        log.info("Wrote %d records to %s", len(records), path)


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------

def main() -> int:
    log.info("Starting TFC scrape — county=%s sale_types=%s",
             COUNTY, SALE_TYPES)
    try:
        records = fetch_all_records()
    except Exception as e:
        # Last-resort catch so the workflow doesn't fail hard — we'd
        # rather have a stale tfc.json than no file at all (which would
        # break the dashboard).
        log.exception("Unhandled error during fetch: %s", e)
        records = []

    write_output(records)
    log.info("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
