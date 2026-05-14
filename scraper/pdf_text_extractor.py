"""
Foreclosure PDF text extraction and field parsing.
==================================================

Reads text from a foreclosure PDF (via pdfplumber) and parses out
the fields we care about for motivated-seller marketing:

  * doc_number    — clerk-portal document number (e.g. "2026000263")
  * borrower      — homeowner whose property is being foreclosed
  * lender        — beneficiary / mortgagee
  * loan_amount   — original principal amount on the deed of trust
  * deed_date     — date the original deed of trust was executed
  * prop_address  — property street address (when in PDF)
  * prop_city / prop_state / prop_zip — parsed from the same address line
  * legal_subdivision / legal_lot / legal_block — for NCAD cross-referencing
    when the PDF doesn't include a street address

The PARSING here is what we want to preserve — independent of however
we obtain the PDFs (manual download today, possibly automated later).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("nueces-pdf-text")


# --------------------------------------------------------------------------- #
# pdfplumber-based text extraction
# --------------------------------------------------------------------------- #

def extract_text(pdf_path: Path) -> str:
    """Extract all text from a PDF.

    Tries pdfplumber first (fast, works for text-based PDFs). If that
    returns nothing — i.e. the PDF is a scanned image — falls back to
    OCR via pytesseract + pdf2image.

    Returns the concatenated text or "" if both extraction methods fail.
    """
    # First try: pdfplumber for text-based PDFs (fast).
    text = _extract_text_pdfplumber(pdf_path)
    if text and len(text.strip()) > 50:
        log.debug("pdfplumber extracted %d chars from %s",
                  len(text), pdf_path.name)
        return text

    # Fallback: OCR via tesseract for image-based PDFs.
    log.info("  pdfplumber found no text in %s — falling back to OCR...",
             pdf_path.name)
    text = _extract_text_ocr(pdf_path)
    if text:
        log.info("  OCR extracted %d chars from %s",
                 len(text), pdf_path.name)
    return text


def _extract_text_pdfplumber(pdf_path: Path) -> str:
    """Extract embedded text via pdfplumber. Fast but only works for
    text-based PDFs (not scanned images).
    """
    try:
        import pdfplumber  # type: ignore
    except ImportError:
        log.warning("pdfplumber not installed")
        return ""
    try:
        parts: List[str] = []
        with pdfplumber.open(str(pdf_path)) as pdf:
            for page in pdf.pages:
                t = page.extract_text() or ""
                if t.strip():
                    parts.append(t)
        return "\n\n".join(parts)
    except Exception as exc:
        log.debug("pdfplumber failed on %s: %s", pdf_path, exc)
        return ""


def _extract_text_ocr(pdf_path: Path) -> str:
    """Render PDF pages to images and OCR them via Tesseract.

    Requires `tesseract-ocr` and `poppler-utils` system packages, plus
    `pytesseract` and `pdf2image` Python packages.

    Only OCRs the FIRST PAGE by default — all the fields we need on a
    foreclosure notice (borrower, lender, loan amount, deed date,
    address, legal) are on page 1. Pages 2+ are boilerplate.
    Override with env var PDF_OCR_MAX_PAGES.
    """
    try:
        import pytesseract  # type: ignore
        from pdf2image import convert_from_path  # type: ignore
    except ImportError as exc:
        log.error("OCR dependencies not installed: %s — install "
                  "pytesseract + pdf2image, plus tesseract-ocr and "
                  "poppler-utils system packages", exc)
        return ""

    import os
    max_pages = int(os.environ.get("PDF_OCR_MAX_PAGES", "1"))
    try:
        # 200 DPI is the sweet spot for OCR accuracy vs. speed/memory.
        # Higher DPI = better quality but exponentially slower.
        images = convert_from_path(
            str(pdf_path), dpi=200, first_page=1, last_page=max_pages)
    except Exception as exc:
        log.warning("pdf2image failed on %s: %s", pdf_path, exc)
        return ""

    parts: List[str] = []
    for i, img in enumerate(images, start=1):
        try:
            # PSM 6 = "Assume a single uniform block of text". Works
            # better than the default for documents with mixed columns.
            text = pytesseract.image_to_string(img, config="--psm 6")
            if text.strip():
                parts.append(text)
                log.debug("  OCR page %d: %d chars", i, len(text))
        except Exception as exc:
            log.warning("tesseract failed on %s page %d: %s",
                        pdf_path.name, i, exc)
            continue

    return "\n\n".join(parts)


# --------------------------------------------------------------------------- #
# Field regex patterns
# --------------------------------------------------------------------------- #

# Document number — Nueces foreclosures stamp the header line at the
# top of every PDF in this exact format:
#   "2026 - 2026000256 04/30/2026 10:56 AM Page 1 of 2"
# This is the ONLY reliable way to identify the foreclosure-notice doc
# number — the body of the notice references OTHER doc numbers (the
# original deed of trust instrument, modifications, etc.) and matching
# those would corrupt the record matching.
_RE_DOC_NUM = [
    # The clerk header line — unambiguous.
    re.compile(r"^\s*\d{4}\s*[-–]\s*(\d{10})\s+\d{1,2}/\d{1,2}/\d{2,4}",
               re.MULTILINE),
]

# Borrower name. Texas foreclosure notices come from different law
# firms with different templates. We've seen these patterns:
#
#   Mackie Wolf:    "Deed of Trust executed by JOHN DOE, provides..."
#   McCarthy:       "10/17/2011 ELAINE SALAZAR, AN UNMARRIED WOMAN,"
#                   (header line: date<space>name<comma>)
#   Nestor:         "Grantor(s): NAME and NAME husband and wife"
#                   (key:value table format)
#   Schmitt:        "NAME ("Borrower"), executed and delivered"
#                   (parenthesized role label)
#   Granado:        "Grantor: NAME AND WIFE, NAME"
#                   (key:value table format, lowercase variant)
#
# The character class accepts OCR artifacts:
#   - Square brackets [] (OCR misreads III as II])
#   - Periods (middle initials, name suffixes)
#   - Apostrophes (O'BRIEN), ampersands (& sons)
_RE_BORROWER = [
    # "executed by NAME, provides" — original Mackie Wolf template
    re.compile(r"executed\s+by\s+([A-Z][A-Z0-9\s&'.\[\]()-]+?)"
                r"\s*,\s*(?:provides|as\s+(?:a|the|his|her)|whose|"
                r"a\s+(?:single|married)|husband|wife|"
                r"an?\s+(?:unmarried|single|married))",
               re.IGNORECASE),
    # "executed by NAME (without comma)" — fallback for Mackie Wolf
    re.compile(r"executed\s+by\s+([A-Z][A-Z0-9\s&'.\[\]()-]+?)"
                r"(?=\s+(?:dated|to\s+\w+|in\s+favor\s+of|"
                r"and\s+(?:recorded|filed)))",
               re.IGNORECASE),
    # 'NAME ("Borrower"), executed and delivered' — Schmitt template
    re.compile(r"([A-Z][a-zA-Z\s&'.\[\]()-]{4,80}?)\s*"
                r"\(['\"]?Borrower['\"]?\)"),
    # "WHEREAS, NAMES, executed and delivered to" — Avots template (260)
    # Capture all borrowers from start of WHEREAS clause.
    re.compile(r"WHEREAS,\s+([A-Z][A-Z\s.,&'-]+?)"
                r"[,\s]+executed\s+and\s+delivered\s+to",
               re.IGNORECASE),
    # "NAMES conveyed to <trustee>" — Arnold Gonzales template (261)
    # The borrowers conveyed the property to the trustee.
    re.compile(r"\b([A-Z][A-Z\s,&'.-]{4,100}?)\s+conveyed\s+to\s+\w",
               re.IGNORECASE),
    # "NAMES, as Grantor(s)" — Hughes Watters / SPS template (262)
    re.compile(r"\b([A-Z][A-Z\s.,&'-]{4,100}?)[,\s]+as\s+Grantor\(?s?\)?",
               re.IGNORECASE),
    # "Grantor(s): NAMES" or "Grantor(s):; NAMES" — table-format
    # templates (Nestor, Hughes Watters/Rally CU). OCR sometimes
    # introduces a semicolon between the colon and the name.
    re.compile(r"Grantor\(?s?\)?[\s:;]+([A-Z][a-zA-Z0-9\s,&'.\[\]()-]{4,120}?)"
                r"(?=\s*\n|\s+Original\s+(?:Trustee|Mortgagee|Lender)|"
                r"\s+Current\s+(?:Mortgagee|Beneficiary)|"
                r"\s+Lender:|\s+Mortgage\s+Servicer:)",
               re.IGNORECASE),
    # Header-line form: "MM/DD/YYYY NAME, MORE_DESCRIPTORS,"
    # Used by McCarthy & Holthus (template 251).
    re.compile(r"^\s*\d{1,2}/\d{1,2}/\d{4}\s+"
                r"([A-Z][A-Z\s&'.\[\]()-]{4,80}?)"
                r"(?=,\s*(?:AN?|HUSBAND|WIFE|A\s+(?:SINGLE|MARRIED))"
                r"|,\s*$)",
               re.MULTILINE),
    # Generic Mortgagor/Obligor/Debtor labels
    re.compile(r"mortgagor[s]?(?:\(s\))?[\s:]+([A-Z][A-Z0-9\s&'.\[\]()-]{3,80}?)"
                r"(?=\s*,|\s+(?:to|in\s+favor|and|dated|provides))",
               re.IGNORECASE),
    re.compile(r"obligor[s]?(?:\(s\))?[\s:]+([A-Z][A-Z0-9\s&'.\[\]()-]{3,80}?)"
                r"(?=\s*,|\s+(?:to|in\s+favor|and|dated|provides))",
               re.IGNORECASE),
    re.compile(r"debtor[s]?(?:\(s\))?[\s:]+([A-Z][A-Z0-9\s&'.\[\]()-]{3,80}?)"
                r"(?=\s*,|\s+(?:to|in\s+favor|and|dated|provides))",
               re.IGNORECASE),
    re.compile(r"(?:property\s+of|record\s+owner)[s]?[\s:]+"
                r"([A-Z][A-Z0-9\s&'.\[\]()-]{3,80}?)(?=\s*,|\n)",
               re.IGNORECASE),
]

# Lender / mortgagee / beneficiary.
# Different templates use different labels. Anchor each pattern to a
# specific label so we don't accidentally grab body text.
_RE_LENDER = [
    # 'for the benefit of NAME ("Lender")' — Schmitt template
    re.compile(r"for\s+the\s+benefit\s+of\s+([A-Z][A-Za-z0-9\s,&.'-]+?)"
                r"\s*\(['\"]?Lender['\"]?\)"),
    # "Current Beneficiary/Mortgagee: ... ACTUAL_LENDER" — McCarthy template.
    # MUST come before the generic "Mortgagee:" pattern below — McCarthy's
    # text starts with "Current Beneficiary/Mortgagee:" on its own line
    # then "MORTGAGE ELECTRONIC..." plus the real lender on the next.
    # MERS is a nominee — the REAL lender is the part AFTER MERS.
    re.compile(r"Current\s+Beneficiary/?Mortgagee[\s:]*\s*"
                r"MORTGAGE\s+ELECTRONIC\s+REGISTRATION\s+SYSTEMS,?\s*INC\.?\s+"
                r"([A-Z][A-Za-z0-9\s&,.'-]+?)"
                r"(?=\s*\n|\s*\([\"']MERS[\"']\))",
               re.IGNORECASE),
    # "Current Mortgagee: NAME" — Nestor/Hughes Watters table format.
    # Requires literal colon so body text like "is the current mortgagee
    # of the note" doesn't match.
    re.compile(r"Current\s+Mortgagee:\s+([A-Z][A-Za-z0-9\s&,.'-]{3,80}?)"
                r"(?=\s*\n|\s+(?:Mortgage\s+Servicer|TS\.\s*#|Original|Mortgagees))",
               re.IGNORECASE),
    # "Mortgagee: NAME" — Hughes Watters / SPS template (262).
    # Capture up to a newline OR a specific stop marker.
    # Comes AFTER McCarthy MERS pattern above so that template's text
    # ("Current Beneficiary/Mortgagee:\nMORTGAGE ELECTRONIC...")
    # doesn't accidentally match here.
    re.compile(r"(?<!Beneficiary/)(?<!Current\s)\bMortgagee:\s+"
                r"([A-Z][A-Za-z0-9\s,&.'-]+?)"
                r"(?=\n|\d{4}\s|SUBSTITUTE\s+TRUSTEE|"
                r"Mortgage\s+Servicer)",
               re.IGNORECASE),
    # "Lender: NAME" — Granado-style table format. Stop at newline or
    # known following labels.
    re.compile(r"\bLender:\s+([A-Z][A-Za-z0-9\s&,.'-]{3,80}?)"
                r"(?=\s*\n|\s+(?:Note|Substitute\s+Trustee|hereby|has))",
               re.IGNORECASE),
    # "NAME is the present owner and holder" — Avots template (260).
    # The real lender is the person/entity who owns the note.
    # Capture just the last 1-4 capitalized words (avoids backtracking
    # all the way to the preceding clause).
    re.compile(r"\b((?:[A-Z][a-zA-Z]*\.?\s+){1,4}[A-Z][a-zA-Z]+)"
                r"\s+is\s+the\s+present\s+(?:owner|beneficiary|holder)",
               re.IGNORECASE),
    # "for the benefit of [the] NAME-WITH-TRUSTY-SUFFIX" — Arnold Gonzales
    # template (261) where the lender is an ESTATE/TRUST.
    re.compile(r"for\s+the\s+benefit\s+of\s+(?:the\s+)?"
                r"([A-Z][A-Z\s,'.-]+?(?:DECEASED|TRUST|FOUNDATION|"
                r"ASSOCIATION|CORPORATION|COMPANY|INC|LLC|N\.A\.|BANK))",
               re.IGNORECASE),
    # "NAME is the current mortgagee" — Mackie Wolf / Power Default
    # templates. Anchor to start of sentence (period or newline) so we
    # capture the FULL lender name (not just the last few words).
    re.compile(r"(?:^|\.|\n)\s*([A-Z][\w\s.,&'-]{4,200}?)\s+is\s+the\s+"
                r"current\s+mortgagee",
               re.MULTILINE),
    # "Beneficiary: NAME" — fallback
    re.compile(r"\bBeneficiary:\s+([A-Z][A-Za-z0-9\s&,.'-]{3,80}?)"
                r"(?=\s*\n|\s+(?:Note|Trustee))",
               re.IGNORECASE),
    # "in favor of NAME" — Mackie Wolf body text
    re.compile(r"in\s+favor\s+of\s+([A-Z][A-Za-z0-9\s,&.'-]{3,80}?)"
                r"(?=\s*,\s*(?:its|a\s+\w+|as\s+|whose|located)|\.|\n|recorded)"),
]

# Loan amount — original principal.
# Templates vary widely. Patterns ordered by specificity (most specific first).
_RE_LOAN_AMOUNT = [
    re.compile(r"original\s+principal\s+(?:balance|amount|sum)?[:\s]+"
                r"\$\s*([\d,]+(?:\.\d{2})?)",
               re.IGNORECASE),
    re.compile(r"principal\s+(?:sum|amount)\s+of\s+\$\s*([\d,]+(?:\.\d{2})?)",
               re.IGNORECASE),
    re.compile(r"in\s+the\s+(?:original\s+)?(?:principal\s+)?amount\s+of\s+"
                r"\$\s*([\d,]+(?:\.\d{2})?)",
               re.IGNORECASE),
    # "Note: WORDS WORDS ($43,750.00)" — Granado / Schmitt format.
    # Allow up to ~200 chars between "Note:" and the parenthesized amount
    # since lenders spell out the amount in words first.
    re.compile(r"Note[\s:][\s\S]{1,200}?\(\s*\$\s*([\d,]+(?:\.\d{2})?)\s*\)",
               re.IGNORECASE),
    # "Amount: $190,400.00" — Hughes Watters table-format template (263).
    # Anchor to start-of-line or whitespace so we don't match the
    # word "amount" inside other phrases.
    re.compile(r"(?:^|\n)\s*Amount:\s+\$\s*([\d,]+(?:\.\d{2})?)",
               re.IGNORECASE),
    # "principal amount of $XXX" generic
    re.compile(r"principal\s+amount\s+of\s+\$\s*([\d,]+(?:\.\d{2})?)",
               re.IGNORECASE),
    # "Note in the amount of $XXX"
    re.compile(r"note[^.]{0,40}?\$\s*([\d,]+(?:\.\d{2})?)",
               re.IGNORECASE),
]

# Deed of trust date — when the original loan was executed.
_RE_DEED_DATE = [
    # "Deed of Trust dated MM/DD/YYYY" or "Deed of Trust Dated MM/DD/YYYY"
    # (case-insensitive)
    re.compile(r"deed\s+of\s+trust\s+(?:dated|executed\s+on)[\s:]+"
                r"([A-Za-z]+\s+\d{1,2},?\s+\d{4})",
               re.IGNORECASE),
    # "Deed of Trust or Contract Lien dated MM/DD/YYYY" — Power Default
    # template (264).
    re.compile(r"deed\s+of\s+trust(?:\s+or\s+contract\s+lien)?\s+"
                r"(?:dated|executed)\s+(\d{1,2}/\d{1,2}/\d{2,4})",
               re.IGNORECASE),
    # Generic "dated <date>" — fallback.
    re.compile(r"(?:dated|executed)\s+([A-Za-z]+\s+\d{1,2},?\s+\d{4})",
               re.IGNORECASE),
]

# Property street address — multiple strategies.
#
# Strategy 1: explicit label like "Commonly known as: ADDRESS",
# "Property Address: ADDRESS", or "(Address: ADDRESS)" — most reliable
# when present. Different law firms use different label phrases.
_RE_LABELED_ADDRESS = re.compile(
    r"\(?\s*(?:Commonly\s+known\s+as|Property\s+Address|"
    r"Property\s+is\s+located\s+at|Address)"
    r"[\s:]+"
    r"(\d{1,5}[A-Z]?\s+[A-Z][A-Za-z0-9\s.]{2,60}?\b"
    r"(?:STREET|ST|AVENUE|AVE|ROAD|RD|DRIVE|DR|LANE|LN|"
    r"BOULEVARD|BLVD|COURT|CT|CIRCLE|CIR|PLACE|PL|"
    r"WAY|TRAIL|TR|PARKWAY|PKWY|HIGHWAY|HWY|TERRACE|TER)\.?)"
    r"[.,\s]+([A-Z][a-zA-Z\s!?]+?)"
    r"(?:[.,]\s+Nueces\s+County)?"
    r"[.,\s]+(?:TX|TEXAS)[.\s]+(\d{5})\)?",
    re.IGNORECASE,
)

# Strategy 2: full address pattern — number + street + suffix + city + TX + zip
# in one shot. Allows OCR slop in city names.
_RE_FULL_ADDRESS = re.compile(
    r"(\d{1,5}[A-Z]?\s+[A-Z][A-Z0-9\s.]{3,60}?\b"
    r"(?:STREET|ST|AVENUE|AVE|ROAD|RD|DRIVE|DR|LANE|LN|"
    r"BOULEVARD|BLVD|COURT|CT|CIRCLE|CIR|PLACE|PL|"
    r"WAY|TRAIL|TR|PARKWAY|PKWY|HIGHWAY|HWY|TERRACE|TER)\.?)"
    r"[.,\s]+([A-Z][A-Z\s!?]+?)[.,\s]+(?:TX|TEXAS)[.\s]+(\d{5})",
    re.IGNORECASE,
)

# Cities served by the Nueces County Clerk. Used to prefer the property
# address over any other addresses on the page (e.g. law firm in Dallas,
# Houston trustee, etc.).
_NUECES_CITIES = ("CORPUS CHRISTI", "ROBSTOWN", "PORT ARANSAS", "BISHOP",
                  "DRISCOLL", "AGUA DULCE", "BANQUETE",
                  # OCR slop forms
                  "CORPUS CHRIST", "CORPUSCHRISTI", "CORPUS")

# Known non-property addresses to blacklist. The Nueces County courthouse
# at 901 LEOPARD STREET is mentioned in nearly every foreclosure notice
# as the "Place of Sale" — we must NOT pick it up as the property address.
_BLACKLIST_ADDRESSES = (
    "901 LEOPARD STREET", "901 LEOPARD ST", "901 LEOPARD",
)

# Legal description — Texas foreclosure notices spell out lot/block as
# words AND digits: "LOT EIGHTEEN (18), BLOCK ONE (1), COUNTRY CLUB
# ESTATES, UNIT 30, A SUBDIVISION..."
# Spelled-out numbers can include hyphens (FORTY-SIX) and multiple words.
_RE_LEGAL_PARENS = re.compile(
    r"LOT[S]?\s+[\w-]+(?:\s+[\w-]+)*?\s*\((\d+)\)[,\s]+"
    r"BLOCK\s+[\w-]+(?:\s+[\w-]+)*?\s*\((\d+)\)[,\s]+"
    r"([A-Z][A-Z\s.,&'-]+?)"
    r"(?:[,.]?\s+UNIT\s+(\d+))?"
    r"\s*,?\s*(?:AN?\s+)?(?:SUBDIVISION|ADDITION|ACCORDING\s+TO)",
    re.IGNORECASE | re.DOTALL,
)

# Fallback legal description — digit-only form: "Lot 5, Block 12, BAY OAKS..."
_RE_LEGAL_DIGITS = re.compile(
    r"(?:Lot[s]?\s+)?([\d,A-Z-]+)[\s,]+(?:Block|Blk\.?)\s+([\dA-Z-]+)"
    r"[,\s]+(?:of\s+)?([A-Z][A-Z\s\d&.'-]+?)\s+(?:Subdivision|Addition|"
    r"Unit|Section|Phase)",
    re.IGNORECASE,
)


# --------------------------------------------------------------------------- #
# Top-level parser
# --------------------------------------------------------------------------- #

def parse_foreclosure_pdf_text(text: str) -> Dict[str, Any]:
    """Apply regex patterns to extract structured fields from a foreclosure
    PDF's text. Returns dict with whatever was found (partial OK).
    """
    if not text:
        return {}
    out: Dict[str, Any] = {}

    # --- Document number ---
    for rx in _RE_DOC_NUM:
        m = rx.search(text)
        if m:
            dn = m.group(1).strip()
            if len(dn) == 10:
                out["doc_number"] = dn
                break

    # --- Borrower ---
    for rx in _RE_BORROWER:
        m = rx.search(text)
        if m:
            name = _clean_name_ocr(m.group(1))
            if name and len(name) >= 4 and not _looks_like_lender(name):
                out["borrower"] = name
                break

    # --- Lender ---
    for rx in _RE_LENDER:
        m = rx.search(text)
        if m:
            name = _clean_name(m.group(1))
            if name and len(name) >= 3:
                out["lender"] = name
                break

    # --- Loan amount ---
    for rx in _RE_LOAN_AMOUNT:
        m = rx.search(text)
        if m:
            try:
                amt = float(m.group(1).replace(",", ""))
                if 1000 < amt < 10_000_000:   # sanity bounds
                    out["loan_amount"] = amt
                    break
            except ValueError:
                continue

    # --- Deed-of-trust date ---
    for rx in _RE_DEED_DATE:
        m = rx.search(text)
        if m:
            # Collapse whitespace including OCR-introduced newlines
            # (e.g. "May 16,\n2019" → "May 16, 2019")
            d = re.sub(r"\s+", " ", m.group(1)).strip()
            out["deed_date"] = d
            break

    # --- Property street address ---
    # Strategy: prefer addresses preceded by explicit labels like
    # "Commonly known as:" or "Property Address:". Fall back to scanning
    # all street-shaped strings, preferring Nueces-area cities and
    # skipping the Nueces County courthouse (which is mentioned in every
    # foreclosure notice as the place of sale).
    chosen_street = None
    chosen_city = None
    chosen_zip = None

    # First try: explicit label
    m_lbl = _RE_LABELED_ADDRESS.search(text)
    if m_lbl:
        chosen_street = m_lbl.group(1)
        chosen_city = m_lbl.group(2)
        chosen_zip = m_lbl.group(3)

    # Second try: scan all addresses and skip blacklisted ones
    if not chosen_street:
        all_addrs = list(_RE_FULL_ADDRESS.finditer(text))
        candidates = []
        for m in all_addrs:
            street_upper = re.sub(r"\s+", " ",
                                    m.group(1)).upper().strip(" ,.")
            if any(bl in street_upper for bl in _BLACKLIST_ADDRESSES):
                continue
            city_upper = m.group(2).upper().strip()
            is_local = any(c in city_upper for c in _NUECES_CITIES)
            candidates.append((m, is_local))
        local_addr = next((m for m, is_local in candidates if is_local), None)
        if local_addr is None and candidates:
            local_addr = candidates[-1][0]
        if local_addr:
            chosen_street = local_addr.group(1)
            chosen_city = local_addr.group(2)
            chosen_zip = local_addr.group(3)

    if chosen_street:
        street = re.sub(r"\s+", " ", chosen_street).upper().strip(" ,.")
        city = re.sub(r"[!?]", "I", chosen_city).upper().strip()
        if city in ("CORPUS CHRIST", "CORPUSCHRISTI", "CORPUS"):
            city = "CORPUS CHRISTI"
        city = re.sub(r"\s+", " ", city)
        out["prop_address"] = street
        out["prop_city"] = city
        out["prop_state"] = "TX"
        out["prop_zip"] = chosen_zip

    # --- Legal description ---
    # Try the parenthesized form first (LOT EIGHTEEN (18), BLOCK ONE (1)…),
    # then fall back to digit-only ("Lot 18, Block 1…").
    m = _RE_LEGAL_PARENS.search(text)
    if not m:
        m = _RE_LEGAL_DIGITS.search(text)
    if m:
        out["legal_lot"] = m.group(1).strip(" ,.")
        out["legal_block"] = m.group(2).strip(" ,.")
        sub = _clean_name(m.group(3))
        # Collapse newlines/whitespace and remove stray periods from
        # OCR-introduced line breaks within multi-word subdivision names
        # (e.g. "COUNTRY CLUB.\nESTATES" → "COUNTRY CLUB ESTATES").
        sub = re.sub(r"\s+", " ", sub).strip(" .,")
        sub = re.sub(r"\.\s+", " ", sub).strip()
        out["legal_subdivision"] = sub
        # Some patterns capture a unit group; preserve it if matched
        if m.lastindex and m.lastindex >= 4 and m.group(4):
            out["legal_unit"] = m.group(4).strip()

    return out


def _clean_name(s: str) -> str:
    """Trim whitespace and strip trailing punctuation/conjunctions."""
    if not s:
        return ""
    s = re.sub(r"\s+", " ", s).strip(" ,.;:&-")
    s = re.sub(r"\s+(and|to|in|the)\s*$", "", s, flags=re.IGNORECASE)
    return s.strip()


def _clean_name_ocr(s: str) -> str:
    """Like _clean_name() but additionally fixes common OCR artifacts that
    appear in borrower names from scanned foreclosure notices:

      - "II]"   → "III"     (OCR misreads the third I as ])
      - "[II"   → "III"
      - "[V"    → "IV"
      - Bracket-only segments → "I"

    Then runs the standard _clean_name() pass.
    """
    if not s:
        return ""
    # Fix Roman-numeral OCR errors (the most common in suffixes).
    # Order matters: longer patterns first.
    s = re.sub(r"\bII\]", "III", s)
    s = re.sub(r"\[II\b", "III", s)
    s = re.sub(r"\bI\]", "II", s)
    s = re.sub(r"\[V\b", "IV", s)
    # Remove any remaining stray brackets (rare).
    s = re.sub(r"[\[\]]", "I", s)
    return _clean_name(s)


def _looks_like_lender(name: str) -> bool:
    """Crude heuristic: does this name look like a bank/financial institution
    rather than an individual? Used to reject false-positive borrower matches.

    Uses word-boundary matching to avoid false positives like "PENA" being
    flagged because it contains "NA".
    """
    upper = name.upper()
    # Word-boundary patterns — avoid substring matches inside person names.
    lender_patterns = [
        r"\bBANK\b", r"\bN\.?A\.?\b", r"\bFEDERAL\b", r"\bMORTGAGE\b",
        r"\bFINANCIAL\b", r"\bCAPITAL\b", r"\bTRUST\s+COMPANY\b",
        r"\bSAVINGS\b", r"\bASSOCIATION\b", r"\bFANNIE\s+MAE\b",
        r"\bFREDDIE\s+MAC\b", r"\bWELLS\s+FARGO\b", r"\bCHASE\b",
        r"\bCITIBANK\b", r"\bBANK\s+OF\s+AMERICA\b", r"\bLLC\b",
        r"\bCORP(?:ORATION)?\b", r"\bCOMPANY\b", r"\bINC\.?\b",
    ]
    for pat in lender_patterns:
        if re.search(pat, upper):
            return True
    return False


# --------------------------------------------------------------------------- #
# Legal description normalization (for cross-referencing PDF ↔ NCAD)
# --------------------------------------------------------------------------- #

def normalize_legal_for_match(legal: str) -> Tuple[str, str, str]:
    """Normalize a legal description into (subdivision, lot, block) tokens
    for cross-source matching.

    Examples:
      'LOT 9 BLOCK 2 DOUGLAS UNIT TWO ADDITION'
        → ('douglas unit 2', '9', '2')
      'Subdivision- Name: DOUGLAS UNIT 2 Lot: 9 Block: 2'
        → ('douglas unit 2', '9', '2')
    """
    if not legal:
        return ("", "", "")
    s = legal.upper()

    lot = ""
    m = re.search(r"\b(?:LOT[S]?|LTS?)\s*[:\-]?\s*([\d,A-Z-]+)", s)
    if m:
        lot = m.group(1).strip(" ,-")

    block = ""
    m = re.search(r"\bBLOCK\s*[:\-]?\s*([\dA-Z-]+)", s)
    if m:
        block = m.group(1).strip(" ,-")
    else:
        # NCAD abbreviates "Block" as either BLK or BK
        m = re.search(r"\b(?:BLK|BK)\.?\s*[:\-]?\s*([\dA-Z-]+)", s)
        if m:
            block = m.group(1).strip(" ,-")

    # Subdivision: strip the structural tokens.
    sub = re.sub(r"SUBDIVISION[\s\-]+NAME[\s:]*", " ", s)
    sub = re.sub(r"\b(?:LOT[S]?|LTS?)\s*[:\-]?\s*[\d,A-Z-]+", " ", sub)
    sub = re.sub(r"\bBLOCK\s*[:\-]?\s*[\dA-Z-]+", " ", sub)
    sub = re.sub(r"\b(?:BLK|BK)\.?\s*[:\-]?\s*[\dA-Z-]+", " ", sub)
    sub = re.sub(r"\bSUBDIVISION\b|\bADDITION\b|\bSECTION\b|\bPHASE\b",
                  " ", sub)
    sub = re.sub(r"[\(\)]", " ", sub)
    sub = re.sub(r"\s+", " ", sub).strip(" ,-:")

    # Normalize number-word forms: UNIT TWO → UNIT 2
    NUM_WORDS = {"ONE": "1", "TWO": "2", "THREE": "3", "FOUR": "4",
                 "FIVE": "5", "SIX": "6", "SEVEN": "7", "EIGHT": "8",
                 "NINE": "9", "TEN": "10"}
    for word, num in NUM_WORDS.items():
        sub = re.sub(rf"\bUNIT\s+{word}\b", f"UNIT {num}", sub)

    return (sub.lower(), lot, block)


def legal_descriptions_match(a: str, b: str) -> bool:
    """True if two legal descriptions probably refer to the same property."""
    sa, la, ba = normalize_legal_for_match(a)
    sb, lb, bb = normalize_legal_for_match(b)
    if not sa or not sb:
        return False
    if not la or not lb:
        return False
    if la != lb:
        return False
    if ba and bb and ba != bb:
        return False
    # Subdivision: at least 1 meaningful token in common.
    STOP = {"the", "of", "a", "an"}
    a_tokens = set(sa.split()) - STOP
    b_tokens = set(sb.split()) - STOP
    return len(a_tokens & b_tokens) >= 1
