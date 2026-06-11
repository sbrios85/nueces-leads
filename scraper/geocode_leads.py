#!/usr/bin/env python3
"""Geocode lead property addresses to lat/lng for the Map view.

Reads the property address from each lead record in:
  - dashboard/foreclosures.json   (Mortgage Foreclosures)
  - dashboard/tfc.json            (Tax Foreclosures)
  - dashboard/city_liens.json     (City of Corpus Christi Liens / CCLN)

…and writes `prop_lat` / `prop_lng` (floats) back into each record using
the FREE U.S. Census Bureau batch geocoder
(https://geocoding.geo.census.gov). No API key, bulk-friendly (up to
10,000 addresses per batch request), purpose-built for US street
addresses.

Design notes
------------
* IDEMPOTENT: by default only records that LACK prop_lat/prop_lng AND
  have a usable street address are submitted. Already-geocoded records
  are skipped (cheap re-runs; new leads get picked up automatically when
  this runs after a scrape). Set GEOCODE_FORCE=1 to re-geocode every
  record (e.g. after fixing an address).
* DRY-RUN by default: prints a coverage report and writes nothing unless
  GEOCODE_APPLY=1. This mirrors the NCAD enrichers' apply/dry pattern.
* RECORD IDENTITY: doc_num is the stable per-record key in all three
  datasets; it's used to map Census results back to the right record.
* UNMATCHABLE addresses (PO boxes, ranges like "1101-1107 1/2 SAM
  RANKIN", malformed) simply don't get coordinates — the map skips
  them. The script logs how many matched vs. didn't per dataset.
* The Census batch endpoint takes a CSV: Unique ID, Street, City,
  State, ZIP (no header). It returns a CSV with match status + lon,lat
  for matched rows. We submit in chunks of <=10000.

Environment
-----------
  GEOCODE_APPLY   "1" to write changes (default "0" = dry-run)
  GEOCODE_FORCE   "1" to re-geocode records that already have coords
  LOG_LEVEL       default INFO
"""

import csv
import io
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("geocode_leads")

APPLY = os.environ.get("GEOCODE_APPLY", "0") == "1"
FORCE = os.environ.get("GEOCODE_FORCE", "0") == "1"

# Census batch geocoder — addressbatch endpoint, current benchmark.
CENSUS_URL = "https://geocoding.geo.census.gov/geocoder/locations/addressbatch"
CENSUS_BENCHMARK = "Public_AR_Current"
BATCH_SIZE = 5000          # well under the 10k cap; keeps each POST modest
REQUEST_TIMEOUT = 300      # batch geocoding can take a few minutes
MAX_RETRIES = 3

# Repo layout: this script lives in scraper/, data lives in dashboard/
# (and a mirror in data/). We write BOTH copies so the committed repo
# and the deployed dashboard stay in sync, exactly like the enrichers.
REPO = Path(__file__).resolve().parent.parent
DATASETS = [
    # (label, dashboard path, data-mirror path)
    ("FC (Mortgage Foreclosures)", REPO / "dashboard" / "foreclosures.json", REPO / "data" / "foreclosures.json"),
    ("TFC (Tax Foreclosures)",     REPO / "dashboard" / "tfc.json",          REPO / "data" / "tfc.json"),
    ("CCLN (City Liens)",          REPO / "dashboard" / "city_liens.json",   REPO / "data" / "city_liens.json"),
    ("CV (Code Violations)",       REPO / "dashboard" / "code_violations.json", REPO / "data" / "code_violations.json"),
]


def _records(obj):
    """Both wrapped ({"records": [...]}) and bare-list JSON are in use."""
    return obj["records"] if isinstance(obj, dict) and "records" in obj else obj


def _build_query(rec: dict) -> Optional[Tuple[str, str, str, str]]:
    """Return (street, city, state, zip) or None if there's no usable
    street address. City/state/zip may be blank — Census tolerates
    partial rows as long as street is present."""
    street = (rec.get("prop_address") or "").strip()
    if not street:
        return None
    city = (rec.get("prop_city") or "").strip()
    state = (rec.get("prop_state") or "TX").strip()
    zc = (rec.get("prop_zip") or "").strip()
    # Census wants ZIP as 5-digit; trim any +4.
    if "-" in zc:
        zc = zc.split("-", 1)[0].strip()
    return street, city, state, zc


