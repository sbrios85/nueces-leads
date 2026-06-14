"""
Tax Overages (Excess Proceeds) — Stage B: NCAD parcel match
===========================================================

Stage A (enrich_excess_deeds.py) found each active overage lead's NEW owner
(the tax-sale grantee) and the legal description off the sheriff's deed and
wrote them into dashboard/excess_proceeds.json as `new_owner` +
`legal_description`.

Stage B (this script) takes those leads and finds the parcel on NCAD, writing
back the fields the dashboard already reads so Property Address / Map / NCAD /
NCTAX light up automatically:

    prop_address, prop_city, prop_zip,
    ncad_prop_id, ncad_year, ncad_owner_id,
    ncad_account_num   (the dashed Geographic ID NCTAX needs),
    appraised_value

It REUSES Sergio's proven NCAD code verbatim — no new esearch logic:

  * Pass B1 — find the parcel by OWNER NAME, corroborated by LEGAL:
        fetch.enrich_via_ncad_search(leads, always_lookup=True)
    That handles the session token, throttle, query variants, best-row pick,
    and the legal-description corroboration guard (rejects loose surname hits).
    It sets ncad_prop_id / ncad_year / ncad_owner_id / appraised_value / address
    but NOT the account number.

  * Pass B2 — read the Geographic ID (account number) off each matched parcel's
    detail page, reusing enrich_fc_ncad_search's helpers:
        _mint_session_token -> _fetch_detail_html -> _extract_account_num

Heavy deps (fetch, enrich_fc_ncad_search, playwright) are imported lazily inside
the functions that use them, so this module can be compiled / unit-tested for
its data plumbing without playwright installed.

RUN LOCATION MATTERS: run from the scraper/ directory so `import fetch` and its
`from pdf_text_extractor import legal_descriptions_match` resolve. Without that
matcher, the corroboration guard disables and NO prop_id attaches to leads that
carry a legal (i.e. all of them) — the script warns loudly if that happens.

Env (the GitHub Actions workflow sets these; CLI flags override):
    APPLY=1         write excess_proceeds.json (default: dry-run)
    LIMIT=5         only process the first N eligible leads (0 = all)
    ONLY_MISSING=1  skip leads that already have BOTH prop_id and account_num
    MIN_BALANCE=0   only leads with balance >= this (0 = no filter)
    CASES=a,b,c     only these specific case numbers (comma-separated)
    FORCE=1         overwrite existing case fields (default: additive only)
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
from typing import Any, Dict, List, Optional

# REPO_ROOT is independent of CWD so the JSON path resolves no matter where the
# script is launched from. (Same convention as fetch.py / enrich_fc.)
REPO_ROOT = Path(__file__).resolve().parent.parent
EXCESS_JSON = REPO_ROOT / "dashboard" / "excess_proceeds.json"

# Owners we can't match to a private NCAD parcel — struck-off properties sit
# under the county trustee, and tax-sale title sometimes lands with a public
# agency (port authority, HUD). None of these resolve to a private NCAD owner,
# so skip them up front rather than burn an NCAD lookup that can only miss.
# NOTE: fetch.py's _looks_institutional catches HUD/COUNTY/CO but NOT the
# "NUECES CTY TRUSTEE" abbreviation seen in the real clerk data — hence the
# explicit CTY alternation here.
NON_PRIVATE_OWNER_RE = re.compile(
    r"NUECES\s+(?:COUNTY|CTY|CO\b)"                  # the county itself
    r"|\b(?:COUNTY|CTY)\s+TRUSTEE"                   # struck-off via county trustee
    r"|STRUCK\s*OFF"
    r"|PORT\s+OF\s+CORPUS\s+CHRISTI"                 # navigation district / port authority
    r"|\bCOUNTY\s+OF\b|\bCITY\s+OF\b|STATE\s+OF\s+TEXAS"
    r"|HOUSING\s+AND\s+URBAN\s+DEVELOPMENT|SECRETARY\s+OF\s+HOUSING|\bHUD\b",
    re.I,
)

# The 8 fields Stage B is responsible for writing back (documented JSON shape).
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
# Lead wrapper — duck-types what fetch.enrich_via_ncad_search reads/writes
# ====================================================================

class ExcessLead:
    """A single overage lead, shaped so enrich_via_ncad_search can treat it
    like a ClerkRecord. `.owner` is the NEW (tax-sale) owner and `.legal` is
    the deed's legal description — those two drive the name search + legal
    corroboration. The remaining attributes are the fields the search fills in.
    """

    __slots__ = (
        "case_number", "owner", "legal",
        "prop_address", "prop_city", "prop_state", "prop_zip",
        "mail_address", "mail_city", "mail_state", "mail_zip",
        "ncad_prop_id", "ncad_year", "ncad_owner_id",
        "appraised_value", "ncad_account_num",
    )

    def __init__(self, case_number: str, owner: str, legal: str,
                 existing: Optional[Dict[str, Any]] = None):
        e = existing or {}
        self.case_number = case_number
        self.owner = owner
        self.legal = legal
        # Seed pre-existing values so enrich_via_ncad_search's "only fill if
        # blank" guards preserve anything already in the JSON.
        self.prop_address = (e.get("prop_address") or "")
        self.prop_city = (e.get("prop_city") or "")
        self.prop_state = (e.get("prop_state") or "TX")
        self.prop_zip = (e.get("prop_zip") or "")
        self.mail_address = ""
        self.mail_city = ""
        self.mail_state = "TX"
        self.mail_zip = ""
        self.ncad_prop_id = (e.get("ncad_prop_id") or "")
        self.ncad_year = (e.get("ncad_year") or "")
        self.ncad_owner_id = (e.get("ncad_owner_id") or "")
        self.appraised_value = e.get("appraised_value")
        self.ncad_account_num = (e.get("ncad_account_num") or "")

    def values(self) -> Dict[str, Any]:
        """The Stage-B writeback fields and their current values."""
        return {f: getattr(self, f) for f in WRITEBACK_FIELDS}


# ====================================================================
# Selection
# ====================================================================

def select_leads(cases: Dict[str, Dict[str, Any]],
                 *, only_missing: bool, min_balance: float,
                 wanted: Optional[set], limit: int) -> List[ExcessLead]:
    out: List[ExcessLead] = []
    skipped_no_data = skipped_county = skipped_done = skipped_balance = 0

    for case_num, c in cases.items():
        if wanted is not None and case_num not in wanted:
            continue
        new_owner = (c.get("new_owner") or "").strip()
        legal = (c.get("legal_description") or "").strip()
        if not new_owner or not legal:
            skipped_no_data += 1
            continue
        if NON_PRIVATE_OWNER_RE.search(new_owner):
            skipped_county += 1
            continue
        if min_balance and float(c.get("balance") or 0) < min_balance:
            skipped_balance += 1
            continue
        if only_missing and c.get("ncad_prop_id") and c.get("ncad_account_num"):
            skipped_done += 1
            continue
        out.append(ExcessLead(case_num, new_owner, legal, existing=c))

    log.info(
        "select: %d eligible (skipped: %d no new_owner/legal, %d non-private/agency-owned, "
        "%d already-complete, %d below min-balance)",
        len(out), skipped_no_data, skipped_county, skipped_done, skipped_balance,
    )
    if limit and limit > 0:
        out = out[:limit]
        log.info("select: limited to first %d", len(out))
    return out


# ====================================================================
# Pass B1 — NCAD name search (+ legal corroboration)
# ====================================================================

def _check_legal_matcher() -> None:
    import fetch  # lazy
    if getattr(fetch, "_legal_match", None) is None:
        log.warning(
            "!! legal_descriptions_match UNAVAILABLE — pdf_text_extractor failed "
            "to import. The corroboration guard is DISABLED, so leads that carry "
            "a legal (all of them) will NOT attach a prop_id and Stage B will "
            "produce nothing. Run from the scraper/ directory so "
            "scraper/pdf_text_extractor.py is importable."
        )


def run_name_search(leads: List[ExcessLead]) -> int:
    import fetch  # lazy
    _check_legal_matcher()
    log.info("Pass B1: NCAD owner-name search for %d leads (legal-corroborated)",
             len(leads))
    matched = fetch.enrich_via_ncad_search(leads, always_lookup=True)
    got_pid = sum(1 for ld in leads if ld.ncad_prop_id)
    log.info("Pass B1 done: %d gained an address, %d carry a prop_id", matched, got_pid)
    return got_pid


# ====================================================================
# Pass B2 — Geographic ID (account number) off each detail page
# ====================================================================

async def _collect_account_numbers(leads: List[ExcessLead]) -> int:
    from playwright.async_api import async_playwright  # lazy
    import enrich_fc_ncad_search as efc  # lazy

    year_default = getattr(efc, "NCAD_YEAR", "2026")
    delay_ms = int(getattr(efc, "INTER_FETCH_DELAY_S", 1.5) * 1000)
    refresh_every = int(getattr(efc, "TOKEN_REFRESH_INTERVAL", 25))
    user_agent = getattr(efc, "USER_AGENT", None)

    targets = [ld for ld in leads if ld.ncad_prop_id and not ld.ncad_account_num]
    if not targets:
        log.info("Pass B2: no leads need an account number")
        return 0

    log.info("Pass B2: fetching Geographic ID for %d parcels", len(targets))
    filled = 0
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await (browser.new_context(user_agent=user_agent)
                     if user_agent else browser.new_context())
        page = await ctx.new_page()
        try:
            await efc._mint_session_token(page)  # warm the session
            since_refresh = 0
            for i, ld in enumerate(targets, 1):
                year = ld.ncad_year or year_default
                html = await efc._fetch_detail_html(
                    page, ld.ncad_prop_id, year, ld.ncad_owner_id)
                acct = efc._extract_account_num(html) if html else ""
                if acct:
                    ld.ncad_account_num = acct
                    filled += 1
                    log.info("  [%d/%d] %s  prop_id=%s -> geo %s",
                             i, len(targets), ld.case_number, ld.ncad_prop_id, acct)
                else:
                    log.warning("  [%d/%d] %s  prop_id=%s -> no geo id found",
                                i, len(targets), ld.case_number, ld.ncad_prop_id)
                since_refresh += 1
                if since_refresh >= refresh_every:
                    await efc._mint_session_token(page)
                    since_refresh = 0
                await page.wait_for_timeout(delay_ms)
        finally:
            await browser.close()
    log.info("Pass B2 done: %d account numbers", filled)
    return filled


# ====================================================================
# Writeback
# ====================================================================

def writeback(cases: Dict[str, Dict[str, Any]], leads: List[ExcessLead],
              *, force: bool) -> Dict[str, int]:
    fields_set = 0
    leads_touched = 0
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
        if touched:
            leads_touched += 1
    return {"fields": fields_set, "leads": leads_touched}


# ====================================================================
# Summary
# ====================================================================

def summarize(leads: List[ExcessLead]) -> None:
    full = [ld for ld in leads if ld.ncad_prop_id and ld.ncad_account_num]
    pid_only = [ld for ld in leads if ld.ncad_prop_id and not ld.ncad_account_num]
    misses = [ld for ld in leads if not ld.ncad_prop_id]
    log.info("=" * 60)
    log.info("SUMMARY: %d leads | %d fully matched (prop_id + geo) | "
             "%d prop_id only (no geo) | %d no match",
             len(leads), len(full), len(pid_only), len(misses))
    if pid_only:
        log.info("  prop_id-only (NCTAX link will fall back to owner search):")
        for ld in pid_only:
            log.info("    %s  %s", ld.case_number, ld.owner)
    if misses:
        log.info("  no NCAD match (check legal/owner, may be county-owned or typo):")
        for ld in misses:
            log.info("    %s  %s", ld.case_number, ld.owner)
    log.info("=" * 60)


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
                   default=_env_flag("APPLY", False),
                   help="write excess_proceeds.json (default: dry-run)")
    p.add_argument("--limit", type=int,
                   default=int(os.getenv("LIMIT", "0") or "0"),
                   help="process only the first N eligible leads (0 = all)")
    p.add_argument("--only-missing", dest="only_missing", action="store_true",
                   default=_env_flag("ONLY_MISSING", True),
                   help="skip leads that already have prop_id AND account_num")
    p.add_argument("--all", dest="only_missing", action="store_false",
                   help="re-process every eligible lead, even completed ones")
    p.add_argument("--min-balance", type=float,
                   default=float(os.getenv("MIN_BALANCE", "0") or "0"),
                   help="only leads with balance >= this (0 = no filter)")
    p.add_argument("--cases", default=os.getenv("CASES", ""),
                   help="comma-separated case numbers to restrict to")
    p.add_argument("--force", action="store_true",
                   default=_env_flag("FORCE", False),
                   help="overwrite existing case fields (default: additive only)")
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

    leads = select_leads(
        cases,
        only_missing=args.only_missing,
        min_balance=args.min_balance,
        wanted=wanted,
        limit=args.limit,
    )
    if not leads:
        log.info("No eligible leads. Nothing to do.")
        return 0

    run_name_search(leads)                       # Pass B1
    asyncio.run(_collect_account_numbers(leads)) # Pass B2

    counts = writeback(cases, leads, force=args.force)
    log.info("writeback: %d fields updated across %d leads",
             counts["fields"], counts["leads"])

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
