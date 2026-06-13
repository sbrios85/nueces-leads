"""
Excess-Proceeds Deed Lookup  (Stage A)
======================================
For each active Tax Overages (excess-proceeds) lead in
``dashboard/excess_proceeds.json``, find the SHERIFF'S DEED that was recorded
*after* the tax suit on the Nueces County Clerk portal
(https://nueces.tx.publicsearch.us/) and pull:

  * new_owner          -> the GRANTEE (the tax-sale buyer who owns it now)
  * legal_description  -> subdivision / lot / block (cites the suit number)
  * deed_doc_num, deed_recorded, deed_clerk_url

Those fields are written back into each case in excess_proceeds.json so the
dashboard auto-fills the "New Owner" and "Legal Description" columns. The
property address + NCAD account are found LATER (Stage B) by searching NCAD
with the new owner name + legal description.

Reuses the proven clerk-portal access pattern from ``fetch.py`` (same Neumo
results-table parser and search-URL builder), so it talks to the portal the
exact same way the daily scraper already does.

Runs in GitHub Actions (like fetch.py) or locally with Playwright + Chromium:
    python scraper/enrich_excess_deeds.py                 # active leads >= $5k
    python scraper/enrich_excess_deeds.py --min-balance 0 # every active lead
    python scraper/enrich_excess_deeds.py --limit 10      # just the first 10
    python scraper/enrich_excess_deeds.py --only-missing  # skip ones already done
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
from datetime import date
from pathlib import Path

# Reuse the clerk-portal helpers from the daily scraper. The script lives in
# scraper/ next to fetch.py, so add this dir to the path and import it.
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fetch  # noqa: E402  (clerk URL builder, Neumo table parser, constants)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("excess_deeds")

ROOT = HERE.parent
EXCESS_JSON = ROOT / "dashboard" / "excess_proceeds.json"

DELAY_SEC = 1.5          # polite pause between clerk searches
NAV_TIMEOUT_MS = 25_000

_DEED_RE = re.compile(r"\bDEED\b", re.I)
_SHERIFF_RE = re.compile(r"sheriff|nueces\s+county\s+sh", re.I)


def _last_first(owner: str) -> str:
    """The report stores owners 'First [Middle] Last' (e.g. 'Linda Sue
    Cochran'); the clerk indexes Last-first. Convert -> 'Cochran Linda Sue'.
    Leaves names that are already 'Last, First' or company names alone."""
    o = re.sub(r"\s+", " ", (owner or "").strip())
    if not o:
        return ""
    if "," in o:                       # already Last, First
        return o.replace(",", " ").strip()
    parts = o.split(" ")
    if len(parts) < 2:
        return o
    return parts[-1] + " " + " ".join(parts[:-1])


def _case_year(case_number: str) -> int:
    """Filing year from the case number. '2019DCV-4860-A' -> 2019;
    legacy '00-00356-00-0-C' -> 2000."""
    m = re.match(r"(\d{4})", case_number or "")
    if m:
        return int(m.group(1))
    m = re.match(r"(\d{2})-", case_number or "")
    if m:
        yy = int(m.group(1))
        return 2000 + yy if yy < 50 else 1900 + yy
    return 1990


def _norm_case(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _pick_deed(rows, case_number: str, case_year: int):
    """From the clerk rows, choose the sheriff's deed for this suit.

    Scoring (higher = better): grantor looks like the sheriff (+3), the
    legal text cites this case number (+4), it's a deed recorded in/after
    the suit year (+1). Ties break toward the most recent recording.
    """
    cnorm = _norm_case(case_number)
    scored = []
    for r in rows:
        if not _DEED_RE.search(r.get("doc_type") or ""):
            continue
        rec_date = r.get("recorded_date") or ""
        ryear = 0
        m = re.search(r"(\d{4})", rec_date)
        if m:
            ryear = int(m.group(1))
        if ryear and ryear < case_year:        # deed predates the suit -> skip
            continue
        score = 1
        if _SHERIFF_RE.search(r.get("grantor") or ""):
            score += 3
        if cnorm and cnorm in _norm_case(r.get("legal") or ""):
            score += 4
        scored.append((score, ryear, r))
    if not scored:
        return None
    scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return scored[0][2]


def _compose_legal(deed: dict) -> str:
    """Build a single legal-description string from the row's legal + lot +
    block cells (the Neumo table splits them out)."""
    legal = (deed.get("legal") or "").strip()
    lot = (deed.get("lot") or "").strip()
    block = (deed.get("block") or "").strip()
    low = legal.lower()
    if lot and "lot" not in low:
        legal = f"{legal} Lot {lot}".strip()
        low = legal.lower()
    if block and "block" not in low and "blk" not in low:
        legal = f"{legal} Block {block}".strip()
    return legal


async def _search(page, url: str):
    """Navigate to a clerk results URL, wait for the table to render, return
    parsed rows. Mirrors fetch.py's _do_search nav/wait, minus the extras."""
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        try:
            await page.wait_for_function(
                """() => {
                    const rows = document.querySelectorAll('table tbody tr');
                    for (const r of rows) {
                        const c = r.querySelector('.col-7');
                        if (c && c.textContent.trim()) return true;
                    }
                    const t = document.body.innerText || '';
                    return t.includes('No Results Found') ||
                           t.includes('returned no results');
                }""",
                timeout=15_000,
            )
        except Exception:
            pass
        await page.wait_for_timeout(400)
        html = await page.content()
    except Exception as exc:
        log.error("   nav failed %s: %s", url, exc)
        return []
    return fetch._extract_clerk_table_rows(html)


async def _run(leads):
    from playwright.async_api import async_playwright

    today = date.today().isoformat()
    out = {}
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = await browser.new_context(user_agent=fetch.USER_AGENT)
        page = await ctx.new_page()
        for i, lead in enumerate(leads, 1):
            case_number = lead["case_number"]
            owner = lead.get("owner") or ""
            cy = _case_year(case_number)
            start = f"{cy}-01-01"
            log.info("[%d/%d] %s  (%s)", i, len(leads), case_number, owner)

            # 1) precise: search by the case number
            url = fetch._build_clerk_search_url(
                start, today, query=case_number, department="RP")
            deed = _pick_deed(await _search(page, url), case_number, cy)
            await page.wait_for_timeout(int(DELAY_SEC * 1000))

            # 2) fallback: search by the former owner (Last First)
            if not deed and owner:
                url = fetch._build_clerk_search_url(
                    start, today, query=_last_first(owner), department="RP")
                deed = _pick_deed(await _search(page, url), case_number, cy)
                await page.wait_for_timeout(int(DELAY_SEC * 1000))

            if not deed:
                log.info("   no sheriff's deed found")
                continue

            href = (deed.get("clerk_url") or "").strip()
            if href.startswith("/"):
                href = fetch.CLERK_BASE + href
            out[case_number] = {
                "new_owner": (deed.get("grantee") or "").strip(),
                "legal_description": _compose_legal(deed),
                "deed_doc_num": (deed.get("doc_number") or "").strip(),
                "deed_recorded": (deed.get("recorded_date") or "").strip(),
                "deed_clerk_url": href,
            }
            log.info("   -> new owner: %s | legal: %s",
                     out[case_number]["new_owner"],
                     out[case_number]["legal_description"][:60])
        await browser.close()
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-balance", type=float, default=5000.0,
                    help="only process active leads at/above this balance")
    ap.add_argument("--limit", type=int, default=0,
                    help="max leads to process (0 = no limit)")
    ap.add_argument("--only-missing", action="store_true",
                    help="skip leads that already have a new_owner")
    args = ap.parse_args()

    data = json.loads(EXCESS_JSON.read_text(encoding="utf-8"))
    cases = data.get("cases", {})

    leads = []
    for cnum, c in cases.items():
        if c.get("status") != "active":
            continue
        if float(c.get("balance") or 0) < args.min_balance:
            continue
        if args.only_missing and c.get("new_owner"):
            continue
        c2 = dict(c)
        c2["case_number"] = cnum
        leads.append(c2)
    leads.sort(key=lambda c: float(c.get("balance") or 0), reverse=True)
    if args.limit:
        leads = leads[:args.limit]
    log.info("processing %d leads (min_balance=%.0f)", len(leads), args.min_balance)
    if not leads:
        log.info("nothing to do")
        return 0

    out = asyncio.run(_run(leads))

    found = 0
    for cnum, fields in out.items():
        if cnum in cases and fields.get("new_owner"):
            cases[cnum].update(fields)
            found += 1
    data["cases"] = cases
    EXCESS_JSON.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    log.info("=== done: deeds found for %d / %d leads; wrote %s ===",
             found, len(leads), EXCESS_JSON)
    return 0


if __name__ == "__main__":
    sys.exit(main())
