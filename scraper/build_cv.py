#!/usr/bin/env python3
"""
build_cv.py - Code Violation monthly pipeline (Level 2 automation).

Reads the newest city code-violation .xls from cv_input/, matches addresses
against the committed NCAD reference (ncad_reference.csv.gz), filters to
In-Progress + individual-owned + residential leads, groups one row per
property (parcel), attaches the NCTAX/geo account, and writes
dashboard/code_violations.json.

Run by .github/workflows/build_cv.yml when a new file lands in cv_input/.
Resolved properties drop out automatically because the list is rebuilt from
the current file each run.
"""
import os, re, gzip, json, glob, tempfile, sys, csv as _csv
from collections import Counter, defaultdict
from datetime import date
import xlrd
from xlrd.xldate import xldate_as_datetime

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)                       # scraper/ -> repo root
CV_INPUT_DIR = os.environ.get("CV_INPUT_DIR", os.path.join(REPO, "cv_input"))
REFERENCE_GZ = os.environ.get("CV_REFERENCE",  os.path.join(HERE, "ncad_reference.csv.gz"))
OUTPUT_JSON  = os.environ.get("CV_OUTPUT",     os.path.join(REPO, "dashboard", "code_violations.json"))

sys.path.insert(0, HERE)
import match_lib
import ccln_owner_filter as ocf

KEEP_STATUS = "In Progress"
DROP_CASE_TYPES = {
    "Zoning", "Signage", "Parking on Unimproved Surfaces", "Building Permit Required",
    "Short-Term Rental (STR)", "Illegal Dumping", "Emergency Measures",
}
# logical field -> acceptable header names (normalized lower-case)
NEEDED = {
    "ref_no":    ["reference no", "reference number", "ref no"],
    "create":    ["create date", "created date", "create"],
    "close":     ["close date", "closed date"],
    "status":    ["status"],
    "case_type": ["case type"],
    "address":   ["address 1", "address", "address1", "situs address"],
    "parcel":    ["parcel id", "parcel"],
    "narr":      ["violation_narrative", "violation narrative", "narrative"],
}

def _norm_hdr(s):
    return re.sub(r"\s+", " ", str(s).strip().lower())

def parse_xls(path):
    """Parse the city export. Handles the truncated-OLE2 file the city produces
    by carving the workbook stream (raw[512:]) when a straight open fails."""
    raw = open(path, "rb").read()
    wb = None
    try:
        wb = xlrd.open_workbook(file_contents=raw)
    except Exception:
        wb = None
    if wb is None:
        wb = xlrd.open_workbook(file_contents=raw[512:])
    sh = wb.sheet_by_index(0)

    hdr_idx, colmap = None, {}
    for i in range(min(80, sh.nrows)):
        cells = [_norm_hdr(c.value) for c in sh.row(i)]
        found = {}
        for logi, names in NEEDED.items():
            for ci, cell in enumerate(cells):
                if cell in names:
                    found[logi] = ci
                    break
        if "ref_no" in found and "case_type" in found and "status" in found:
            hdr_idx, colmap = i, found
            break
    if hdr_idx is None:
        raise SystemExit("Could not find the header row (need Reference No / Case Type / Status).")

    recs = []
    for i in range(hdr_idx + 1, sh.nrows):
        row = sh.row(i)
        def cell(logi):
            ci = colmap.get(logi)
            return row[ci] if (ci is not None and ci < len(row)) else None
        ref_c = cell("ref_no")
        ref = str(ref_c.value).strip() if ref_c is not None else ""
        if not ref:
            continue
        def iso(logi):
            c = cell(logi)
            if c is None or c.value in ("", None):
                return ""
            v = c.value
            if isinstance(v, (int, float)) and v > 0:
                try:
                    return xldate_as_datetime(v, wb.datemode).date().isoformat()
                except Exception:
                    return ""
            return str(v).strip()
        def txt(logi):
            c = cell(logi)
            return str(c.value).strip() if c is not None else ""
        recs.append({
            "Reference No": ref,
            "Create Date":  iso("create"),
            "Close Date":   iso("close"),
            "Status":       txt("status"),
            "Case Type":    txt("case_type"),
            "Address":      txt("address"),
            "Parcel ID":    txt("parcel"),
            "Violation Narrative": txt("narr"),
        })
    return recs

def label(c):
    if c == 'A1':
        return 'Single-family'
    if re.match(r'^B[1-9][0-9]?$', c):
        return 'Multifamily'
    if c.startswith('C1'):
        return 'Vacant lot'
    return c or 'Unknown'

def money(s):
    s = (s or "").strip()
    try:
        return int(s) if s else None
    except Exception:
        return None

def mailing(e):
    if not e:
        return ""
    street = " ".join(p for p in [e.get("appr_addr_line1", "").strip(),
                                   e.get("appr_addr_line2", "").strip()] if p)
    csz = f"{e.get('appr_addr_city','').strip()}, {e.get('appr_addr_state','').strip()} {e.get('appr_addr_zip','').strip()}".strip(" ,")
    return (street + ("  " + csz if csz else "")).strip()

