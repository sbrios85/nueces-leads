#!/usr/bin/env python3
"""
extract_excess_proceeds.py
==========================
Turns the County Clerk's monthly "Excess Proceeds / Registry Accounts & Trusts"
tax-case PDF into structured lead records, and maintains a cumulative store that
tracks every case across months: when it first appeared, when it was last seen,
and when it dropped off the report (claimed / disbursed).

The clerk publishes one report per month. Each report lists open tax cases that
still have excess proceeds sitting in the court registry. A case leaving the
report between months almost always means the owner / heirs claimed the money.

Two case-number formats appear in the reports:
    - tax cause      e.g. 00-00356-00-0-C
    - district civil e.g. 2015DCV-5576-F

Usage
-----
Backfill from a folder of PDFs (builds history from scratch, date-ordered):
    python extract_excess_proceeds.py --pending-dir pdfs/excess_pending \
        --out dashboard/excess_proceeds.json --fresh

Monthly update (ingest new PDFs in pending/ into the existing store):
    python extract_excess_proceeds.py --pending-dir pdfs/excess_pending \
        --out dashboard/excess_proceeds.json

Requires: poppler-utils (provides `pdftotext`).
"""
import argparse, datetime, glob, json, os, re, subprocess, sys

CASE_LINE = re.compile(
    r'^\s*(?P<case>\d{2}-\d{4,6}-\d{2}-\d-[A-Z]|\d{4}DCV-\d{2,6}-[A-Z])\s+(?P<rest>.*)$'
)
MONEY = re.compile(r'\$\s*([\d,]+\.\d{2})')
FOOTER_DATE = re.compile(r'\b(\d{1,2})/(\d{1,2})/(\d{4})\s+\d{1,2}:\d{2}:\d{2}')
NOISE = re.compile(
    r'^(case\s*nbr|style|total|balance|registry accounts|selected case|tax cases'
    r'|page\b|\d+\s*/\s*\d+\s*$)', re.I
)
MONTH_NAMES = ["", "January", "February", "March", "April", "May", "June",
               "July", "August", "September", "October", "November", "December"]


