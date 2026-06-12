#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
extract_ncad_exemptions.py
==========================
ONE-TIME (re-runnable) extractor that reads the Nueces CAD / PACS appraisal-roll
export and pulls, for every parcel:

  * which property-tax EXEMPTIONS it carries  (HS, OV65, DP, DVHS, ... )  -> File #2
  * whether it has an ACTIVE TAX DEFERRAL      (the authoritative list)    -> File #19

Why this exists
---------------
The dashboard's deferral signal today comes from the "(TD)" tag glued onto the
owner name in the appraisal roll. That tag is INCOMPLETE -- e.g. 6333 Fitzhugh
(TAMEZ) is deferred on the tax office site but has no "(TD)" tag. The PACS export
ships a dedicated file -- APPRAISAL_TAX_DEFERRAL_INFO.TXT (File #19) -- that lists
every property/owner/exemption deferral combo. That file is the ground truth.

It runs on YOUR PC (the full export is ~2 GB; we stream it line-by-line, so it
never loads into memory). Stdlib only -- no pip installs.

HOW TO RUN  (Windows, double-click-friendly .bat or PowerShell)
---------------------------------------------------------------
1. Download "2026 Preliminary Appraisal Roll Export" from nuecescad.net and UNZIP it.
2. Run:
       python extract_ncad_exemptions.py --export-dir "C:\\path\\to\\unzipped\\export"
3. It writes  ncad_exemptions.csv  and  ncad_exemptions.csv.gz  next to this script
   (or use --out to choose a path), and prints a summary you can paste back to me.

OUTPUT COLUMNS (ncad_exemptions.csv)
------------------------------------
geo_id, prop_id, exemptions, tax_deferral, deferral_types, deferral_start, deferral_owner
  exemptions      = ';'-joined exemption codes present on the parcel (e.g. "HS;OV65")
  tax_deferral    = "Y" if the parcel appears in File #19, else ""
  deferral_types  = ';'-joined exempt-type codes that are being deferred
  deferral_start  = earliest deferral start date seen
  deferral_owner  = owner name from the deferral file
"""

import argparse, csv, glob, gzip, os, sys

# ---------------------------------------------------------------------------
# File #2 (APPRAISAL_INFO.TXT) fixed-width positions  -- 1-indexed, inclusive.
# prop_id 1-12 ; geo_id 547-596 ; exemption flags are single 'T'/'F' chars.
# ---------------------------------------------------------------------------
PROP_PROPID = (1, 12)
PROP_GEOID  = (547, 596)

# code -> 1-indexed char position of its 'T'/'F' flag in File #2
EX_FLAGS = {
    "HS":   2609,  # Homestead (owner-occupied signal)
    "OV65": 2610,  # Over-65
    "OV65S":2661,  # Over-65 surviving spouse
    "DP":   2662,  # Disabled person
    "DV1":  2663, "DV1S":2664,
    "DV2":  2665, "DV2S":2666,
    "DV3":  2667, "DV3S":2668,
    "DV4":  2669, "DV4S":2670,
    "EX":   2671,  # Total / absolute exemption (govt/charity -> usually NOT a lead)
    "DPS":  5435,  # Disabled person surviving spouse
    "DVHS": 5463,  # 100% disabled-veteran homestead
    "DVHSS":7239,  # DVHS surviving spouse
}
# Exemptions that ENABLE a Sec. 33.06 tax deferral (age/disability/veteran).
DEFERRAL_ELIGIBLE = {
    "OV65","OV65S","DP","DPS",
    "DV1","DV1S","DV2","DV2S","DV3","DV3S","DV4","DV4S",
    "DVHS","DVHSS",
}

# Need lines at least this long to read every flag (highest pos = DVHSS 7239).
MIN_PROP_LINE = max(EX_FLAGS.values())

# ---------------------------------------------------------------------------
# File #19 (APPRAISAL_TAX_DEFERRAL_INFO.TXT) fixed-width positions.
# ---------------------------------------------------------------------------
DEF_PROPID = (1, 12)
DEF_EXMPT  = (25, 29)
DEF_START  = (30, 54)
DEF_GEOID  = (80, 129)
DEF_OWNER  = (130, 199)


def _slice(line, span):
    """1-indexed inclusive slice -> trimmed string (safe past end-of-line)."""
    a, b = span
    return line[a-1:b].strip()


def _flag(line, pos):
    return len(line) >= pos and line[pos-1:pos].upper() == "T"


def find_file(export_dir, *needles):
    """Case-insensitive search for the largest file whose name ends with needle."""
    best, best_sz = None, -1
    for root, _dirs, files in os.walk(export_dir):
        for fn in files:
            up = fn.upper()
            if any(up.endswith(n.upper()) for n in needles):
                p = os.path.join(root, fn)
                sz = os.path.getsize(p)
                if sz > best_sz:
                    best, best_sz = p, sz
    return best


def sanity_check_prop(path):
    """Read first non-empty lines; confirm fixed-width slicing looks right."""
    with open(path, "r", encoding="latin-1", newline="") as f:
        for line in f:
            line = line.rstrip("\r\n")
            if not line.strip():
                continue
            pid = _slice(line, PROP_PROPID)
            geo = _slice(line, PROP_GEOID)
            ok_pid = pid.isdigit()
            print("  first record  -> prop_id=%r  geo_id=%r  line_len=%d" % (pid, geo, len(line)))
            if "\t" in line or "|" in line[:600]:
                print("  !! WARNING: found a TAB or '|' in the line -- this file may be")
                print("     delimited, not fixed-width. Stop and send me the first line.")
            if not ok_pid:
                print("  !! ERROR: prop_id slice is not numeric -- positions look wrong.")
                print("     Stop and send me this first line so I can re-check the layout.")
                return False
            if len(line) < MIN_PROP_LINE:
                print("  .. note: line shorter than %d chars; some flags past the end" % MIN_PROP_LINE)
                print("     will read as 'no exemption'. Usually fine (trailing-space trim).")
            return True
    print("  !! ERROR: property file appears empty.")
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--export-dir", required=True,
                    help="Folder containing the unzipped PACS appraisal export")
    ap.add_argument("--out", default="ncad_exemptions.csv",
                    help="Output CSV path (a .gz copy is written too)")
    args = ap.parse_args()

    if not os.path.isdir(args.export_dir):
        sys.exit("export-dir not found: %s" % args.export_dir)

    prop_path = find_file(args.export_dir, "APPRAISAL_INFO.TXT", "PROP.TXT")
    def_path  = find_file(args.export_dir, "APPRAISAL_TAX_DEFERRAL_INFO.TXT",
                          "TAX_DEFERRAL_INFO.TXT")

    if not prop_path:
        sys.exit("Could not find APPRAISAL_INFO.TXT / PROP.TXT under %s" % args.export_dir)
    print("Property file (File #2):  %s  (%.1f GB)"
          % (prop_path, os.path.getsize(prop_path) / 1e9))
    print("Deferral file (File #19): %s" % (def_path or "** NOT FOUND **"))
    print("\nSanity-checking property file layout...")
    if not sanity_check_prop(prop_path):
        sys.exit(1)

    # ---- Pass 1: File #19 deferrals (small, do first) --------------------
    deferrals = {}   # geo_id -> {"types":set, "start":str, "owner":str}
    def_by_pid = {}  # prop_id -> geo_id   (fallback join)
    n_def_rows = 0
    if def_path:
        print("\nReading tax-deferral file (File #19)...")
        with open(def_path, "r", encoding="latin-1", newline="") as f:
            for line in f:
                line = line.rstrip("\r\n")
                if not line.strip():
                    continue
                n_def_rows += 1
                pid   = _slice(line, DEF_PROPID)
                exmpt = _slice(line, DEF_EXMPT)
                start = _slice(line, DEF_START)
                geo   = _slice(line, DEF_GEOID)
                owner = _slice(line, DEF_OWNER)
                key = geo or ("PID:" + pid)
                d = deferrals.setdefault(key, {"types": set(), "start": "", "owner": ""})
                if exmpt: d["types"].add(exmpt)
                if start and (not d["start"] or start < d["start"]):
                    d["start"] = start
                if owner and not d["owner"]:
                    d["owner"] = owner
                if pid and geo:
                    def_by_pid[pid] = geo
        print("  %d deferral rows -> %d distinct deferred parcels" % (n_def_rows, len(deferrals)))

    # ---- Pass 2: stream File #2, collect exemptions ---------------------
    print("\nStreaming property file for exemptions (this is the slow part)...")
    ex_map = {}          # geo_id -> set(codes)
    pid_to_geo = {}      # prop_id -> geo_id (to attach pid-keyed deferrals)
    n_lines = 0
    for_progress = 250000
    with open(prop_path, "r", encoding="latin-1", newline="") as f:
        for line in f:
            line = line.rstrip("\r\n")
            if not line.strip():
                continue
            n_lines += 1
            if n_lines % for_progress == 0:
                print("    ...%d,000 lines" % (n_lines // 1000))
            pid = _slice(line, PROP_PROPID)
            geo = _slice(line, PROP_GEOID)
            if not geo:
                continue
            if pid:
                pid_to_geo[pid] = geo
            codes = ex_map.get(geo)
            for code, pos in EX_FLAGS.items():
                if _flag(line, pos):
                    if codes is None:
                        codes = ex_map[geo] = set()
                    codes.add(code)
    print("  done: %d property rows scanned, %d parcels carry >=1 captured exemption"
          % (n_lines, len(ex_map)))

    # resolve any deferrals that were keyed by prop_id only
    for pid, geo in def_by_pid.items():
        if geo not in deferrals and ("PID:" + pid) in deferrals:
            deferrals[geo] = deferrals.pop("PID:" + pid)

    # ---- Merge + write --------------------------------------------------
    all_geos = set(ex_map) | set(g for g in deferrals if not g.startswith("PID:"))
    rows = []
    for geo in all_geos:
        codes = sorted(ex_map.get(geo, set()))
        d = deferrals.get(geo)
        rows.append({
            "geo_id": geo,
            "prop_id": "",  # filled below if known
            "exemptions": ";".join(codes),
            "tax_deferral": "Y" if d else "",
            "deferral_types": ";".join(sorted(d["types"])) if d else "",
            "deferral_start": d["start"] if d else "",
            "deferral_owner": d["owner"] if d else "",
        })
    geo_to_pid = {g: p for p, g in pid_to_geo.items()}
    for r in rows:
        r["prop_id"] = geo_to_pid.get(r["geo_id"], "")
    rows.sort(key=lambda r: r["geo_id"])

    cols = ["geo_id","prop_id","exemptions","tax_deferral",
            "deferral_types","deferral_start","deferral_owner"]
    with open(args.out, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols); w.writeheader(); w.writerows(rows)
    with gzip.open(args.out + ".gz", "wt", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols); w.writeheader(); w.writerows(rows)

    # ---- Summary --------------------------------------------------------
    per_code = {c: 0 for c in EX_FLAGS}
    n_any = 0
    n_elig = 0
    for geo, codes in ex_map.items():
        if codes: n_any += 1
        for c in codes: per_code[c] += 1
        if codes & DEFERRAL_ELIGIBLE: n_elig += 1
    n_actual = sum(1 for g in deferrals if not g.startswith("PID:"))

    print("\n" + "="*60)
    print("SUMMARY  (paste this back to me)")
    print("="*60)
    print("Output written: %s  (+ .gz)" % args.out)
    print("Parcels with >=1 captured exemption : %d" % n_any)
    print("Parcels deferral-ELIGIBLE (OV65/DP/DV/DVHS family): %d" % n_elig)
    print("Parcels with ACTUAL deferral (File #19): %d" % n_actual)
    print("-"*60)
    print("Per-exemption parcel counts:")
    for c in EX_FLAGS:
        print("  %-6s %d" % (c, per_code[c]))
    print("="*60)


if __name__ == "__main__":
    main()
