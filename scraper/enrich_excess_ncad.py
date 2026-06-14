"""
Tax Overages (Excess Proceeds) — Stage B: NCAD parcel match  (v2, lot/block)
============================================================================

Stage A wrote each active overage lead's NEW owner (the tax-sale grantee) plus
a legal description. The deed legals are thin/garbled for tax-sale deeds — they
carry the cause number and a lot/block but usually NOT the subdivision name
(e.g. "2014DCV-5081-H Lot 14 Block 1" for what NCAD calls "BLUNTZER PT LOS DOS
PALOMAS UNREC OUT SHR F, TR 14 BLK 1"). The lot/block themselves are correct.

So instead of relying on subdivision-name corroboration (which rejected almost
everything), Stage B:

  1. Searches NCAD by the NEW owner name and pulls EVERY parcel that buyer owns
     (the result list gives owner, situs, type, prop_id, owner_id, year, legal,
     appraised value).
  2. Parses lot + block from our lead's legal AND from each NCAD parcel's legal
     (treating Tract == Lot, expanding "122&123" / "18 & 19" / "15 THRU 17").
  3. If exactly ONE of the buyer's parcels matches our lot+block, attaches it:
     prop_address, ncad_prop_id/year/owner_id, appraised_value, and OVERWRITES
     legal_description with NCAD's clean legal (fixes the junk in the dashboard).
  4. Reads the dashed Geographic ID off that parcel's detail page for NCTAX.

A unique match is required, so a buyer's other parcels can't cause a mis-attach.
Cases where the grantee flipped the parcel (no longer in their NCAD holdings) or
whose legal has no lot/block correctly fall through to manual entry.

Reuses Sergio's proven NCAD code: fetch._esearch_query_variants,
fetch._parse_esearch_result_list, fetch._split_us_address, and
enrich_fc_ncad_search's _mint_session_token / _fetch_detail_html /
_extract_account_num. Heavy deps are imported lazily so this module compiles and
its matcher unit-tests without playwright installed.

Env (the workflow sets these; CLI flags override):
    APPLY=1         write excess_proceeds.json (default: dry-run)
    LIMIT=5         only the first N eligible leads (0 = all)
    ONLY_MISSING=1  skip leads that already have prop_id AND account_num
    MIN_BALANCE=0   only leads with balance >= this (0 = no filter)
    CASES=a,b,c     only these case numbers
    FORCE=1         overwrite existing case fields (default: additive,
                    except legal_description which is always upgraded on a match)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
EXCESS_JSON = REPO_ROOT / "dashboard" / "excess_proceeds.json"

NON_PRIVATE_OWNER_RE = re.compile(
    r"NUECES\s+(?:COUNTY|CTY|CO\b)"
    r"|\b(?:COUNTY|CTY)\s+TRUSTEE"
    r"|STRUCK\s*OFF"
    r"|PORT\s+OF\s+CORPUS\s+CHRISTI"
    r"|\bCOUNTY\s+OF\b|\bCITY\s+OF\b|STATE\s+OF\s+TEXAS"
    r"|HOUSING\s+AND\s+URBAN\s+DEVELOPMENT|SECRETARY\s+OF\s+HOUSING|\bHUD\b",
    re.I,
)

# Fields Stage B writes back. legal_description is handled specially (upgraded
# from NCAD's clean legal on a match), so it's not in this additive set.
WRITEBACK_FIELDS = (
    "prop_address", "prop_city", "prop_zip",
    "ncad_prop_id", "ncad_year", "ncad_owner_id",
    "ncad_account_num", "appraised_value",
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("enrich-excess-ncad")


# ====================================================================
# Lot / block parsing + matching  (validated against real NCAD legals)
# ====================================================================

_RANGE = re.compile(r"^\s*(\d+)\s*(?:thru|through|to|-)\s*(\d+)\s*$", re.I)
_BLK = re.compile(r"\b(?:BLOCK|BLK|BK)\s*[:#]?\s*(\d+[A-Za-z]?|[A-Za-z])\b", re.I)
_LOTVAL = r"\d+[A-Za-z]?(?:\s*(?:&|,|and|thru|through|to|-)\s*\d+[A-Za-z]?)*"
_LOT = re.compile(
    rf"\b(?:LOTS|LOT|LTS|LT|TRACTS|TRACT|TRS|TR)\s*[:#]?\s*({_LOTVAL})", re.I)


def _expand_lots(tok: str) -> Set[str]:
    out: Set[str] = set()
    for part in re.split(r"\s*(?:&|,|and)\s*", (tok or "").strip(), flags=re.I):
        if not part:
            continue
        m = _RANGE.match(part)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            for n in range(min(a, b), max(a, b) + 1):
                out.add(str(n))
        else:
            out.add(part.strip().upper())
    return out


def _lotblock(legal: str) -> Tuple[str, Set[str]]:
    """Return (block, {lots}) parsed from a legal description. Handles both our
    thin legals ('... Lot 14 Block 1') and NCAD's ('... TR 14 BLK 1',
    'BLK 1 LOT 122&123', 'LTS 18 & 19 BLK 7', 'LTS 15 THRU 17 BLK 15')."""
    legal = re.sub(r"\s+", " ", legal or "")
    b = _BLK.search(legal)
    block = b.group(1).upper() if b else ""
    l = _LOT.search(legal)
    lots = _expand_lots(l.group(1)) if l else set()
    return block, lots


def _lotblock_match(ours: Tuple[str, Set[str]],
                    parcel: Tuple[str, Set[str]]) -> bool:
    ob, ol = ours
    pb, pl = parcel
    if not ol or not pl:
        return False
    if ob and pb and ob != pb:    # blocks present on both and disagree
        return False
    return bool(ol & pl)          # lot sets overlap


# Strip a leading suit/cause token from a thin (unmatched) legal so the
# dashboard doesn't show cause-number junk. Keeps any real lot/block
# remainder; returns "" when nothing meaningful survives (a bare cause #).
_CAUSE = re.compile(
    r"^\s*(?:SUIT\s*(?:NO\.?|#)?\s*|CAUSE\s*(?:NO\.?|#)?\s*|#\s*)?"
    r"(?:\d{4}\s*DCV[\s-]*\d+[\s-]*[A-Z]"          # 2012DCV-4384-A / 2023DCV 3194 E
    r"|\d{2}-\d{4,5}-\d{2}-\d-[A-Z])"               # 93-01244-00-0-G
    r"\b[\s,.\-]*", re.I)


def _clean_thin_legal(legal: str) -> str:
    s = re.sub(r"\s+", " ", (legal or "").strip())
    s2 = _CAUSE.sub("", s).strip(" ,.-")
    if not re.search(r"\b(?:LOT|LOTS|LT|LTS|BLK|BLOCK|BK|TR|TRACT|SUBDIVISION)\b",
                     s2, re.I):
        return ""
    return s2


# ====================================================================
# Lead wrapper
# ====================================================================

class ExcessLead:
    __slots__ = (
        "case_number", "owner", "legal",
        "prop_address", "prop_city", "prop_state", "prop_zip",
        "ncad_prop_id", "ncad_year", "ncad_owner_id",
        "ncad_account_num", "appraised_value",
        "ncad_legal", "n_candidates", "n_hits",
    )

    def __init__(self, case_number: str, owner: str, legal: str,
                 existing: Optional[Dict[str, Any]] = None):
        e = existing or {}
        self.case_number = case_number
        self.owner = owner
        self.legal = legal
        self.prop_address = e.get("prop_address") or ""
        self.prop_city = e.get("prop_city") or ""
        self.prop_state = e.get("prop_state") or "TX"
        self.prop_zip = e.get("prop_zip") or ""
        self.ncad_prop_id = e.get("ncad_prop_id") or ""
        self.ncad_year = e.get("ncad_year") or ""
        self.ncad_owner_id = e.get("ncad_owner_id") or ""
        self.ncad_account_num = e.get("ncad_account_num") or ""
        self.appraised_value = e.get("appraised_value")
        self.ncad_legal = ""          # clean legal from the matched NCAD parcel
        self.n_candidates = 0          # how many parcels the buyer owns
        self.n_hits = 0                # how many matched our lot/block

    def values(self) -> Dict[str, Any]:
        return {f: getattr(self, f) for f in WRITEBACK_FIELDS}


# ====================================================================
# Selection
# ====================================================================

def select_leads(cases: Dict[str, Dict[str, Any]],
                 *, only_missing: bool, min_balance: float,
                 wanted: Optional[set], limit: int) -> List[ExcessLead]:
    out: List[ExcessLead] = []
    skipped_no_data = skipped_np = skipped_done = skipped_balance = 0
    for case_num, c in cases.items():
        if wanted is not None and case_num not in wanted:
            continue
        new_owner = (c.get("new_owner") or "").strip()
        legal = (c.get("legal_description") or "").strip()
        if not new_owner or not legal:
            skipped_no_data += 1
            continue
        if NON_PRIVATE_OWNER_RE.search(new_owner):
            skipped_np += 1
            continue
        if min_balance and float(c.get("balance") or 0) < min_balance:
            skipped_balance += 1
            continue
        if only_missing and c.get("ncad_prop_id") and c.get("ncad_account_num"):
            skipped_done += 1
            continue
        out.append(ExcessLead(case_num, new_owner, legal, existing=c))
    log.info("select: %d eligible (skipped: %d no new_owner/legal, "
             "%d non-private/agency-owned, %d already-complete, "
             "%d below min-balance)",
             len(out), skipped_no_data, skipped_np, skipped_done, skipped_balance)
    if limit and limit > 0:
        out = out[:limit]
        log.info("select: limited to first %d", len(out))
    return out


# ====================================================================
# NCAD search + match
# ====================================================================

async def _search_owner(page, fetch, owner: str, token: str, year: str) -> List[Dict[str, Any]]:
    """Run NCAD esearch for an owner name and return ALL parsed result rows.
    Tries fetch.py's proven name variants; returns the first that yields rows."""
    from urllib.parse import urlencode
    for cand in fetch._esearch_query_variants(owner):
        params = {"keywords": f"OwnerName:{cand} Year:{year} "}
        if token:
            params["searchSessionToken"] = token
        url = f"{fetch.NCAD_ESEARCH_BASE}/search/result?{urlencode(params)}"
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=20_000)
            try:
                await page.wait_for_selector(
                    "table tbody tr, [class*='no-results'], [class*='NoResults']",
                    timeout=8_000)
            except Exception:
                pass
            await page.wait_for_timeout(400)
            html = await page.content()
        except Exception as exc:
            log.debug("   esearch nav failed for %r: %s", cand, exc)
            continue
        rows = fetch._parse_esearch_result_list(html)
        if rows:
            return rows
    return []