def pdf_text(path):
    out = subprocess.run(["pdftotext", "-layout", path, "-"],
                         capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(f"pdftotext failed on {path}: {out.stderr}")
    return out.stdout


def parse_owner(style):
    """Split 'PLAINTIFF vs DEFENDANT, et al' -> (plaintiff, owner)."""
    style = re.sub(r'\s+', ' ', style).strip()
    parts = re.split(r'\bvs\b\.?', style, maxsplit=1, flags=re.I)
    plaintiff = parts[0].strip().strip(',').strip()
    owner = parts[1].strip() if len(parts) > 1 else ""
    owner = re.sub(r',?\s*et\s*al\.?\s*$', '', owner, flags=re.I).strip()
    owner = owner.lstrip(' .,-').strip().strip(',').strip()
    return plaintiff, owner


def parse_report(path):
    """Parse one PDF -> (period_key 'YYYY-MM', label 'Month YYYY', [records])."""
    text = pdf_text(path)

    # Report period from the footer generation date (filename-independent).
    period_key, label = None, None
    md = FOOTER_DATE.search(text)
    if md:
        mo, _, yr = int(md.group(1)), md.group(2), int(md.group(3))
        period_key = f"{yr}-{mo:02d}"
        label = f"{MONTH_NAMES[mo]} {yr}"

    records, cur = [], None

    def flush():
        if cur is None:
            return
        plaintiff, owner = parse_owner(" ".join(cur["style"]))
        records.append({
            "case_number": cur["case"],
            "case_type": "district_civil" if "DCV" in cur["case"] else "tax",
            "plaintiff": plaintiff,
            "owner": owner,
            "balance": cur["bal"],
        })

    for line in text.splitlines():
        m = CASE_LINE.match(line)
        if m:
            flush()
            rest = m.group("rest")
            bals = MONEY.findall(rest)
            bal = float(bals[-1].replace(",", "")) if bals else None
            style0 = re.sub(r'\$\s*[\d,]+\.\d{2}\s*$', '', rest).strip()
            cur = {"case": m.group("case"), "bal": bal,
                   "style": [style0] if style0 else []}
        elif cur is not None:
            t = line.strip()
            if t and not NOISE.match(t) and not FOOTER_DATE.search(t):
                cur["style"].append(t)
    flush()
    return period_key, label, records


def per_case(records):
    """Collapse a month's rows to one entry per case (sum *distinct* amounts,
    so the clerk's verbatim duplicate rows don't double-count)."""
    by = {}
    for r in records:
        e = by.setdefault(r["case_number"], {
            "amounts": set(), "owner": r["owner"],
            "plaintiff": r["plaintiff"], "case_type": r["case_type"]})
        if r["balance"] is not None:
            e["amounts"].add(round(r["balance"], 2))
        if r["owner"] and not e["owner"]:
            e["owner"] = r["owner"]
    return {c: {"balance": round(sum(v["amounts"]), 2),
                "owner": v["owner"], "plaintiff": v["plaintiff"],
                "case_type": v["case_type"]} for c, v in by.items()}


def ingest(store, period_key, label, records):
    cases = store["cases"]
    cur = per_case(records)
    cur_set = set(cur.keys())
    prev_active = {c for c, i in cases.items() if i["status"] == "active"}

    added = [c for c in cur_set if c not in cases]
    reappeared = [c for c in cur_set
                  if c in cases and cases[c]["status"] == "claimed"]
    removed = sorted(prev_active - cur_set)  # were active, now gone -> claimed

    for c, info in cur.items():
        if c not in cases:
            cases[c] = {
                "case_number": c, "case_type": info["case_type"],
                "plaintiff": info["plaintiff"], "owner": info["owner"],
                "first_seen": period_key, "last_seen": period_key,
                "status": "active", "balance": info["balance"],
                "balance_history": {period_key: info["balance"]},
                "claimed_month": None,
            }
        else:
            cc = cases[c]
            cc["last_seen"] = period_key
            cc["status"] = "active"
            cc["claimed_month"] = None
            cc["balance"] = info["balance"]
            cc["balance_history"][period_key] = info["balance"]
            if info["owner"] and not cc.get("owner"):
                cc["owner"] = info["owner"]

    for c in removed:
        cases[c]["status"] = "claimed"
        cases[c]["claimed_month"] = period_key

    store["months"].append({
        "period": period_key, "label": label,
        "lead_count": len(cur_set),
        "total_balance": round(sum(v["balance"] for v in cur.values()), 2),
        "added": len(added),
        "reappeared": len(reappeared),
        "removed_claimed": len(removed),
    })
    return added, removed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pending-dir", default="pdfs/excess_pending")
    ap.add_argument("--out", default="dashboard/excess_proceeds.json")
    ap.add_argument("--fresh", action="store_true",
                    help="ignore any existing --out and rebuild from scratch")
    ap.add_argument("--keep-pdfs", action="store_true",
                    help="do not delete PDFs after a successful ingest")
    args = ap.parse_args()

    if args.fresh or not os.path.exists(args.out):
        store = {"generated": "", "source": "Nueces County Clerk - Excess Proceeds report",
                 "months": [], "cases": {}}
    else:
        store = json.load(open(args.out))
        store.setdefault("months", [])
        store.setdefault("cases", {})

    already = {m["period"] for m in store["months"]}
    pdfs = sorted(glob.glob(os.path.join(args.pending_dir, "*.pdf")) +
                  glob.glob(os.path.join(args.pending_dir, "*.PDF")))
    if not pdfs:
        print(f"No PDFs in {args.pending_dir}")
        # still refresh derived fields / timestamp below

    parsed = []
    for p in pdfs:
        pk, label, recs = parse_report(p)
        if not pk:
            print(f"  SKIP {os.path.basename(p)}: no report date found")
            continue
        parsed.append((pk, label, recs, p))
    parsed.sort(key=lambda x: x[0])  # chronological by report month

    ingested_paths = []
    for pk, label, recs, p in parsed:
        if pk in already:
            print(f"  SKIP {label} ({os.path.basename(p)}): already in store")
            continue
        added, removed = ingest(store, pk, label, recs)
        already.add(pk)
        ingested_paths.append(p)
        print(f"  + {label}: {store['months'][-1]['lead_count']} leads "
              f"(added {len(added)}, claimed/removed {len(removed)})")

    # Derived summary
    cases = store["cases"]
    active = [c for c in cases.values() if c["status"] == "active"]
    claimed = [c for c in cases.values() if c["status"] == "claimed"]
    n_months = len(store["months"])
    all_months = sum(1 for c in active if len(c["balance_history"]) == n_months) if n_months else 0
    store["summary"] = {
        "months_tracked": n_months,
        "active_leads": len(active),
        "active_balance": round(sum(c["balance"] for c in active), 2),
        "claimed_total": len(claimed),
        "present_every_month": all_months,
    }
    store["generated"] = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    json.dump(store, open(args.out, "w"), indent=2)
    print(f"\nWrote {args.out}")
    print(f"  months tracked    : {store['summary']['months_tracked']}")
    print(f"  active leads      : {store['summary']['active_leads']}")
    print(f"  active balance    : ${store['summary']['active_balance']:,.2f}")
    print(f"  claimed/removed   : {store['summary']['claimed_total']}")
    print(f"  on every report   : {store['summary']['present_every_month']}")

    if not args.keep_pdfs and ingested_paths and not args.fresh:
        for p in ingested_paths:
            try:
                os.remove(p)
                print(f"  removed processed PDF {os.path.basename(p)}")
            except OSError:
                pass


if __name__ == "__main__":
    main()