def _census_batch(rows: List[Tuple[str, str, str, str, str]]) -> Dict[str, Tuple[float, float]]:
    """Submit one chunk of rows to the Census batch geocoder.

    rows: list of (uid, street, city, state, zip)
    Returns {uid: (lat, lng)} for matched rows only.
    """
    buf = io.StringIO()
    w = csv.writer(buf)
    for uid, street, city, state, zc in rows:
        w.writerow([uid, street, city, state, zc])
    csv_bytes = buf.getvalue().encode("utf-8")

    files = {"addressFile": ("addresses.csv", csv_bytes, "text/csv")}
    data = {"benchmark": CENSUS_BENCHMARK}

    last_exc = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(CENSUS_URL, files=files, data=data, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            break
        except Exception as exc:  # noqa: BLE001 — network is flaky; retry
            last_exc = exc
            wait = 5 * attempt
            log.warning("  batch attempt %d/%d failed (%s); retrying in %ds",
                        attempt, MAX_RETRIES, exc, wait)
            time.sleep(wait)
    else:
        log.error("  batch permanently failed: %s", last_exc)
        return {}

    # Response is CSV WITHOUT a header. Columns (per Census docs):
    # 0 unique_id, 1 input_address, 2 match_indicator ("Match"/"No_Match"
    # /"Tie"), 3 match_type, 4 matched_address, 5 "lon,lat", 6 tiger_line_id,
    # 7 side. The coordinate field is x,y = LON,LAT (note the order).
    out: Dict[str, Tuple[float, float]] = {}
    reader = csv.reader(io.StringIO(resp.text))
    for row in reader:
        if len(row) < 6:
            continue
        uid, match_ind, coord = row[0], row[2], row[5]
        if match_ind != "Match" or not coord:
            continue
        try:
            lon_s, lat_s = coord.split(",")
            lat, lng = float(lat_s), float(lon_s)
        except (ValueError, IndexError):
            continue
        out[uid] = (lat, lng)
    return out


def main() -> int:
    log.info("Geocode leads — APPLY=%s FORCE=%s", APPLY, FORCE)
    grand_total = grand_have = grand_no_addr = grand_matched = grand_unmatched = 0

    for label, dash_path, data_path in DATASETS:
        if not dash_path.exists():
            log.info("[%s] %s not found — skipping", label, dash_path.name)
            continue
        obj = json.loads(dash_path.read_text())
        recs = _records(obj)
        total = len(recs)

        # Build the work list: records needing geocoding, keyed by doc_num.
        # If doc_num is missing/duplicated, fall back to the list index as
        # the unique id (prefixed so it can't collide with a real doc_num).
        pending: List[Tuple[str, Tuple[str, str, str, str]]] = []
        uid_to_rec: Dict[str, dict] = {}
        have = no_addr = 0
        for i, rec in enumerate(recs):
            has_coords = rec.get("prop_lat") is not None and rec.get("prop_lng") is not None
            if has_coords and not FORCE:
                have += 1
                continue
            q = _build_query(rec)
            if q is None:
                no_addr += 1
                continue
            uid = str(rec.get("doc_num") or f"_idx{i}")
            if uid in uid_to_rec:        # duplicate doc_num — disambiguate
                uid = f"{uid}_idx{i}"
            uid_to_rec[uid] = rec
            pending.append((uid, q))

        log.info("[%s] total=%d  already-geocoded=%d  no-address=%d  to-geocode=%d",
                 label, total, have, no_addr, len(pending))

        matched = 0
        # Submit in chunks.
        for start in range(0, len(pending), BATCH_SIZE):
            chunk = pending[start:start + BATCH_SIZE]
            rows = [(uid, s, c, st, z) for uid, (s, c, st, z) in chunk]
            log.info("  submitting batch %d-%d of %d…",
                     start, start + len(chunk), len(pending))
            results = _census_batch(rows)
            for uid, (lat, lng) in results.items():
                rec = uid_to_rec.get(uid)
                if rec is not None:
                    rec["prop_lat"] = round(lat, 6)
                    rec["prop_lng"] = round(lng, 6)
                    matched += 1
            log.info("  batch matched %d/%d", len(results), len(chunk))

        unmatched = len(pending) - matched
        log.info("[%s] geocoded %d, unmatched %d (no Census hit)", label, matched, unmatched)

        if APPLY and matched > 0:
            for p in (dash_path, data_path):
                try:
                    if p == data_path and not p.exists():
                        # mirror may not exist for every dataset; write it anyway
                        p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_text(json.dumps(obj, indent=2, ensure_ascii=False))
                    log.info("  wrote %s", p)
                except Exception as exc:  # noqa: BLE001
                    log.warning("  could not write %s: %s", p, exc)
        elif not APPLY:
            log.info("  [dry-run] would write %d coords to %s (+ data/ mirror)",
                     matched, dash_path.name)

        grand_total += total
        grand_have += have
        grand_no_addr += no_addr
        grand_matched += matched
        grand_unmatched += unmatched

    log.info("==== SUMMARY ====")
    log.info("records=%d  already-had-coords=%d  no-address=%d  newly-geocoded=%d  unmatched=%d",
             grand_total, grand_have, grand_no_addr, grand_matched, grand_unmatched)
    if not APPLY:
        log.info("DRY-RUN — set GEOCODE_APPLY=1 to write coordinates.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
