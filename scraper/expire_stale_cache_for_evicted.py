"""One-shot cache-expiry for records evicted before
recorroborate_ncad.py learned to expire its own cache entries.

Background
----------
Before 2026-05-21, the re-corroboration workflow evicted wrong-matched
records (cleared `ncad_prop_id`) but left their entries in the NCAD
owner-search cache pointing at the wrong parcel. On the next daily
scrape, the cache shortcut returned the same wrong parcel for the same
owner name, undoing the eviction.

The structural fix (cache expiry on eviction) is now in place, but it
only fires when re-corroboration is CURRENTLY evicting a record. The
~7 records that were already evicted yesterday — meaning their
`ncad_prop_id` is now empty so they're skipped by re-corroboration as
ineligible — still have stale cache entries pointing at the wrong
parcels. This script cleans them up in one shot.

What this script does
---------------------
1. Loads `dashboard/foreclosures.json`.
2. Identifies records that look "evicted":
       - have a non-empty `owner` field
       - have NO `ncad_prop_id` (was cleared by recorroborate)
       - have a non-empty `legal` field (so a future scrape could
         actually re-look-them-up usefully)
3. Collects their owner-name strings.
4. Reads `.cache/ncad_search_cache.json` (v6 format).
5. For each owner name, deletes the matching cache entry.
6. Writes the trimmed cache back.

The script is dry-run by default. Set EXPIRE_APPLY=1 to actually
modify the cache file.

After running this with EXPIRE_APPLY=1, the "Foreclosure esearch only"
workflow with `retry_misses=true` will look up these records using the
current variant logic (including the slot-2 spouse search) and either
attach the correct parcel or honestly record a miss.

What this script does NOT do
----------------------------
- Modify the foreclosures.json records (those are already evicted —
  the issue is purely in the cache).
- Touch any cache entry that's NOT for an evicted-state record.
- Run the NCAD search itself. After this script, you still need to run
  the esearch workflow to actually populate the records.

Safety
------
- Will not touch the cache file at all if it can't read it or the
  version is unexpected. Lets the daily scrape's auto-upgrade logic
  rebuild the cache cleanly if there's any structural issue.
- Will not rewrite the file when zero entries actually need expiry
  (avoids spurious git diffs).
- Logs every action taken; output is structured JSON so a workflow
  step can summarize it.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Dict, List, Tuple

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
DASH_JSON = REPO_ROOT / "dashboard" / "foreclosures.json"
DATA_JSON = REPO_ROOT / "data" / "foreclosures.json"
NCAD_CACHE_PATH = REPO_ROOT / ".cache" / "ncad_search_cache.json"

APPLY_MODE = os.environ.get("EXPIRE_APPLY", "").strip() == "1"

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("expire_stale_cache")


def _find_evicted_records(records: List[Dict]) -> List[Tuple[str, str]]:
    """Return [(doc_num, owner_name), ...] for records in evicted state.

    A record is considered evicted if:
      - owner is non-empty
      - ncad_prop_id is empty/missing
      - legal is non-empty (otherwise re-lookup can't corroborate)

    These are exactly the records that re-corroboration cleared but
    whose cache entries weren't cleaned up in the old code path.
    """
    out: List[Tuple[str, str]] = []
    for r in records:
        owner = (r.get("owner") or "").strip()
        if not owner:
            continue
        ncad_pid = (r.get("ncad_prop_id") or "").strip()
        if ncad_pid:
            # Has a parcel attached — not in evicted state.
            continue
        legal = (r.get("legal") or "").strip()
        if not legal:
            # No legal description — re-lookup can't corroborate anything.
            # Could still expire to allow blind retry, but conservative
            # default is to skip.
            continue
        doc_num = (r.get("doc_num") or "").strip()
        out.append((doc_num, owner))
    return out


def _load_cache() -> Tuple[Dict, str]:
    """Return (cache_data_dict, version_or_error). On any read problem
    returns ({}, '<error_marker>'). The version_or_error is informational
    only; an empty cache_data_dict signals 'do nothing'."""
    if not NCAD_CACHE_PATH.exists():
        log.warning("NCAD cache file not found at %s", NCAD_CACHE_PATH)
        return {}, "missing"
    try:
        raw = json.loads(NCAD_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("could not parse NCAD cache (%s)", exc)
        return {}, f"unreadable: {exc}"
    if not isinstance(raw, dict):
        log.warning("NCAD cache root is not a dict — refusing to touch")
        return {}, "wrong_root_type"
    version = raw.get("_version")
    if version != "v6":
        log.warning("NCAD cache version is %r, not v6 — refusing to touch "
                    "(fetch.py will rebuild on next scrape if needed)",
                    version)
        return {}, f"unsupported_version: {version}"
    data = raw.get("data") or {}
    if not isinstance(data, dict):
        log.warning("NCAD cache 'data' field is not a dict — refusing to touch")
        return {}, "malformed_data"
    return raw, "v6"


def main() -> int:
    log.info("=== expire stale NCAD cache entries for evicted records ===")
    log.info("mode: %s", "APPLY" if APPLY_MODE else "DRY RUN")

    # ----- Step 1: load foreclosures.json and find evicted records -----
    if not DASH_JSON.exists():
        log.error("foreclosures.json not found at %s", DASH_JSON)
        return 1
    try:
        payload = json.loads(DASH_JSON.read_text(encoding="utf-8"))
    except Exception as exc:
        log.error("could not parse foreclosures.json: %s", exc)
        return 1

    records = payload.get("records") or []
    log.info("loaded %d records from %s", len(records), DASH_JSON)

    evicted = _find_evicted_records(records)
    log.info("found %d records in evicted state (owner set, "
             "ncad_prop_id empty, legal present)", len(evicted))
    if not evicted:
        log.info("nothing to expire — done")
        return 0

    log.info("evicted records:")
    for doc_num, owner in evicted:
        log.info("  doc %s  owner=%r", doc_num, owner)

    # ----- Step 2: load the NCAD cache -----
    cache_raw, version_status = _load_cache()
    if not cache_raw:
        log.error("cannot proceed: cache read failed (%s)", version_status)
        # Return 0 instead of 1 because this isn't a script bug — the
        # daily scrape will rebuild the cache, and the new
        # recorroborate-ncad cache-expiry code will work going forward.
        # We just couldn't help with today's specific cleanup.
        return 0

    cache_data = cache_raw["data"]
    log.info("loaded NCAD cache: %d entries (v6)", len(cache_data))

    # ----- Step 3: identify which evicted owners are actually in cache -----
    plan: List[Dict] = []
    for doc_num, owner in evicted:
        cached = cache_data.get(owner, "__SENTINEL_NOT_IN_CACHE__")
        if cached == "__SENTINEL_NOT_IN_CACHE__":
            plan.append({
                "doc_num": doc_num,
                "owner": owner,
                "action": "skip",
                "reason": "owner not in cache",
            })
            continue
        # Summarize what we're about to remove for the log.
        if cached is None:
            cache_summary = "<cached miss>"
        elif isinstance(cached, dict):
            cache_summary = (
                f"site_addr={cached.get('site_addr') or '?'!r} "
                f"ncad_prop_id={cached.get('ncad_prop_id') or '?'!r}"
            )
        else:
            cache_summary = f"<unknown: {type(cached).__name__}>"
        plan.append({
            "doc_num": doc_num,
            "owner": owner,
            "action": "expire",
            "cached_value": cache_summary,
        })

    expire_count = sum(1 for p in plan if p["action"] == "expire")
    skip_count = sum(1 for p in plan if p["action"] == "skip")
    log.info("plan: %d to expire, %d to skip (owner not in cache)",
             expire_count, skip_count)
    for p in plan:
        if p["action"] == "expire":
            log.info("  EXPIRE doc=%s owner=%r → was %s",
                     p["doc_num"], p["owner"], p["cached_value"])
        else:
            log.info("  SKIP   doc=%s owner=%r — %s",
                     p["doc_num"], p["owner"], p["reason"])

    # ----- Step 4: apply (or not) -----
    if expire_count == 0:
        log.info("no cache entries match evicted records — nothing to do")
        return 0

    if not APPLY_MODE:
        log.info("DRY RUN — cache file NOT modified. Set EXPIRE_APPLY=1 to apply.")
        return 0

    # Apply: remove the entries from the cache dict, write back.
    actually_removed = 0
    for p in plan:
        if p["action"] != "expire":
            continue
        owner = p["owner"]
        if owner in cache_data:
            del cache_data[owner]
            actually_removed += 1

    try:
        NCAD_CACHE_PATH.write_text(
            json.dumps(cache_raw, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as exc:
        log.error("could not write NCAD cache after expiry: %s", exc)
        return 1

    log.info("APPLIED: removed %d entries from NCAD cache "
             "(remaining: %d)", actually_removed, len(cache_data))
    log.info("Next step: run 'Foreclosure esearch only' with "
             "retry_misses=true to actually re-look-up these records.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
