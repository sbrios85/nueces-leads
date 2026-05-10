"""
One-shot backfill: clerk-portal document details for City of Corpus Christi liens.
=================================================================================

Iterates the cumulative `data/city_liens.json` and fetches the per-document
detail page (`/doc/<doc_num>`) for any record that doesn't yet have a
consideration value. Detail-page fields (consideration, instrument_date)
are merged into the record, and the dashboard mirror is rewritten.

This script is **resumable**: it caches every fetch result at
`.cache/clerk_doc_details.json` so a partial run picks up where the
previous one left off. Run it 2-3 times to fully cover ~1900 CCLN records
that need consideration enrichment.

Run via the dedicated GitHub Actions workflow
`.github/workflows/backfill_doc_details.yml`. Manual-only trigger.

Expected runtime: ~50-55 minutes per run (~1500 detail pages at
~2 sec each, plus rate-limit pause). Budget caps it at 55 min so the
60-min workflow timeout has a safety margin.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
import traceback
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

# Allow importing fetch.py from the same scraper directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fetch import (  # noqa: E402  — local import after sys.path tweak
    DASHBOARD_DIR,
    DATA_DIR,
    CITY_LIENS_FILE,
    load_city_liens,
    save_city_liens,
    enrich_with_doc_details,
    _coerce_amount,
)

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("nueces-backfill-doc-details")


def main() -> int:
    log.info("=== Doc-details backfill ===")

    records = load_city_liens()
    if not records:
        log.warning("city_liens.json is empty; nothing to enrich")
        return 0

    log.info("loaded %d cumulative city-lien records", len(records))

    # Stats before
    have_cons = sum(1 for r in records if r.get("consideration") or r.get("amount"))
    log.info("  records with consideration: %d / %d", have_cons, len(records))

    # Run the enrichment. Mutates records in place.
    try:
        gained = asyncio.run(enrich_with_doc_details(records))
    except Exception as exc:
        log.error("doc-details enrichment failed: %s\n%s",
                  exc, traceback.format_exc())
        gained = 0

    # Re-save city_liens.json with the enriched data. We always write —
    # even if `gained == 0` ensures the dashboard mirror stays current.
    save_city_liens(records)

    # Stats after
    have_cons_after = sum(1 for r in records
                           if r.get("consideration") or r.get("amount"))
    log.info("=== doc-details backfill done: "
             "+%d records (now %d / %d have consideration) ===",
             gained, have_cons_after, len(records))
    return 0


if __name__ == "__main__":
    sys.exit(main())
