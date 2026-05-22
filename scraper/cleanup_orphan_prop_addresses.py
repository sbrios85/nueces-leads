"""One-time cleanup: clear prop_address on records where the NCAD link
was already evicted (e.g. by a prior recorroborate run) but the wrong
prop_address from that evicted NCAD parcel is still attached.

Why this exists
---------------
The recorroborate workflow added conditional prop_address eviction
(2026-05-21): when evicting a wrong NCAD match, it also clears
prop_address if it matches the cached site_addr for that owner (proof
it came from esearch attachment, not from PDF parser).

But that fix only runs WHEN there's a current eviction. Records that
were evicted by EARLIER recorroborate runs (before this fix existed)
still have their wrong prop_addresses sitting on them, because the
old code only cleared NCAD-derived fields.

This script does a one-time sweep:
  for every record without ncad_prop_id that still has prop_address,
    look up its owner in the NCAD search cache,
    if cached site_addr equals prop_address (after normalization),
      we know prop_address came from a wrong esearch attachment
      that's already been evicted → clear it now.

Records where prop_address doesn't match the cache are LEFT ALONE —
they probably came from the PDF parser or manual edit.

Modes
-----
Default: DRY-RUN. Lists what would be cleared, writes nothing.
Apply: set env CLEANUP_APPLY=1. Mutates both
  dashboard/foreclosures.json and data/foreclosures.json.

Usage
-----
Local: python scraper/cleanup_orphan_prop_addresses.py
       CLEANUP_APPLY=1 python scraper/cleanup_orphan_prop_addresses.py

CI: triggered manually via .github/workflows/cleanup_orphan_addresses.yml
    (workflow file separate; user uploads).
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Dict, List

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
DASH_JSON = REPO_ROOT / "dashboard" / "foreclosures.json"
DATA_JSON = REPO_ROOT / "data" / "foreclosures.json"
NCAD_SEARCH_CACHE = REPO_ROOT / ".cache" / "ncad_search_cache.json"

# Fields cleared when a record's prop_address matches its owner's
# cached NCAD site_addr (proof the address came from the wrong esearch
# attachment). Same set used by recorroborate_ncad.py's conditional
# eviction — kept in lockstep deliberately.
PROP_ADDR_FIELDS = ("prop_address", "prop_city", "prop_state", "prop_zip")

APPLY_MODE = os.environ.get("CLEANUP_APPLY", "").strip() == "1"

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("cleanup_orphan_prop_addresses")


def _normalize_addr(s: str) -> str:
    """Address normalization for equality comparison. Mirror of
    recorroborate_ncad._normalize_addr — keep in sync. Handles:
      - case + whitespace + punctuation
      - apostrophe variants (ASCII ' and Unicode ' '): NCAD stores
        Irish street names without apostrophes ("O MALLEY"), PDFs
        write them with — normalize both to no-apostrophe form.
    """
    s = (s or "").lower()
    s = re.sub(r"[.,]", "", s)
    s = re.sub(r"[\u2018\u2019']", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _load_ncad_cache() -> Dict[str, Dict]:
    """Returns the cache's `data` dict (owner_name → {site_addr, ...}).
    Empty dict if cache is missing or unreadable."""
    if not NCAD_SEARCH_CACHE.exists():
        log.warning("NCAD search cache not found at %s — nothing to "
                    "compare against, exiting clean", NCAD_SEARCH_CACHE)
        return {}
    try:
        raw = json.loads(NCAD_SEARCH_CACHE.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("could not read NCAD cache (%s) — exiting clean", exc)
        return {}
    if raw.get("_version") != "v6":
        log.warning("NCAD cache version is %r, not v6 — exiting clean "
                    "rather than risk wrong reads", raw.get("_version"))
        return {}
    data = raw.get("data") or {}
    if not isinstance(data, dict):
        return {}
    return data


def _candidate(r: Dict) -> bool:
    """True if record is in the cleanup-eligible state:
        - no ncad_prop_id (already evicted)
        - has a prop_address (legacy value still attached)
    """
    has_pid = bool((r.get("ncad_prop_id") or "").strip()
                   if isinstance(r.get("ncad_prop_id"), str)
                   else r.get("ncad_prop_id"))
    has_addr = bool((r.get("prop_address") or "").strip())
    return (not has_pid) and has_addr


def _clear_prop_addr_fields(r: Dict) -> None:
    """Clear prop_address + prop_city + prop_state + prop_zip on r,
    using empty strings (matching the rest of the data's convention)."""
    for k in PROP_ADDR_FIELDS:
        if k in r:
            r[k] = ""


def main() -> int:
    if not DASH_JSON.exists():
        log.error("missing %s", DASH_JSON)
        return 2
    payload = json.loads(DASH_JSON.read_text(encoding="utf-8"))
    records = payload.get("records") or []
    log.info("loaded %d records from %s", len(records), DASH_JSON)

    cache_data = _load_ncad_cache()
    log.info("NCAD cache loaded with %d entries", len(cache_data))

    # Walk candidates and decide per record. We KEEP the original
    # values in a "to_clear" list so we can apply later and produce
    # a transparent log of exactly what changed.
    to_clear: List[Dict] = []   # [{doc_num, owner, old_addr, cached_addr}]
    inspected = 0
    skipped_no_owner = 0
    skipped_owner_not_in_cache = 0
    skipped_addr_mismatch = 0
    for r in records:
        if not _candidate(r):
            continue
        inspected += 1
        owner = (r.get("owner") or "").strip()
        if not owner:
            skipped_no_owner += 1
            continue
        cached_entry = cache_data.get(owner) or {}
        if not isinstance(cached_entry, dict):
            skipped_owner_not_in_cache += 1
            continue
        cached_addr = (cached_entry.get("site_addr") or "").strip()
        if not cached_addr:
            skipped_owner_not_in_cache += 1
            continue
        rec_addr_norm = _normalize_addr(r.get("prop_address") or "")
        cached_addr_norm = _normalize_addr(cached_addr)
        if not rec_addr_norm or rec_addr_norm != cached_addr_norm:
            skipped_addr_mismatch += 1
            continue
        # Match: this record's prop_address is the (now-evicted) NCAD
        # parcel's site_addr → clear it.
        to_clear.append({
            "doc_num":     r.get("doc_num"),
            "owner":       owner,
            "old_addr":    r.get("prop_address"),
            "old_city":    r.get("prop_city"),
            "old_zip":     r.get("prop_zip"),
            "cached_addr": cached_addr,
        })

    log.info("---- scan summary ----")
    log.info("candidates inspected            : %d", inspected)
    log.info("  → no owner on record          : %d", skipped_no_owner)
    log.info("  → owner not in NCAD cache     : %d",
              skipped_owner_not_in_cache)
    log.info("  → addr doesn't match cache    : %d", skipped_addr_mismatch)
    log.info("  → MATCH (will clear)          : %d", len(to_clear))

    if to_clear:
        log.info("---- records to clean ----")
        for entry in to_clear:
            log.info("  %s  owner=%r  addr=%r",
                      entry["doc_num"], entry["owner"], entry["old_addr"])

    if not to_clear:
        log.info("Nothing to do.")
        return 0

    if not APPLY_MODE:
        log.info("DRY RUN — no JSON files changed. "
                 "Set CLEANUP_APPLY=1 to apply.")
        return 0

    # Apply: build a doc_num lookup for fast mutation, then write both
    # the dashboard and data copies.
    clear_set = {e["doc_num"] for e in to_clear}
    for r in records:
        if r.get("doc_num") in clear_set:
            _clear_prop_addr_fields(r)
    DASH_JSON.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    log.info("wrote %s", DASH_JSON)

    if DATA_JSON.exists():
        data_payload = json.loads(DATA_JSON.read_text(encoding="utf-8"))
        data_recs = data_payload.get("records") or []
        for r in data_recs:
            if r.get("doc_num") in clear_set:
                _clear_prop_addr_fields(r)
        DATA_JSON.write_text(
            json.dumps(data_payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        log.info("wrote %s", DATA_JSON)

    log.info("APPLIED: cleared prop_address on %d record(s)",
              len(to_clear))
    return 0


if __name__ == "__main__":
    sys.exit(main())