async def _match_all(leads: List[ExcessLead]) -> int:
    from playwright.async_api import async_playwright
    import fetch
    import enrich_fc_ncad_search as efc

    year_default = getattr(efc, "NCAD_YEAR", "2026")
    delay_ms = int(getattr(efc, "INTER_FETCH_DELAY_S", 1.5) * 1000)
    refresh_every = int(getattr(efc, "TOKEN_REFRESH_INTERVAL", 25))
    ua = getattr(efc, "USER_AGENT", None)

    matched = 0
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = await (browser.new_context(user_agent=ua) if ua
                     else browser.new_context())
        page = await ctx.new_page()
        try:
            token = await efc._mint_session_token(page)
            since = 0
            for i, ld in enumerate(leads, 1):
                rows = await _search_owner(page, fetch, ld.owner, token, year_default)
                since += 1
                cand = [r for r in rows
                        if (r.get("type") or "").upper().startswith("R")]
                ld.n_candidates = len(cand)
                ours = _lotblock(ld.legal)
                hits = [r for r in cand if _lotblock_match(ours, _lotblock(r.get("legal", "")))]
                ld.n_hits = len(hits)

                if len(hits) == 1:
                    r = hits[0]
                    ld.ncad_prop_id = r.get("prop_id", "") or ""
                    ld.ncad_year = r.get("year", "") or year_default
                    ld.ncad_owner_id = r.get("owner_id", "") or ""
                    addr, city, state, zc = fetch._split_us_address(r.get("situs", "") or "")
                    ld.prop_address = addr
                    ld.prop_city = city or "CORPUS CHRISTI"
                    ld.prop_state = state or "TX"
                    ld.prop_zip = zc
                    ld.appraised_value = r.get("appraised_value")
                    ld.ncad_legal = (r.get("legal") or "").strip()
                    # Geographic ID (account number) off the detail page.
                    html = await efc._fetch_detail_html(
                        page, ld.ncad_prop_id, ld.ncad_year, ld.ncad_owner_id)
                    ld.ncad_account_num = efc._extract_account_num(html) if html else ""
                    matched += 1
                    log.info("  [%d/%d] %s  %s -> prop_id=%s geo=%s | %s",
                             i, len(leads), ld.case_number, ld.owner,
                             ld.ncad_prop_id, ld.ncad_account_num or "(none)",
                             ld.ncad_legal[:48])
                    await page.wait_for_timeout(delay_ms)
                else:
                    why = ("no parcels for owner" if not cand
                           else f"{len(cand)} parcels, {len(hits)} lot/block hits")
                    log.info("  [%d/%d] %s  %s -> no unique match (%s)",
                             i, len(leads), ld.case_number, ld.owner, why)

                if since >= refresh_every:
                    token = await efc._mint_session_token(page)
                    since = 0
                await page.wait_for_timeout(delay_ms)
        finally:
            await browser.close()
    return matched


