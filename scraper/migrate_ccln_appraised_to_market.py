#!/usr/bin/env python3
"""One-shot migration: rename CCLN `appraised_value` (string) → `market_value` (number).

WHY THIS EXISTS
---------------
The CCLN NCAD enrichment shipped 2026-05-30 wrote `appraised_value` to records
as a *dollar-formatted string* like "$28,315". The dashboard's renderer did
`Number(r.appraised_value)` and got NaN, displaying "$NaN" on every CCLN
record's Appraised Value column for over a day.

Sergio's call 2026-05-31: drop the Appraised Value column entirely, keep
Market Value, and store the value as a raw number so the dashboard's
`moneyFmt`-style rendering can format it consistently.

WHAT THIS DOES
--------------
1. Reads data/city_liens.json (the canonical store).
2. For each record:
   - If `appraised_value` is set (any non-empty value):
     - Parse to a numeric float (strip $, commas, whitespace).
     - Write to `market_value` IF market_value is currently null/empty.
       If market_value already has a value, leave it alone — defensive,
       avoids overwriting any future per-record manual entry.
     - Delete the `appraised_value` field from the record.
3. Writes back to data/city_liens.json AND dashboard/city_liens.json.
4. Prints a summary of what changed.

SAFETY
------
- Strict parse: only digits/decimal point survive. "$1,234.56" → 1234.56.
- If parse fails (unexpected format), the record is logged but otherwise
  left untouched (appraised_value field kept). Worst case: a record retains
  the legacy field, which is unused by the dashboard now anyway.
- Idempotent: re-running on an already-migrated file finds no
  `appraised_value` fields and reports 0 changes. Safe to run twice.

HOW TO RUN
----------
Locally:
    python3 scraper/migrate_ccln_appraised_to_market.py

In GitHub Actions (via a workflow_dispatch — see workflow file):
    Actions tab → "Migrate CCLN appraised→market" → Run workflow.
"""
import json
import logging
import re
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("migrate_ccln")

# Repo root: file is at scraper/migrate_ccln_appraised_to_market.py so the
# repo root is two levels up.
ROOT = Path(__file__).resolve().parent.parent
PRIMARY = ROOT / "data" / "city_liens.json"
DASHBOARD_COPY = ROOT / "dashboard" / "city_liens.json"


def parse_money(v) -> float | None:
    """Parse a dollar-formatted string to a float. Returns None on failure."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if not s:
        return None
    # Keep digits, decimal point, and minus sign; drop $/comma/space/etc.
    cleaned = re.sub(r"[^\d.\-]", "", s)
    if not cleaned or cleaned in ("-", ".", "-."):
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def main() -> int:
    if not PRIMARY.exists():
        log.error("primary file not found: %s", PRIMARY)
        return 1

    log.info("=== CCLN appraised→market migration ===")
    log.info("reading %s", PRIMARY)

    with PRIMARY.open() as f:
        data = json.load(f)

    if "records" not in data or not isinstance(data["records"], list):
        log.error("unexpected shape — expected top-level 'records' list, got %r",
                  type(data).__name__)
        return 1

    records = data["records"]
    log.info("loaded %d records", len(records))

    migrated = 0          # records where we successfully moved a value
    deleted_only = 0      # records where appraised_value existed but
                          # couldn't be parsed or market_value already
                          # had a value (in either case we drop the field)
    skipped_no_appr = 0   # records that had no appraised_value field
    parse_failed = []     # (doc_num, raw) tuples for diagnostic

    for r in records:
        if "appraised_value" not in r or r["appraised_value"] in (None, ""):
            # Field absent OR empty — clean up the empty entry too (it's
            # noise in the JSON either way).
            if "appraised_value" in r:
                del r["appraised_value"]
                deleted_only += 1
            else:
                skipped_no_appr += 1
            continue

        raw = r["appraised_value"]
        parsed = parse_money(raw)

        if parsed is None:
            # Couldn't parse. Log for visibility but still drop the
            # field — the dashboard no longer reads it anyway.
            parse_failed.append((r.get("doc_num", "?"), raw))
            del r["appraised_value"]
            deleted_only += 1
            continue

        # Successful parse. Only write to market_value if it's not
        # already set. This protects any manual entry that might exist.
        existing_mv = r.get("market_value")
        if existing_mv is None or existing_mv == "":
            r["market_value"] = parsed
            migrated += 1
        else:
            # market_value already has a value — leave it alone. We
            # still drop the appraised_value field so the orphan
            # string doesn't linger.
            log.debug("doc %s: market_value already=%r, keeping it; "
                      "dropping appraised_value=%r",
                      r.get("doc_num"), existing_mv, raw)
            deleted_only += 1

        del r["appraised_value"]

    log.info("migration summary:")
    log.info("  migrated to market_value: %d", migrated)
    log.info("  appraised_value dropped (existing mv kept or parse-fail): %d",
             deleted_only)
    log.info("  records without appraised_value (unchanged): %d", skipped_no_appr)
    if parse_failed:
        log.warning("  %d parse failures (field still dropped):", len(parse_failed))
        for doc, raw in parse_failed[:10]:
            log.warning("    doc=%s raw=%r", doc, raw)
        if len(parse_failed) > 10:
            log.warning("    ... and %d more", len(parse_failed) - 10)

    # If nothing changed, no need to write or copy.
    if migrated == 0 and deleted_only == 0:
        log.info("no changes — exiting without writing")
        return 0

    # Write back to primary location.
    with PRIMARY.open("w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    log.info("wrote %s", PRIMARY)

    # Sync dashboard copy (identical content).
    if DASHBOARD_COPY.exists() or DASHBOARD_COPY.parent.exists():
        DASHBOARD_COPY.parent.mkdir(parents=True, exist_ok=True)
        with DASHBOARD_COPY.open("w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        log.info("wrote %s", DASHBOARD_COPY)
    else:
        log.warning("dashboard/ directory missing — skipped dashboard copy")

    return 0


if __name__ == "__main__":
    sys.exit(main())
