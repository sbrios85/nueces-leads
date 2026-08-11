#!/usr/bin/env python3
"""
build_cv.py - Code Violation monthly pipeline (Level 2 automation, MERGE model).

Reads the newest city code-violation .xls from cv_input/, matches addresses
against the committed NCAD reference (ncad_reference.csv.gz), filters to
In-Progress + individual-owned + residential leads, and MERGES them into a
persistent per-violation ledger (scraper/cv_ledger.json). The dashboard file
dashboard/code_violations.json is then rebuilt (one row per property) from the
OPEN violations in the ledger.

Why a ledger (merge) instead of full-rebuild-from-one-file:
  The city's exports are date-scoped (each file only covers a recent window),
  so a plain rebuild off a single file would DROP every still-open violation
  that isn't repeated in that file. The ledger accumulates across uploads so
  nothing is lost:
    * A violation is ADDED   when it first appears with an open status.
    * A violation is REFRESHED with the newest data when it reappears open.
    * A violation is CLOSED (removed from the dashboard) only when a file
      reports it with a resolved status (anything other than In Progress/New).
    * A property drops off the dashboard only once ALL its violations close.
  Violations the city never re-reports simply stay open (nothing lost); use a
  periodic full-snapshot upload, or the dashboard CRM, to retire those by hand.

On first run the ledger is SEEDED from the existing dashboard/code_violations.json
so the ~1,500 already-loaded properties carry over intact.

Run by .github/workflows/build_cv.yml when a new file lands in cv_input/.
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
LEDGER_JSON  = os.environ.get("CV_LEDGER",     os.path.join(HERE, "cv_ledger.json"))

sys.path.insert(0, HERE)
import match_lib
import ccln_owner_filter as ocf

# Statuses that mean the violation is still active. Everything else
# (Compliant, Closed, Removed by City, Owner Compliance, ...) = resolved.
OPEN_STATUSES = {"in progress", "new"}
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

def load_reference():
    """Decompress the NCAD reference once; return (match index, no-parse index,
    enrichment map keyed by prop_id)."""
    with gzip.open(REFERENCE_GZ, "rt", encoding="utf-8-sig") as f:
        ref_text = f.read()
    tmp = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8")
    tmp.write(ref_text); tmp.close()
    full, nop = match_lib.build_index(tmp.name)
    enr = {}
    for r in _csv.DictReader(open(tmp.name, encoding="utf-8-sig"), delimiter="|"):
        enr[r["prop_id"].strip()] = r
    os.unlink(tmp.name)
    return full, nop, enr

def enrich_open_row(v, full, nop, enr):
    """Match one open city violation to a residential, individually-owned NCAD
    parcel and return the enriched per-violation 'case' dict, or None if it
    fails any filter (same rules as the original single-file pipeline)."""
    if v["Case Type"] in DROP_CASE_TYPES:
        return None
    how, cands = match_lib.match(v["Address"], full, nop)
    if not how:
        return None
    nb = [c for _, c in cands if c]
    cls = Counter(nb).most_common(1)[0][0] if nb else ''
    if not (cls == 'A1' or cls.startswith('C1') or re.match(r'^B[2-9]$', cls)):
        return None
    pid = next((p for p, c in cands if c == cls), cands[0][0])
    e = enr.get(pid, {})
    owner = e.get("appr_owner_name", "").strip()
    kind, keep = ocf.classify_owner(owner)
    if not keep:
        return None
    zp = (e.get("situs_zip", "") or "").split("-")[0][:5]
    return {
        "case_num": v["Reference No"], "cited": v["Create Date"], "violation_type": v["Case Type"],
        "violation_narrative": (v["Violation Narrative"] or "").strip(), "prop_address": v["Address"],
        "owner": owner, "mail_address": mailing(e), "state_class": cls, "property_type": label(cls),
        "market_value": money(e.get("market_value", "")), "legal": e.get("legal_desc", "").strip(),
        "prop_id": pid, "geo_id": e.get("geo_id", "").strip(),
        "prop_city": (e.get("situs_city", "").strip() or "CORPUS CHRISTI"), "prop_zip": zp,
        "prop_lat": None, "prop_lng": None,
    }

# ---------- ledger seed (first run) ----------

def _expand_types(summary):
    """'Care of Premises ×2 · Vacant Lot' -> ['Care of Premises','Care of Premises','Vacant Lot']"""
    out = []
    for seg in (summary or "").split(" · "):
        seg = seg.strip()
        if not seg:
            continue
        m = re.match(r'^(.*?) ×(\d+)$', seg)
        if m:
            out += [m.group(1)] * int(m.group(2))
        else:
            out.append(seg)
    return out

def _parse_narr_pairs(narr):
    """'[T1] n1  •  [T2] n2' -> {T1: n1, T2: n2} (first narrative kept per type)."""
    pairs = {}
    for part in (narr or "").split("  •  "):
        m = re.match(r'^\[(.*?)\]\s*(.*)$', part, re.S)
        if m and m.group(1) not in pairs:
            pairs[m.group(1)] = m.group(2).strip()
    return pairs

def seed_ledger_from_output():
    """Build the initial ledger from the existing dashboard JSON so already-loaded
    properties survive. Reconstructs per-violation entries; the property-level
    enrichment is exact, and the type/narrative summaries rebuild identically."""
    ledger = {}
    if not os.path.exists(OUTPUT_JSON):
        return ledger
    try:
        data = json.load(open(OUTPUT_JSON, encoding="utf-8"))
    except Exception:
        return ledger
    for rec in data.get("records", []):
        case_nums = rec.get("case_nums") or ([rec.get("primary_case")] if rec.get("primary_case") else [])
        case_nums = [c for c in case_nums if c]
        if not case_nums:
            continue
        types = _expand_types(rec.get("violation_type", ""))
        if len(types) < len(case_nums):
            types += [""] * (len(case_nums) - len(types))
        elif len(types) > len(case_nums):
            types = types[:len(case_nums)]
        narr_pairs = _parse_narr_pairs(rec.get("violation_narrative", ""))
        used = set()
        for i, ref in enumerate(case_nums):
            t = types[i]
            n = ""
            if t and t in narr_pairs and t not in used:
                n = narr_pairs[t]; used.add(t)
            ledger[ref] = {
                "case_num": ref, "cited": rec.get("cited", ""), "violation_type": t,
                "violation_narrative": n, "prop_address": rec.get("prop_address", ""),
                "owner": rec.get("owner", ""), "mail_address": rec.get("mail_address", ""),
                "state_class": rec.get("state_class", ""), "property_type": rec.get("property_type", ""),
                "market_value": rec.get("market_value"), "legal": rec.get("legal", ""),
                "prop_id": rec.get("prop_id", "") or rec.get("case_num", ""),
                "geo_id": rec.get("ncad_account_num", ""),
                "prop_city": rec.get("prop_city", ""), "prop_zip": rec.get("prop_zip", ""),
                "prop_lat": rec.get("prop_lat"), "prop_lng": rec.get("prop_lng"),
                "_status": "open",
            }
    return ledger

def load_or_seed_ledger():
    if os.path.exists(LEDGER_JSON):
        try:
            data = json.load(open(LEDGER_JSON, encoding="utf-8"))
            return data.get("violations", {}), True
        except Exception:
            pass
    return seed_ledger_from_output(), False

def save_ledger(violations):
    open_ct = sum(1 for e in violations.values() if e.get("_status") == "open")
    out = {
        "updated": date.today().isoformat(),
        "open_count": open_ct,
        "total_tracked": len(violations),
        "violations": violations,
    }
    os.makedirs(os.path.dirname(LEDGER_JSON), exist_ok=True)
    json.dump(out, open(LEDGER_JSON, "w"), separators=(",", ":"))

# ---------- rebuild dashboard JSON from open ledger entries ----------

def group_and_write(cases, src_name):
    groups = defaultdict(list)
    for c in cases:
        if not c.get("prop_id"):
            continue
        groups[c["prop_id"]].append(c)
    recs = []
    for pid, gc in groups.items():
        gc.sort(key=lambda x: x.get("cited", ""), reverse=True)
        base = gc[0]
        vt = Counter(c["violation_type"] for c in gc if c.get("violation_type"))
        summary = " · ".join(f"{t} ×{n}" if n > 1 else t for t, n in vt.most_common())
        case_nums = [c["case_num"] for c in gc]
        seen, parts = set(), []
        for c in gc:
            k = (c.get("violation_type", ""), c.get("violation_narrative", ""))
            if c.get("violation_narrative") and k not in seen:
                seen.add(k); parts.append(f"[{c['violation_type']}] {c['violation_narrative']}")
        cc = len(gc)
        # Preserve any known coordinates so seeded properties keep their map
        # pins even when their newest violation is a freshly-added one.
        lat = next((c.get("prop_lat") for c in gc if c.get("prop_lat") is not None), None)
        lng = next((c.get("prop_lng") for c in gc if c.get("prop_lng") is not None), None)
        recs.append({
            "case_num": pid, "case_count": cc, "case_nums": case_nums,
            "case_summary": (f"{cc} cases: " + ", ".join(case_nums)) if cc > 1 else case_nums[0],
            "primary_case": case_nums[0], "cited": base.get("cited", ""), "violation_type": summary,
            "violation_narrative": "  •  ".join(parts),
            "prop_address": base.get("prop_address", ""), "owner": base.get("owner", ""),
            "mail_address": base.get("mail_address", ""),
            "state_class": base.get("state_class", ""), "property_type": base.get("property_type", ""),
            "market_value": base.get("market_value"), "legal": base.get("legal", ""), "prop_id": pid,
            "ncad_account_num": base.get("geo_id", ""),
            "prop_city": base.get("prop_city", ""), "prop_state": "TX", "prop_zip": base.get("prop_zip", ""),
            "prop_lat": lat, "prop_lng": lng,
        })
    recs.sort(key=lambda x: x["cited"], reverse=True)
    out = {
        "import_date": date.today().isoformat(),
        "source": f"{src_name} - merged ledger, In-Progress individual-owned residential, one row per property",
        "records": recs,
    }
    os.makedirs(os.path.dirname(OUTPUT_JSON), exist_ok=True)
    json.dump(out, open(OUTPUT_JSON, "w"), separators=(",", ":"))
    return len(recs)

def main():
    files = (glob.glob(os.path.join(CV_INPUT_DIR, "*.xls")) +
             glob.glob(os.path.join(CV_INPUT_DIR, "*.xlsx")))
    if not files:
        raise SystemExit(f"No .xls/.xlsx found in {CV_INPUT_DIR}")
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

    full, nop, enr = load_reference()
    print("Reference parcels:", len(enr))

    ledger, existed = load_or_seed_ledger()
    print(f"Ledger: {'loaded' if existed else 'SEEDED from existing dashboard JSON'} — "
          f"{len(ledger)} violations ({sum(1 for e in ledger.values() if e.get('_status')=='open')} open)")

    added = refreshed = newly_closed = skipped = 0
    for v in viol:
        ref = v["Reference No"]
        st = _norm_hdr(v["Status"])
        if st in OPEN_STATUSES:
            case = enrich_open_row(v, full, nop, enr)
            if case is None:
                skipped += 1
                continue
            case["_status"] = "open"
            if ref in ledger and ledger[ref].get("_status") == "open":
                refreshed += 1
                # keep a known coordinate if the fresh row hasn't been geocoded
                old = ledger[ref]
                if case.get("prop_lat") is None and old.get("prop_lat") is not None:
                    case["prop_lat"] = old.get("prop_lat")
                    case["prop_lng"] = old.get("prop_lng")
            else:
                added += 1
            ledger[ref] = case
        else:
            if ref in ledger:
                if ledger[ref].get("_status") == "open":
                    newly_closed += 1
                ledger[ref]["_status"] = "closed"
            else:
                ledger[ref] = {"case_num": ref, "_status": "closed"}
    print(f"From this file — added: {added}, refreshed: {refreshed}, "
          f"newly closed: {newly_closed}, skipped(non-match/non-residential): {skipped}")

    save_ledger(ledger)

    cases = [{k: val for k, val in e.items() if k != "_status"}
             for e in ledger.values() if e.get("_status") == "open"]
    n = group_and_write(cases, os.path.basename(src))
    print(f"Wrote {n} property rows -> {OUTPUT_JSON}  (from {len(cases)} open violations)")

if __name__ == "__main__":
    main()