# ====================================================================
# Writeback
# ====================================================================

def writeback(cases: Dict[str, Dict[str, Any]], leads: List[ExcessLead],
              *, force: bool) -> Dict[str, int]:
    fields_set = 0
    leads_touched = 0
    legals_upgraded = 0
    legals_cleaned = 0
    for ld in leads:
        c = cases.get(ld.case_number)
        if c is None:
            continue
        touched = False
        for field, val in ld.values().items():
            if val in (None, ""):
                continue
            existing = c.get(field)
            if existing in (None, "") or force:
                if existing != val:
                    c[field] = val
                    fields_set += 1
                    touched = True
        # Upgrade legal_description to NCAD's clean legal whenever we matched.
        if ld.ncad_legal and ld.ncad_prop_id and c.get("legal_description") != ld.ncad_legal:
            c["legal_description"] = ld.ncad_legal
            legals_upgraded += 1
            touched = True
        # No match: strip the cause-number junk off the thin deed legal.
        elif not ld.ncad_prop_id:
            cur = c.get("legal_description") or ""
            cleaned = _clean_thin_legal(cur)
            if cleaned != cur:
                c["legal_description"] = cleaned
                legals_cleaned += 1
                touched = True
        if touched:
            leads_touched += 1
    return {"fields": fields_set, "leads": leads_touched,
            "legals": legals_upgraded, "cleaned": legals_cleaned}


