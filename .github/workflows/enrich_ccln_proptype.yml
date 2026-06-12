#!/usr/bin/env python3
"""
Enrich CCLN (City Liens) records with property type.

CCLN records carry an NCAD account number (geo_id) but no property class.
This joins them against the committed NCAD reference table
(scraper/ncad_reference.csv.gz, ~219k parcels) by geo_id and writes two
fields onto each record:

    state_class    – the raw NCAD state class code (e.g. A1, C1, F1)
    property_type  – a human label (Single-family, Vacant lot, ...)

It mirrors the label() mapping used by build_cv.py so the Code Violation,
City Lien, Delinquent, and Stack tabs all describe property type the same way.

Writes both the dashboard copy and the data/ mirror so the committed repo
and the deployed dashboard stay in sync (same pattern as the geocoder).

Idempotent: only fills records missing property_type unless FORCE=1.
Env flags: CCLN_PROPTYPE_APPLY=1 to write (else dry-run report),
           CCLN_PROPTYPE_FORCE=1 to re-fill records that already have it.
"""
import csv, gzip, json, os, sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
REF  = REPO / "scraper" / "ncad_reference.csv.gz"
DASH = REPO / "dashboard" / "city_liens.json"
DATA = REPO / "data" / "city_liens.json"

APPLY = os.environ.get("CCLN_PROPTYPE_APPLY", "") in ("1", "true", "True")
FORCE = os.environ.get("CCLN_PROPTYPE_FORCE", "") in ("1", "true", "True")


def label(c):
    """NCAD state class code -> human property-type label."""
    c = (c or "").strip().upper()
    if not c:
        return ""
    if c == "A1":
        return "Single-family"
    if c == "A2":
        return "Mobile home"
    if c in ("A3", "A4"):
        return "Condo"
    if c.startswith("A"):
        return "Residential"
    if len(c) >= 2 and c[0] == "B" and c[1].isdigit():
        return "Multifamily"
    if c.startswith("C"):
        return "Vacant lot"
    if c.startswith("D"):
        return "Rural / acreage"
    if c.startswith("E"):
        return "Farm / ranch"
    if c == "F1":
        return "Commercial"
    if c == "F2":
        return "Industrial"
    if c.startswith("F"):
        return "Commercial"
    if c.startswith("G"):
        return "Mineral"
    if c.startswith("J"):
        return "Utility"
    if c.startswith("L"):
        return "Business personal"
    return c  # unknown code -> show the raw code


def load_reference():
    """geo_id -> state class code (improvement first, then land)."""
    ref = {}
    with gzip.open(REF, "rt", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f, delimiter="|"):
            gid = (row.get("geo_id") or "").strip()
            if not gid:
                continue
            cls = (row.get("imprv_state_cd") or "").strip() or (row.get("land_state_cd") or "").strip()
            ref[gid] = cls
    return ref


def main():
    print(f"Enrich CCLN property type — APPLY={APPLY} FORCE={FORCE}")
    if not REF.exists():
        print(f"ERROR: reference not found at {REF}")
        sys.exit(1)
    if not DASH.exists():
        print(f"ERROR: {DASH} not found")
        sys.exit(1)

    ref = load_reference()
    print(f"  loaded {len(ref):,} NCAD reference parcels")

    obj = json.loads(DASH.read_text())
    records = obj["records"] if isinstance(obj, dict) and "records" in obj else obj

    total = len(records)
    filled = skipped = miss = 0
    for r in records:
        if r.get("property_type") and not FORCE:
            skipped += 1
            continue
        gid = (r.get("ncad_account_num") or "").strip()
        cls = ref.get(gid)
        if cls is None:
            miss += 1
            continue
        r["state_class"] = cls
        r["property_type"] = label(cls)
        filled += 1

    print(f"  total={total}  filled={filled}  already-had={skipped}  no-reference-match={miss}")

    if not APPLY:
        print("  DRY RUN — set CCLN_PROPTYPE_APPLY=1 to write. No files changed.")
        return

    DASH.write_text(json.dumps(obj, indent=2, ensure_ascii=False))
    print(f"  wrote {DASH}")
    try:
        DATA.parent.mkdir(parents=True, exist_ok=True)
        DATA.write_text(json.dumps(obj, indent=2, ensure_ascii=False))
        print(f"  wrote {DATA}")
    except Exception as e:
        print(f"  WARN: could not write data mirror: {e}")
    print("==== DONE ====")


if __name__ == "__main__":
    main()
