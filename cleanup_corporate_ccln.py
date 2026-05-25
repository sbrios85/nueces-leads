"""
One-shot cleanup: remove corporate-owned CCLN records from city_liens.json.

Sweeps every record in ``data/city_liens.json`` through the owner
classifier (see ccln_owner_filter.py) and HARD-DELETES any whose
owner is corporate (LLC, INC, CORP, religious, school, government,
nonprofit, HOA, institutional trust).

KEEPS:
- Individuals
- Estates (deceased owners; heirs are leads)
- Family/personal trusts (Living Trust, Family Trust, etc.)

This is a destructive operation. Records ARE removed from the JSON,
not soft-deleted via the dashboard's dead-row mechanic. To recover,
you would need to re-run the original CCLN backfill workflow against
the clerk portal.

================================================================
USAGE
================================================================
Manually via GitHub Actions ("Cleanup corporate CCLN leads" workflow)
or locally:

    DRY_RUN=true python3 scraper/cleanup_corporate_ccln.py
        — preview what would be deleted, don't write

    python3 scraper/cleanup_corporate_ccln.py
        — actually delete (writes data/city_liens.json +
          dashboard/city_liens.json)

The DRY_RUN preview is RECOMMENDED before running the real cleanup,
so you can spot-check the list and make sure no real leads will be
removed by mistake.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

# Allow importing the classifier from this directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ccln_owner_filter import classify_owner, kind_label  # noqa: E402


logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("ccln-corporate-cleanup")


REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_FILE = REPO_ROOT / "data" / "city_liens.json"
DASHBOARD_FILE = REPO_ROOT / "dashboard" / "city_liens.json"


def load_json():
    """Load city_liens.json. Returns the FULL envelope dict (with
    metadata) or None if missing. Records list is under "records"
    key; legacy bare-list format also supported."""
    if not DATA_FILE.exists():
        log.error("data file not found: %s", DATA_FILE)
        return None
    try:
        with DATA_FILE.open("r", encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict) and "records" in raw:
            return raw
        if isinstance(raw, list):
            # Wrap legacy bare-list format in an envelope.
            return {"records": raw}
        log.error("unexpected JSON shape: %s", type(raw).__name__)
        return None
    except Exception as exc:
        log.error("failed to load %s: %s", DATA_FILE, exc)
        return None


def save_json(envelope):
    """Write envelope back to both data/ and dashboard/ paths."""
    envelope = dict(envelope)  # don't mutate caller's dict
    envelope["fetched_at"] = (
        datetime.now(timezone.utc).isoformat().replace("+00:00", "+00:00"))
    envelope.setdefault("source", "Nueces County Clerk — CCLN cumulative")
    envelope["total"] = len(envelope.get("records", []))
    payload = json.dumps(envelope, indent=2, default=str, ensure_ascii=False)
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DASHBOARD_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(payload, encoding="utf-8")
    DASHBOARD_FILE.write_text(payload, encoding="utf-8")
    log.info("saved %d records to %s + %s",
             envelope["total"], DATA_FILE, DASHBOARD_FILE)


def main():
    dry_run = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")
    if dry_run:
        log.info("DRY_RUN=true — will NOT modify files")

    envelope = load_json()
    if envelope is None:
        return 1
    records = envelope.get("records", [])
    log.info("loaded %d CCLN records", len(records))

    keepers = []
    excluded = []
    breakdown = Counter()  # kind → count

    for rec in records:
        owner = rec.get("owner") or ""
        kind, keep = classify_owner(owner)
        if keep:
            keepers.append(rec)
        else:
            excluded.append((rec, kind))
        breakdown[kind] += 1

    # Print breakdown by classification
    log.info("=== Classification breakdown ===")
    for kind in sorted(breakdown.keys()):
        count = breakdown[kind]
        action = "KEEP" if classify_owner_kind_keeps(kind) else "SKIP"
        log.info("  %s  %-14s  %5d", action, kind, count)
    log.info("Total: %d keep, %d exclude", len(keepers), len(excluded))

    # Sample of excluded records for spot-checking
    if excluded:
        log.info("=== First 20 excluded (sample) ===")
        for rec, kind in excluded[:20]:
            doc = rec.get("doc_num", "?")
            owner = rec.get("owner", "?")
            log.info("  [%s] %-16s  %s", kind, doc, owner)
        if len(excluded) > 20:
            log.info("  ... and %d more", len(excluded) - 20)

    if dry_run:
        log.info("DRY_RUN — exiting without saving")
        return 0

    if not excluded:
        log.info("no corporate records found — nothing to delete")
        return 0

    # Write the filtered envelope back
    envelope["records"] = keepers
    save_json(envelope)
    log.info("=== Cleanup complete: removed %d corporate records ===",
             len(excluded))
    return 0


def classify_owner_kind_keeps(kind: str) -> bool:
    """Look up whether a classification kind is a keep-decision.
    Mirrors KEEP_KINDS from ccln_owner_filter but avoids importing
    the set directly (helps when this script is read in isolation)."""
    return kind in {"individual", "estate", "family_trust"}


if __name__ == "__main__":
    sys.exit(main())