# ====================================================================
# Summary
# ====================================================================

def summarize(leads: List[ExcessLead]) -> None:
    full = [ld for ld in leads if ld.ncad_prop_id and ld.ncad_account_num]
    pid = [ld for ld in leads if ld.ncad_prop_id and not ld.ncad_account_num]
    no_owner = [ld for ld in leads if ld.n_candidates == 0]
    ambiguous = [ld for ld in leads if ld.n_candidates and not ld.ncad_prop_id]
    log.info("=" * 64)
    log.info("SUMMARY: %d leads | %d fully matched | %d prop_id only (no geo) | "
             "%d owner not on NCAD | %d no unique lot/block",
             len(leads), len(full), len(pid), len(no_owner), len(ambiguous))
    if ambiguous:
        log.info("  buyer found but no unique lot/block (flipped or thin legal):")
        for ld in ambiguous:
            log.info("    %s  %s  (%d parcels, %d hits)",
                     ld.case_number, ld.owner, ld.n_candidates, ld.n_hits)
    if no_owner:
        log.info("  owner not found on NCAD (resold, or name format):")
        for ld in no_owner:
            log.info("    %s  %s", ld.case_number, ld.owner)
    log.info("=" * 64)


# ====================================================================
# Entry point
# ====================================================================

def _env_flag(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "y")


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Tax Overages Stage B — NCAD match")
    p.add_argument("--apply", action="store_true",
                   default=_env_flag("APPLY", False))
    p.add_argument("--limit", type=int,
                   default=int(os.getenv("LIMIT", "0") or "0"))
    p.add_argument("--only-missing", dest="only_missing", action="store_true",
                   default=_env_flag("ONLY_MISSING", True))
    p.add_argument("--all", dest="only_missing", action="store_false")
    p.add_argument("--min-balance", type=float,
                   default=float(os.getenv("MIN_BALANCE", "0") or "0"))
    p.add_argument("--cases", default=os.getenv("CASES", ""))
    p.add_argument("--force", action="store_true",
                   default=_env_flag("FORCE", False))
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    if not EXCESS_JSON.exists():
        log.error("excess_proceeds.json not found at %s", EXCESS_JSON)
        return 1

    data = json.loads(EXCESS_JSON.read_text(encoding="utf-8"))
    cases = data.get("cases", {})
    log.info("loaded %d cases from %s", len(cases), EXCESS_JSON)

    wanted = None
    if args.cases:
        wanted = {c.strip() for c in args.cases.split(",") if c.strip()}

    leads = select_leads(cases, only_missing=args.only_missing,
                         min_balance=args.min_balance, wanted=wanted,
                         limit=args.limit)
    if not leads:
        log.info("No eligible leads. Nothing to do.")
        return 0

    asyncio.run(_match_all(leads))

    counts = writeback(cases, leads, force=args.force)
    log.info("writeback: %d fields across %d leads (%d legals upgraded, "
             "%d junk legals cleaned)",
             counts["fields"], counts["leads"], counts["legals"],
             counts["cleaned"])

    summarize(leads)

    if args.apply:
        EXCESS_JSON.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8")
        log.info("APPLIED -> %s", EXCESS_JSON)
    else:
        log.info("DRY RUN — nothing written. Re-run with --apply (or APPLY=1).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
