"""
Manual-upload foreclosure PDF processor.
========================================

Scans `pdfs/foreclosures/` for PDF files, extracts text + structured
fields from each, matches them to records in `dashboard/foreclosures.json`
by doc number (parsed FROM the PDF content, since uploaded filenames
vary), and writes the enriched records back.

After processing, PROCESSED PDFs are deleted from the folder. Unprocessed
PDFs (e.g. couldn't extract doc number, or no matching record) are left
in place with a log warning so you can investigate.

Designed to be triggered manually via the
`parse_uploaded_pdfs.yml` workflow after you upload PDFs to the folder
through GitHub's web UI.

Run via:    python scraper/extract_uploaded_pdfs.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# Allow `import fetch` and `import pdf_text_extractor` from the scraper dir.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pdf_text_extractor import (   # noqa: E402
    extract_text,
    parse_foreclosure_pdf_text,
    legal_descriptions_match,
)

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("nueces-pdf-uploader")


# --------------------------------------------------------------------------- #
# Repo paths
# --------------------------------------------------------------------------- #

ROOT_DIR = Path(__file__).resolve().parent.parent
PDFS_DIR = ROOT_DIR / "pdfs" / "foreclosures"
# Every successfully-parsed PDF's EXTRACTED TEXT is saved here (not
# the PDF itself — text is ~50-100x smaller, so the repo stays tiny
# for decades). This lets us re-run parser improvements over the full
# history WITHOUT re-downloading notices from the clerk portal. The
# normal daily flow never deletes from this archive. Re-OCR'ing a
# scanned PDF is the only thing this can't do — rare, handled by
# re-uploading those specific PDFs.
TEXT_ARCHIVE_DIR = ROOT_DIR / "pdfs" / "text_archive"
DASHBOARD_FILE = ROOT_DIR / "dashboard" / "foreclosures.json"
DATA_FILE      = ROOT_DIR / "data" / "foreclosures.json"


def _load_foreclosures() -> dict:
    for path in (DASHBOARD_FILE, DATA_FILE):
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                log.warning("could not parse %s: %s", path, exc)
    return {}


def _save_foreclosures(payload: dict) -> None:
    payload["fetched_at"] = datetime.now(timezone.utc).isoformat()
    for path in (DASHBOARD_FILE, DATA_FILE):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, default=str),
                         encoding="utf-8")
        log.info("wrote %s (%d records)", path,
                  len(payload.get("records", [])))


def _normalize_name_for_ncad(raw: str) -> str:
    """Extract just the primary borrower name for an NCAD lookup.

    The PDF parser captures the full borrower clause which often
    includes a spouse / co-borrower:
       "JOHN BRUNO AMARO AND WIFE, SANDRA ANN AMARO"
       "Brandon C Cordell and Arlene Madali Cordell husband and wife"
       "MIGUEL PENA III AND CLAIRE O. GUERRA"
    NCAD's esearch indexes by individual person, so we want just the
    first name. We split on common connectors (AND/and/&) and take
    the left half.

    Returns "" if the result is too short to be useful.
    """
    if not raw:
        return ""
    # Drop trailing descriptors like ", AN UNMARRIED WOMAN" — anything
    # after a comma followed by a generic descriptor.
    s = re.sub(r"\s*,\s*(?:AN?\s|UNMARRIED|SINGLE|MARRIED|A\s+SINGLE|"
                r"HUSBAND|WIFE|HIS\s+SPOUSE|HER\s+SPOUSE).*$",
                "", raw, flags=re.IGNORECASE)
    # Split on the first "AND"/"and"/"&" between full names. Use
    # word boundaries to avoid splitting inside a name like "ANDERSON".
    parts = re.split(r"\s+(?:AND|and|&)\s+", s, maxsplit=1)
    primary = parts[0].strip(" ,.").strip()
    # Remove trailing single-letter words (titles, initials that lost
    # their period during OCR — e.g. "JOHN A" → "JOHN").
    primary = re.sub(r"\s+[A-Z]\.?$", "", primary).strip()
    # Sanity check: needs at least 4 chars
    if len(primary) < 4:
        return ""
    return primary


def _extract_secondary_name(raw: str) -> str:
    """Extract the spouse / co-borrower name from a joint borrower clause.

    Examples:
       "JOHN BRUNO AMARO AND WIFE, SANDRA ANN AMARO"
           → "SANDRA ANN AMARO"
       "Brandon C Cordell and Arlene Madali Cordell husband and wife"
           → "Arlene Madali Cordell"
       "MIGUEL PENA III AND CLAIRE O. GUERRA"
           → "CLAIRE O. GUERRA"
       "ELAINE SALAZAR" (no spouse)
           → ""

    The secondary name is everything after the FIRST connector
    ("AND"/"and"/"&"), with stop words removed.
    """
    if not raw:
        return ""
    # Split on the first connector to get the right-hand side.
    parts = re.split(r"\s+(?:AND|and|&)\s+", raw, maxsplit=1)
    if len(parts) < 2:
        return ""
    secondary = parts[1].strip()
    # Strip leading "WIFE,"/"HUSBAND," and similar role labels.
    secondary = re.sub(r"^(?:WIFE|HUSBAND|SPOUSE)\s*,?\s*",
                        "", secondary, flags=re.IGNORECASE)
    # Strip trailing "husband and wife"/"his wife"/"her husband" etc.
    secondary = re.sub(r"\s+(?:husband\s+and\s+wife|wife\s+and\s+husband|"
                        r"his\s+(?:wife|spouse)|her\s+(?:husband|spouse))"
                        r"\s*$", "", secondary, flags=re.IGNORECASE)
    secondary = secondary.strip(" ,.").strip()
    # Strip trailing single-letter words
    secondary = re.sub(r"\s+[A-Z]\.?$", "", secondary).strip()
    # Need at least 4 chars to be useful
    if len(secondary) < 4:
        return ""
    return secondary


def _looks_like_garbage(text: str) -> bool:
    """Heuristic: is this string clearly junk from a page header/footer
    that the regex grabbed by mistake?"""
    if not text:
        return True
    upper = text.upper()
    # Tokens that strongly indicate header/footer garbage, not real data
    junk_tokens = ("PAGE ", " OF ", "KARA SANDS", "CLERK OF",
                    "COUNTY COURT", "COUNTY OF", "RECORDED", "RECEIVED",
                    "UNOFFICIAL", "AM ", "PM ")
    hits = sum(1 for tok in junk_tokens if tok in upper)
    return hits >= 2


def _apply_fields(rec: Dict[str, Any], fields: Dict[str, Any],
                   overwrite: bool = False) -> bool:
    """Copy parsed PDF fields onto a foreclosure record. Returns True if
    any field was newly populated (i.e. the record changed).

    Applies sanity filters to reject obvious page-header garbage from OCR.

    overwrite: if True, also overwrites EXISTING values on the record
        (useful for re-processing a PDF after a parser fix). Default
        False — only fills empty fields. In overwrite mode, when the
        new parse returns an empty value for a field, the existing
        value is also CLEARED — this is essential for fixing records
        whose old wrong values (e.g. a law firm address that was
        mis-extracted) need to be removed by a newer parser version.
    """
    if not fields:
        return False
    changed = False
    # --- Borrower (owner) ---
    borrower = fields.get("borrower", "")
    if borrower and not _looks_like_garbage(borrower):
        if not rec.get("owner") or overwrite:
            if rec.get("owner") != borrower:
                rec["owner"] = borrower
                changed = True
    # --- Loan amount ---
    if fields.get("loan_amount"):
        if not rec.get("loan_amount") or overwrite:
            if rec.get("loan_amount") != fields["loan_amount"]:
                rec["loan_amount"] = fields["loan_amount"]
                changed = True
    # --- Lender ---
    lender = fields.get("lender", "")
    if lender and not _looks_like_garbage(lender):
        if not rec.get("lender") or overwrite:
            if rec.get("lender") != lender:
                rec["lender"] = lender
                changed = True
    # --- Deed date ---
    if fields.get("deed_date"):
        if not rec.get("deed_date") or overwrite:
            if rec.get("deed_date") != fields["deed_date"]:
                rec["deed_date"] = fields["deed_date"]
                changed = True
    # --- Original deed date (only present when a loan mod replaced
    # the displayed deed_date). Mirror the parser's view: set it when
    # present, clear it when the latest parse no longer produces one
    # (e.g. the mod language went away on a re-parse).
    new_orig = fields.get("deed_date_original") or ""
    if rec.get("deed_date_original", "") != new_orig:
        if new_orig:
            rec["deed_date_original"] = new_orig
        elif "deed_date_original" in rec:
            del rec["deed_date_original"]
        changed = True
    # --- Loan document (deed-of-trust recording / instrument number) ---
    if fields.get("loan_doc"):
        if not rec.get("loan_doc") or overwrite:
            if rec.get("loan_doc") != fields["loan_doc"]:
                rec["loan_doc"] = fields["loan_doc"]
                changed = True
    # --- Loan modification instrument number ---
    # Mirror the parser: set when present, clear when a re-parse no
    # longer finds one (e.g. mod language changed). Only meaningful
    # on loan-mod records; absent otherwise.
    new_mod_doc = fields.get("loan_mod_doc") or ""
    if rec.get("loan_mod_doc", "") != new_mod_doc:
        if new_mod_doc:
            rec["loan_mod_doc"] = new_mod_doc
        elif "loan_mod_doc" in rec:
            del rec["loan_mod_doc"]
        changed = True
    # --- Loan modification flag ---
    # Always reflect the parser's current view (it's a derived boolean,
    # not user data). If the latest parse no longer sees a mod, clear
    # it; if it now sees one, set it.
    new_mod = bool(fields.get("has_loan_mod"))
    if rec.get("has_loan_mod") != new_mod:
        rec["has_loan_mod"] = new_mod
        changed = True
    # --- Property address ---
    addr = fields.get("prop_address", "")
    if addr and not _looks_like_garbage(addr):
        # New parse has a valid address — overwrite or fill empty.
        if not rec.get("prop_address") or overwrite:
            if rec.get("prop_address") != addr:
                rec["prop_address"] = addr
                rec["prop_city"] = fields.get("prop_city", "")
                rec["prop_state"] = fields.get("prop_state", "TX")
                rec["prop_zip"] = fields.get("prop_zip", "")
                changed = True
    elif overwrite and rec.get("prop_address"):
        # New parse legitimately returned no address (e.g. Plutus
        # records where the only address in the PDF is the San Antonio
        # law firm). In overwrite mode, clear the stale wrong value.
        rec["prop_address"] = ""
        rec["prop_city"] = ""
        rec["prop_state"] = "TX"
        rec["prop_zip"] = ""
        changed = True
    # --- Legal description fields ---
    for fld in ("legal_lot", "legal_block", "legal_subdivision"):
        val = fields.get(fld, "")
        if val and not _looks_like_garbage(val):
            if not rec.get(fld) or overwrite:
                if rec.get(fld) != val:
                    rec[fld] = val
                    changed = True
    rec["pdf_parsed_at"] = datetime.now(timezone.utc).isoformat()
    return changed


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main() -> int:
    log.info("=== Manual-upload PDF processor ===")

    # SOURCE selection.
    #   inbox        (default) — parse PDFs in pdfs/foreclosures/, the
    #                 normal manual-upload flow.
    #   text_archive — re-parse the saved extracted TEXT in
    #                 pdfs/text_archive/. Used after a parser
    #                 improvement to refresh ALL historical records
    #                 without re-downloading or re-OCR'ing anything.
    source = os.environ.get("PDF_SOURCE", "inbox").strip().lower()
    text_archive_mode = source in ("text_archive", "archive")

    # OVERWRITE mode: re-process even records that already have fields.
    # Useful after fixing a regex bug. Text-archive mode ALWAYS
    # overwrites (the entire point of re-parsing is to replace stale
    # data produced by the old parser).
    overwrite = os.environ.get("PDF_OVERWRITE", "").lower() in (
        "1", "true", "yes", "y")
    if text_archive_mode:
        overwrite = True
        log.info("TEXT-ARCHIVE mode: re-parsing saved text in "
                 "pdfs/text_archive/ (overwrite forced ON)")
    elif overwrite:
        log.info("OVERWRITE mode is ON — existing fields will be replaced")

    if text_archive_mode:
        src_dir = TEXT_ARCHIVE_DIR
        suffix = ".txt"
    else:
        src_dir = PDFS_DIR
        suffix = ".pdf"

    if not src_dir.exists():
        log.info("no %s folder yet — nothing to do",
                 src_dir.relative_to(ROOT_DIR))
        return 0

    pdfs = sorted([p for p in src_dir.iterdir()
                   if p.is_file() and p.suffix.lower() == suffix])
    if not pdfs:
        log.info("no %s files in %s — nothing to do",
                 suffix, src_dir.relative_to(ROOT_DIR))
        return 0

    log.info("found %d %s file(s) to process",
             len(pdfs), suffix)

    payload = _load_foreclosures()
    records = payload.get("records", [])
    if not records:
        log.warning("foreclosures.json has no records — PDFs cannot be "
                    "matched. Run the daily scraper first.")
        return 0

    # Index records by doc_num for fast lookup
    by_doc = {r.get("doc_num"): r for r in records if r.get("doc_num")}
    log.info("loaded %d existing foreclosure records", len(records))

    # Stats before
    have_owner_before = sum(1 for r in records if r.get("owner"))
    have_addr_before  = sum(1 for r in records if r.get("prop_address"))

    processed_count = 0
    enriched_count  = 0
    skipped_count   = 0

    for pdf_path in pdfs:
        log.info("processing %s ...", pdf_path.name)
        try:
            if text_archive_mode:
                # Source IS already-extracted text — read it straight,
                # no PDF/OCR step. This is what makes re-parsing the
                # full history fast and dependency-free.
                try:
                    text = pdf_path.read_text(encoding="utf-8")
                except Exception as exc:
                    log.warning("  could not read %s: %s — skipping",
                                pdf_path.name, exc)
                    skipped_count += 1
                    continue
            else:
                text = extract_text(pdf_path)
            if not text:
                log.warning("  no text extracted (scanned PDF?) — leaving "
                            "%s in place", pdf_path.name)
                skipped_count += 1
                continue

            # Save the OCR'd text to a debug folder so we can tune regex
            # patterns against real output. The folder is gitignored so
            # this stays local to the workflow run (visible in artifacts).
            try:
                debug_dir = ROOT_DIR / "debug"
                debug_dir.mkdir(parents=True, exist_ok=True)
                debug_path = debug_dir / f"{pdf_path.stem}.txt"
                debug_path.write_text(text, encoding="utf-8")
                log.info("  saved extracted text to debug/%s "
                         "(%d chars) for regex tuning",
                         debug_path.name, len(text))
            except Exception:
                pass

            fields = parse_foreclosure_pdf_text(text)
            dn = fields.get("doc_number")
            if not dn:
                log.warning("  could not parse doc number from %s — "
                            "leaving in place for manual review",
                            pdf_path.name)
                skipped_count += 1
                continue

            rec = by_doc.get(dn)
            if not rec:
                if text_archive_mode:
                    # Expected: the foreclosure list is a rolling
                    # window, so old archived notices naturally have no
                    # matching live record anymore. Not a problem —
                    # just nothing to update for this one.
                    log.info("  doc %s not in current window — "
                             "skipping (archived notice aged out)", dn)
                else:
                    log.warning("  doc %s parsed from %s but no matching "
                                "record in foreclosures.json — leaving "
                                "PDF in place (maybe daily scraper "
                                "hasn't run yet?)", dn, pdf_path.name)
                skipped_count += 1
                continue

            changed = _apply_fields(rec, fields, overwrite=overwrite)
            if changed:
                enriched_count += 1
                log.info("  → enriched record %s: owner=%r, addr=%r, "
                         "loan=%r",
                         dn, rec.get("owner"), rec.get("prop_address"),
                         rec.get("loan_amount"))
            else:
                log.info("  → record %s already had all fields, no change",
                         dn)

            # In text-archive mode the source IS the archive — don't
            # re-save or delete it; leave it for the next re-parse.
            if not text_archive_mode:
                # Save the extracted TEXT to the archive (keyed by doc
                # number — stable, dedupes re-uploads of the same
                # notice). Text is ~50-100x smaller than the PDF, so
                # this keeps the repo tiny while still letting future
                # parser improvements re-run over the full history.
                try:
                    TEXT_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
                    arch_name = (f"{dn}.txt" if dn
                                 else f"{pdf_path.stem}.txt")
                    (TEXT_ARCHIVE_DIR / arch_name).write_text(
                        text, encoding="utf-8")
                    log.debug("  archived text -> pdfs/text_archive/%s "
                              "(%d chars)", arch_name, len(text))
                except Exception as exc:
                    log.warning("  could not archive text for %s: %s",
                                pdf_path.name, exc)

                # Delete the processed PDF from the inbox folder
                try:
                    pdf_path.unlink()
                    log.debug("  deleted %s", pdf_path.name)
                except Exception as exc:
                    log.warning("  could not delete %s: %s",
                                pdf_path.name, exc)
            processed_count += 1

        except Exception as exc:
            log.error("  failed to process %s: %s\n%s",
                      pdf_path.name, exc, traceback.format_exc())
            skipped_count += 1

    # Cross-reference: for records that NOW have a borrower + legal
    # description (but no street address from the PDF), run NCAD lookup
    # to fill in the address.
    xref_count = 0
    try:
        xref_count = _cross_reference_addresses(records)
    except Exception as exc:
        log.error("cross-reference phase failed: %s\n%s",
                  exc, traceback.format_exc())

    # Stats after
    have_owner_after = sum(1 for r in records if r.get("owner"))
    have_addr_after  = sum(1 for r in records if r.get("prop_address"))

    log.info("=== run done ===")
    log.info("PDFs: %d processed, %d skipped", processed_count, skipped_count)
    log.info("records enriched: +%d owners (PDF), +%d addresses (xref)",
             enriched_count, xref_count)
    log.info("totals: %d/%d have owner (was %d), %d/%d have addr (was %d)",
             have_owner_after, len(records), have_owner_before,
             have_addr_after, len(records), have_addr_before)

    # Save updated records
    payload["records"] = records
    _save_foreclosures(payload)

    return 0


# --------------------------------------------------------------------------- #
# NCAD cross-reference for legal-description-only records
# --------------------------------------------------------------------------- #

def _cross_reference_addresses(records: List[Dict[str, Any]]) -> int:
    """For records that have borrower + legal but no street address,
    query NCAD by borrower name and match the legal description to
    find the property's street address.

    Returns the number of records newly enriched with an address.
    """
    eligible = []
    for r in records:
        if r.get("prop_address"):
            continue
        if not r.get("owner"):
            continue
        if not (r.get("legal_subdivision") or r.get("legal_lot")):
            continue
        eligible.append(r)

    if not eligible:
        log.info("cross-ref: no records eligible for legal-match enrichment")
        return 0

    log.info("cross-ref: %d records eligible — running NCAD search...",
             len(eligible))

    # Import lazily — these are heavy and only needed when there's work.
    try:
        from fetch import (   # type: ignore
            _esearch_query_variants,
            _parse_esearch_result_list,
            _split_us_address,
            NCAD_ESEARCH_BASE,
        )
    except Exception as exc:
        log.warning("could not import NCAD helpers from fetch.py: %s", exc)
        return 0

    try:
        from playwright.async_api import async_playwright  # type: ignore
    except ImportError:
        log.warning("playwright not installed — skipping cross-reference")
        return 0

    return asyncio.run(_async_cross_reference(
        eligible, _esearch_query_variants, _parse_esearch_result_list,
        _split_us_address, NCAD_ESEARCH_BASE, async_playwright))


async def _async_cross_reference(eligible, _esearch_query_variants,
                                   _parse_esearch_result_list,
                                   _split_us_address, NCAD_ESEARCH_BASE,
                                   async_playwright) -> int:
    from urllib.parse import urlencode
    enriched = 0

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True, args=["--no-sandbox"])
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/124.0.0.0 Safari/537.36")
        page = await context.new_page()

        # Mint NCAD session token from homepage.
        token = ""
        try:
            await page.goto(NCAD_ESEARCH_BASE + "/",
                             wait_until="domcontentloaded", timeout=30_000)
            await page.wait_for_timeout(500)
            token = await page.evaluate("""() => {
                const m = document.querySelector('meta[name="search-token"]');
                return m ? m.getAttribute('content') : '';
            }""") or ""
        except Exception as exc:
            log.warning("NCAD homepage load failed: %s", exc)

        log.info("NCAD session token: %s",
                  "present" if token else "missing")

        current_year = str(datetime.now(timezone.utc).year)

        for rec in eligible:
            raw_name = rec.get("owner", "")
            if not raw_name:
                continue

            # Try lookup with up to THREE name forms:
            #   1. The full borrower line as captured from PDF
            #      ("JOHN BRUNO AMARO AND WIFE, SANDRA ANN AMARO")
            #      — NCAD sometimes indexes joint owners this way
            #   2. The primary borrower name only
            #      ("JOHN BRUNO AMARO")
            #      — most common NCAD owner-name form
            #   3. The secondary borrower (spouse/co-borrower)
            #      ("SANDRA ANN AMARO")
            #      — for cases where the property is in spouse's name
            name_variants: List[str] = [raw_name]
            primary = _normalize_name_for_ncad(raw_name)
            if primary and primary != raw_name:
                name_variants.append(primary)
            secondary = _extract_secondary_name(raw_name)
            if secondary and secondary not in name_variants:
                name_variants.append(secondary)

            pdf_legal = (
                f"Lot: {rec.get('legal_lot', '')} "
                f"Block: {rec.get('legal_block', '')} "
                f"Subdivision- Name: {rec.get('legal_subdivision', '')}"
            )

            # === STAGE 1: name-based search with legal-description filter ===
            #
            # Two match tiers:
            #   STRONG: name search returns rows + legal descriptions match
            #   NONE:   no row's legal description matches
            #
            # We do NOT attempt a "weak" match (taking a row just because
            # the last name matches) because common surnames like SMITH,
            # RODRIGUEZ, GARCIA, MARTINEZ return many unrelated NCAD
            # records — a name-only match would often give the wrong
            # address. Better to admit no match than to invent one.

            best_match: Optional[Dict[str, str]] = None

            for name_idx, name in enumerate(name_variants):
                log.info("  cross-ref attempt %d/%d: %r",
                          name_idx + 1, len(name_variants), name)
                for variant in _esearch_query_variants(name)[:8]:
                    keywords = f"OwnerName:{variant} Year:{current_year} "
                    params = {"keywords": keywords}
                    if token:
                        params["searchSessionToken"] = token
                    url = (NCAD_ESEARCH_BASE + "/search/result?"
                            + urlencode(params))
                    try:
                        await page.goto(url, wait_until="domcontentloaded",
                                         timeout=20_000)
                        await page.wait_for_timeout(400)
                        html = await page.content()
                    except Exception:
                        continue

                    results = _parse_esearch_result_list(html)
                    if not results:
                        continue

                    # Accept any row whose legal description matches the
                    # PDF's. legal_descriptions_match requires same lot
                    # AND same block AND ≥1 shared subdivision token, so
                    # false positives are unlikely.
                    for res in results:
                        if legal_descriptions_match(
                                pdf_legal, res.get("legal", "")):
                            best_match = res
                            log.info("    STRONG match found "
                                     "(legal descriptions agree): %s",
                                     res.get("owner", ""))
                            break
                    if best_match:
                        break
                    await asyncio.sleep(1.0)
                if best_match:
                    break

            # === STAGE 2 (only if no strong match): legal-description search ===
            #
            # If name search comes up dry, try searching by the
            # subdivision name. NCAD's general keyword search may pick
            # up properties indexed under entity names that include
            # the subdivision (e.g. a builder/developer record). Filter
            # candidates strictly by legal-description match.
            if not best_match:
                subdivision = rec.get("legal_subdivision", "").strip()
                lot = rec.get("legal_lot", "").strip()
                if subdivision and lot:
                    log.info("    STAGE 2: legal-description search "
                              "with subdivision=%r lot=%r",
                              subdivision, lot)
                    keywords = (f"OwnerName:{subdivision} "
                                 f"Year:{current_year} ")
                    params = {"keywords": keywords}
                    if token:
                        params["searchSessionToken"] = token
                    url = (NCAD_ESEARCH_BASE + "/search/result?"
                            + urlencode(params))
                    try:
                        await page.goto(url,
                                         wait_until="domcontentloaded",
                                         timeout=20_000)
                        await page.wait_for_timeout(400)
                        html = await page.content()
                    except Exception:
                        pass
                    else:
                        results = _parse_esearch_result_list(html)
                        if results:
                            for res in results:
                                if legal_descriptions_match(
                                        pdf_legal,
                                        res.get("legal", "")):
                                    best_match = res
                                    log.info("    STAGE 2 match found")
                                    break
                    await asyncio.sleep(1.0)

            if best_match and best_match.get("situs"):
                site_addr, site_city, site_state, site_zip = (
                    _split_us_address(best_match["situs"]))
                if site_addr:
                    rec["prop_address"] = site_addr
                    rec["prop_city"] = site_city or "CORPUS CHRISTI"
                    rec["prop_state"] = site_state or "TX"
                    rec["prop_zip"] = site_zip
                    enriched += 1
                    log.info("  cross-ref %r → %s",
                              raw_name, site_addr)

                # Appraised value comes directly from the result-list row
                # (the BIS result-list parser pulls it from the
                # `_appraisedValueDisplay` cell).
                if best_match.get("appraised_value"):
                    rec["appraised_value"] = best_match["appraised_value"]
                    log.info("    appraised value: $%s",
                             f"{best_match['appraised_value']:,.0f}")
            else:
                log.info("  cross-ref %r → no match "
                         "(tried %d name variant(s) + legal fallback)",
                         raw_name, len(name_variants))

            await asyncio.sleep(1.5)

        await context.close()
        await browser.close()

    log.info("cross-ref: %d records gained address via legal-match", enriched)
    return enriched


if __name__ == "__main__":
    sys.exit(main())
