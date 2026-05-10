"""
Foreclosure PDF extraction runner.
==================================

Loads dashboard/foreclosures.json, runs PDF download + text extraction
on up to N pre-foreclosure records that don't yet have an owner name,
then saves the enriched records back. Run twice daily by separate
GitHub Actions workflows.

Steps:
  1. Load existing foreclosure records
  2. Run PDF extraction on eligible records (login → download → parse)
  3. For records that gained a borrower name + legal description (but
     no street address), run NCAD reverse-lookup-by-name and match the
     legal description to recover the street address
  4. Save updated records back to dashboard/foreclosures.json

Triggered manually or by cron (workflow YAML defines the schedule).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

# Allow `import fetch` and `import pdf_reader` from the scraper directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pdf_reader import (   # noqa: E402
    process_foreclosure_pdfs,
    cross_reference_legal_descriptions,
    PDF_DOWNLOADS_PER_RUN,
)

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("nueces-pdf-runner")


# Repo-relative paths.
ROOT_DIR = Path(__file__).resolve().parent.parent
DASHBOARD_FILE = ROOT_DIR / "dashboard" / "foreclosures.json"
DATA_FILE      = ROOT_DIR / "data" / "foreclosures.json"


def _load_foreclosures() -> dict:
    """Read the dashboard's foreclosures.json. We use the dashboard copy
    as authoritative because the data dir may not exist on a fresh clone.
    """
    for path in (DASHBOARD_FILE, DATA_FILE):
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                log.warning("could not parse %s: %s", path, exc)
    return {}


def _save_foreclosures(payload: dict) -> None:
    """Write the updated payload back to BOTH dashboard/ and data/."""
    payload["fetched_at"] = datetime.now(timezone.utc).isoformat()
    for path in (DASHBOARD_FILE, DATA_FILE):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, default=str),
                         encoding="utf-8")
        log.info("wrote %s (%d records)", path,
                  len(payload.get("records", [])))


async def _amain() -> int:
    log.info("=== Foreclosure PDF extraction run ===")

    payload = _load_foreclosures()
    records = payload.get("records", [])
    if not records:
        log.warning("no foreclosure records in foreclosures.json — nothing to do")
        return 0

    log.info("loaded %d foreclosure records", len(records))

    # Stats before
    have_owner_before = sum(1 for r in records if r.get("owner"))
    have_addr_before  = sum(1 for r in records if r.get("prop_address"))
    log.info("  before: %d/%d have owner, %d/%d have prop_address",
             have_owner_before, len(records),
             have_addr_before, len(records))

    # Pull max_downloads from env so the workflow YAML can override it
    # (e.g. set to 2 for a smoke test before going to 10).
    try:
        max_downloads = int(os.environ.get("PDF_MAX_DOWNLOADS",
                                            str(PDF_DOWNLOADS_PER_RUN)))
    except ValueError:
        max_downloads = PDF_DOWNLOADS_PER_RUN
    log.info("max_downloads for this run: %d", max_downloads)

    # === Phase 1: PDF extraction ===
    try:
        gained_pdf = await process_foreclosure_pdfs(
            records, root_dir=ROOT_DIR, max_downloads=max_downloads)
    except Exception as exc:
        log.error("PDF extraction crashed: %s\n%s", exc, traceback.format_exc())
        gained_pdf = 0

    # === Phase 2: NCAD legal-description cross-reference ===
    # For records that got a borrower + legal but no street address.
    try:
        gained_xref = await cross_reference_legal_descriptions(
            records, root_dir=ROOT_DIR)
    except Exception as exc:
        log.error("legal cross-ref crashed: %s\n%s",
                  exc, traceback.format_exc())
        gained_xref = 0

    # Stats after
    have_owner_after = sum(1 for r in records if r.get("owner"))
    have_addr_after  = sum(1 for r in records if r.get("prop_address"))
    log.info("  after:  %d/%d have owner, %d/%d have prop_address",
             have_owner_after, len(records),
             have_addr_after, len(records))
    log.info("=== run done: +%d owners (PDF), +%d addresses (xref) ===",
             gained_pdf, gained_xref)

    # Persist results
    payload["records"] = records
    _save_foreclosures(payload)

    return 0


def main() -> int:
    return asyncio.run(_amain())


if __name__ == "__main__":
    sys.exit(main())
