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

# Document number — Nueces foreclosures use a 10-digit YYYYNNNNNN format.
# May appear as "Document No. 2026000263" or in a header banner.
_RE_DOC_NUM = [
    re.compile(r"(?:document|instrument|doc)\s*(?:no|number|#)\.?[\s:]*(\d{10})",
               re.IGNORECASE),
    # Header banner format: "2026 - 2026000264 05/07/2026"
    re.compile(r"^\s*\d{4}\s*[-–]\s*(\d{10})\s+\d{1,2}/\d{1,2}/\d{2,4}",
               re.MULTILINE),
    # Just any standalone 10-digit number starting with year — fallback
    re.compile(r"\b(202\d{7})\b"),
]

# Borrower name. Foreclosure notices in Texas typically use one of
# these phrasings:
#   "Deed of Trust executed by JOHN DOE AND JANE DOE, provides..."
#   "Deed of Trust executed by MELANIE SANDERS, AN UNMARRIED WOMAN ..."
#   "Mortgagor: JOHN DOE"
#   "Obligor: JOHN DOE"
#   "Grantor(s): JOHN DOE"
#
# The character class is permissive to handle OCR artifacts:
#   - Square brackets [] (OCR misreads III as II])
#   - Parens (suffix like JR/SR sometimes appears in parens)
#   - Apostrophes (O'BRIEN), periods (middle initials), ampersands (& sons)
#
# Stop conditions are anchored to phrases that follow the name in real
# foreclosure notices: ", provides", "dated", "to <party>", etc.
_RE_BORROWER = [
    re.compile(r"executed\s+by\s+([A-Z][A-Z0-9\s&'.\[\]()-]+?)"
                r"\s*,\s*(?:provides|as\s+(?:a|the|his|her)|whose|"
                r"a\s+(?:single|married)|husband|wife|"
                r"an?\s+(?:unmarried|single|married))",
               re.IGNORECASE),
    re.compile(r"executed\s+by\s+([A-Z][A-Z0-9\s&'.\[\]()-]+?)"
                r"(?=\s+(?:dated|to\s+\w+|in\s+favor\s+of|"
                r"and\s+(?:recorded|filed)))",
               re.IGNORECASE),
    re.compile(r"mortgagor[s]?(?:\(s\))?[\s:]+([A-Z][A-Z0-9\s&'.\[\]()-]{3,80}?)"
                r"(?=\s*,|\s+(?:to|in\s+favor|and|dated|provides))",
               re.IGNORECASE),
    re.compile(r"obligor[s]?(?:\(s\))?[\s:]+([A-Z][A-Z0-9\s&'.\[\]()-]{3,80}?)"
                r"(?=\s*,|\s+(?:to|in\s+favor|and|dated|provides))",
               re.IGNORECASE),
    re.compile(r"grantor[s]?(?:\(s\))?[\s:]+([A-Z][A-Z0-9\s&'.\[\]()-]{3,80}?)"
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
_RE_LENDER = [
    # "MIDFIRST BANK is the current mortgagee" (Texas foreclosure notice
    # standard phrasing — usually the most reliable lender source)
    re.compile(r"([A-Z][A-Z0-9\s&,.'-]+?)\s+is\s+the\s+current\s+mortgagee"),
    re.compile(r"in\s+favor\s+of\s+([A-Z][A-Za-z\s,&.'-]{3,80}?)"
                r"(?=\s*,\s*(?:its|a\s+\w+|as\s+|whose|located)|\.|\n|recorded)"),
    re.compile(r"lender[\s:]+([A-Z][A-Za-z\s,&.'-]{3,80}?)(?=\s*[,.\n])",
               re.IGNORECASE),
    re.compile(r"mortgagee[\s:]+([A-Z][A-Za-z\s,&.'-]{3,80}?)(?=\s*[,.\n])",
               re.IGNORECASE),
    re.compile(r"beneficiary[\s:]+([A-Z][A-Za-z\s,&.'-]{3,80}?)(?=\s*[,.\n])",
               re.IGNORECASE),
    re.compile(r"(?:current\s+(?:mortgagee|noteholder)|noteholder)[\s:]+"
                r"([A-Z][A-Za-z\s,&.'-]{3,80}?)(?=\s*[,.\n])",
               re.IGNORECASE),
]

# Loan amount — original principal.
_RE_LOAN_AMOUNT = [
    re.compile(r"original\s+principal\s+(?:balance|amount|sum)?[:\s]+"
                r"\$\s*([\d,]+(?:\.\d{2})?)",
               re.IGNORECASE),
    re.compile(r"principal\s+(?:sum|amount)\s+of\s+\$\s*([\d,]+(?:\.\d{2})?)",
               re.IGNORECASE),
    re.compile(r"in\s+the\s+(?:original\s+)?(?:principal\s+)?amount\s+of\s+"
                r"\$\s*([\d,]+(?:\.\d{2})?)",
               re.IGNORECASE),
    # "Note in the amount of $XXX" — generic
    re.compile(r"note[^.]{0,40}?\$\s*([\d,]+(?:\.\d{2})?)",
               re.IGNORECASE),
    # As a last resort, the largest dollar amount on the doc is often the loan
    # (we filter for $-prefixed numbers >= $10,000)
]

# Deed of trust date — when the original loan was executed.
_RE_DEED_DATE = [
    re.compile(r"deed\s+of\s+trust\s+(?:dated|executed\s+on)\s+"
                r"([A-Za-z]+\s+\d{1,2},?\s+\d{4})",
               re.IGNORECASE),
    re.compile(r"(?:dated|executed)\s+([A-Za-z]+\s+\d{1,2},?\s+\d{4})",
               re.IGNORECASE),
    # MM/DD/YYYY format
    re.compile(r"deed\s+of\s+trust\s+(?:dated|executed)\s+"
                r"(\d{1,2}/\d{1,2}/\d{2,4})",
               re.IGNORECASE),
]

# Property street address — full address pattern: number + street + suffix
# + city + TX + zip in one shot. Allows OCR slop in the city name (e.g.
# OCR turns "CHRISTI" into "CHRIST!" or "CORPUSCHRISTI").
_RE_FULL_ADDRESS = re.compile(
    r"(\d{1,5}[A-Z]?\s+[A-Z][A-Z0-9\s.]{3,60}?\b"
    r"(?:STREET|ST|AVENUE|AVE|ROAD|RD|DRIVE|DR|LANE|LN|"
    r"BOULEVARD|BLVD|COURT|CT|CIRCLE|CIR|PLACE|PL|"
    r"WAY|TRAIL|TR|PARKWAY|PKWY|HIGHWAY|HWY|TERRACE|TER)\.?)"
    r"[,\s]+([A-Z][A-Z\s!?]+?)[,\s]+(?:TX|TEXAS)[\s.]+(\d{5})",
    re.IGNORECASE,
)

# Cities served by the Nueces County Clerk. Used to prefer the property
# address over any other addresses on the page (e.g. law firm in Dallas,
# Houston trustee, etc.).
_NUECES_CITIES = ("CORPUS CHRISTI", "ROBSTOWN", "PORT ARANSAS", "BISHOP",
                  "DRISCOLL", "AGUA DULCE", "BANQUETE",
                  # OCR slop forms
                  "CORPUS CHRIST", "CORPUSCHRISTI", "CORPUS")

# Legal description — Texas foreclosure notices spell out lot/block as
# words AND digits: "LOT EIGHTEEN (18), BLOCK ONE (1), COUNTRY CLUB
# ESTATES, UNIT 30, A SUBDIVISION..."
# Prefer the parenthesized digit form for parsing.
_RE_LEGAL_PARENS = re.compile(
    r"LOT[S]?\s+\w+[\w\s]*\((\d+)\)[,\s]+"
    r"BLOCK\s+\w+\s*\((\d+)\)[,\s]+"
    r"([A-Z][A-Z\s.,&'-]+?)"
    r"(?:[,.]?\s+UNIT\s+(\d+))?"
    r"\s*,?\s*(?:A\s+)?(?:SUBDIVISION|ADDITION|ACCORDING\s+TO)",
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
            out["deed_date"] = m.group(1).strip()
            break

    # --- Property street address ---
    # Find ALL addresses in the document; prefer ones in Nueces County
    # cities (the law firm / trustee addresses appear too, in Dallas or
    # Houston, so we don't want to grab those).
    all_addrs = list(_RE_FULL_ADDRESS.finditer(text))
    chosen_addr = None
    # First pass: look for a Nueces-area city
    for m in all_addrs:
        city_upper = m.group(2).upper().strip()
        if any(c in city_upper for c in _NUECES_CITIES):
            chosen_addr = m
            break
    # Fallback: if nothing in Nueces, take the LAST address on the page
    # (foreclosure notices typically put the property address at the end).
    if chosen_addr is None and all_addrs:
        chosen_addr = all_addrs[-1]

    if chosen_addr:
        street = re.sub(r"\s+", " ",
                         chosen_addr.group(1)).upper().strip(" ,.")
        city = re.sub(r"[!?]", "I", chosen_addr.group(2)).upper().strip()
        # Normalize "CORPUS CHRIST" → "CORPUS CHRISTI"
        if city in ("CORPUS CHRIST", "CORPUSCHRISTI", "CORPUS"):
            city = "CORPUS CHRISTI"
        city = re.sub(r"\s+", " ", city)
        out["prop_address"] = street
        out["prop_city"] = city
        out["prop_state"] = "TX"
        out["prop_zip"] = chosen_addr.group(3)

    # --- Legal description ---
    # Try the parenthesized form first (LOT EIGHTEEN (18), BLOCK ONE (1)…),
    # then fall back to digit-only ("Lot 18, Block 1…").
    m = _RE_LEGAL_PARENS.search(text)
    if not m:
        m = _RE_LEGAL_DIGITS.search(text)
    if m:
        out["legal_lot"] = m.group(1).strip()
        out["legal_block"] = m.group(2).strip()
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
    m = re.search(r"\bLOT[S]?\s*[:\-]?\s*([\d,A-Z-]+)", s)
    if m:
        lot = m.group(1).strip(" ,-")

    block = ""
    m = re.search(r"\bBLOCK\s*[:\-]?\s*([\dA-Z-]+)", s)
    if m:
        block = m.group(1).strip(" ,-")
    else:
        m = re.search(r"\bBLK\.?\s*[:\-]?\s*([\dA-Z-]+)", s)
        if m:
            block = m.group(1).strip(" ,-")

    # Subdivision: strip the structural tokens (LOT, BLOCK, SUBDIVISION:, etc.)
    sub = re.sub(r"SUBDIVISION[\s\-]+NAME[\s:]*", " ", s)
    sub = re.sub(r"\bLOT[S]?\s*[:\-]?\s*[\d,A-Z-]+", " ", sub)
    sub = re.sub(r"\bBLOCK\s*[:\-]?\s*[\dA-Z-]+", " ", sub)
    sub = re.sub(r"\bBLK\.?\s*[:\-]?\s*[\dA-Z-]+", " ", sub)
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