def main():
    files = (glob.glob(os.path.join(CV_INPUT_DIR, "*.xls")) +
             glob.glob(os.path.join(CV_INPUT_DIR, "*.xlsx")))
    if not files:
        raise SystemExit(f"No .xls/.xlsx found in {CV_INPUT_DIR}")
    # CI checkouts reset mtimes, so prefer the file whose NAME carries the
    # latest MM-DD-YYYY date; fall back to mtime only if no date is present.
    def _key(p):
        m = re.search(r'(\d{2})-(\d{2})-(\d{4})', os.path.basename(p))
        if m:
            mm, dd, yyyy = m.groups()
            return (1, f"{yyyy}{mm}{dd}")
        return (0, str(int(os.path.getmtime(p))))
    src = max(files, key=_key)
    print("Input city file:", os.path.basename(src))
    viol = parse_xls(src)
    print("Parsed violation rows:", len(viol))

    # decompress reference once to a temp CSV (reused by match_lib + enrich map)
    with gzip.open(REFERENCE_GZ, "rt", encoding="utf-8-sig") as f:
        ref_text = f.read()
    tmp = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8")
    tmp.write(ref_text); tmp.close()
    full, nop = match_lib.build_index(tmp.name)
    enr = {}
    for r in _csv.DictReader(open(tmp.name, encoding="utf-8-sig"), delimiter="|"):
        enr[r["prop_id"].strip()] = r
    os.unlink(tmp.name)
    print("Reference parcels:", len(enr))

    cases = []
    for v in viol:
        if v["Status"] != KEEP_STATUS:
            continue
        if v["Case Type"] in DROP_CASE_TYPES:
            continue
        how, cands = match_lib.match(v["Address"], full, nop)
        if not how:
            continue
        nb = [c for _, c in cands if c]
        cls = Counter(nb).most_common(1)[0][0] if nb else ''
        if not (cls == 'A1' or cls.startswith('C1') or re.match(r'^B[2-9]$', cls)):
            continue
        pid = next((p for p, c in cands if c == cls), cands[0][0])
        e = enr.get(pid, {})
        owner = e.get("appr_owner_name", "").strip()
        kind, keep = ocf.classify_owner(owner)
        if not keep:
            continue
        zp = (e.get("situs_zip", "") or "").split("-")[0][:5]
        cases.append({
            "case_num": v["Reference No"], "cited": v["Create Date"], "violation_type": v["Case Type"],
            "violation_narrative": (v["Violation Narrative"] or "").strip(), "prop_address": v["Address"],
            "owner": owner, "mail_address": mailing(e), "state_class": cls, "property_type": label(cls),
            "market_value": money(e.get("market_value", "")), "legal": e.get("legal_desc", "").strip(),
            "prop_id": pid, "geo_id": e.get("geo_id", "").strip(),
            "prop_city": (e.get("situs_city", "").strip() or "CORPUS CHRISTI"), "prop_zip": zp,
        })
    print("Leads after filters:", len(cases))

    groups = defaultdict(list)
    for c in cases:
        groups[c["prop_id"]].append(c)
    recs = []
    for pid, gc in groups.items():
        gc.sort(key=lambda x: x["cited"], reverse=True)
        base = gc[0]
        vt = Counter(c["violation_type"] for c in gc)
        summary = " · ".join(f"{t} ×{n}" if n > 1 else t for t, n in vt.most_common())
        case_nums = [c["case_num"] for c in gc]
        seen, parts = set(), []
        for c in gc:
            k = (c["violation_type"], c["violation_narrative"])
            if c["violation_narrative"] and k not in seen:
                seen.add(k); parts.append(f"[{c['violation_type']}] {c['violation_narrative']}")
        cc = len(gc)
        recs.append({
            "case_num": pid, "case_count": cc, "case_nums": case_nums,
            "case_summary": (f"{cc} cases: " + ", ".join(case_nums)) if cc > 1 else case_nums[0],
            "primary_case": case_nums[0], "cited": base["cited"], "violation_type": summary,
            "violation_narrative": "  •  ".join(parts),
            "prop_address": base["prop_address"], "owner": base["owner"], "mail_address": base["mail_address"],
            "state_class": base["state_class"], "property_type": base["property_type"],
            "market_value": base["market_value"], "legal": base["legal"], "prop_id": pid,
            "ncad_account_num": base["geo_id"],
            "prop_city": base["prop_city"], "prop_state": "TX", "prop_zip": base["prop_zip"],
            "prop_lat": None, "prop_lng": None,
        })
    recs.sort(key=lambda x: x["cited"], reverse=True)
    out = {
        "import_date": date.today().isoformat(),
        "source": f"{os.path.basename(src)} - In-Progress, individual-owned residential, grouped one row per property",
        "records": recs,
    }
    os.makedirs(os.path.dirname(OUTPUT_JSON), exist_ok=True)
    json.dump(out, open(OUTPUT_JSON, "w"), separators=(",", ":"))
    print(f"Wrote {len(recs)} property rows -> {OUTPUT_JSON}")

if __name__ == "__main__":
    main()
