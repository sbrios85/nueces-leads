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
    """Extract all text from a PDF using pdfplumber.

    Returns the concatenated text of every page, or "" if extraction fails
    (e.g. the PDF is a scanned image with no embedded text).
    """
    try:
        import pdfplumber  # type: ignore
    except ImportError:
        log.error("pdfplumber not installed — cannot extract PDF text")
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
        log.warning("pdfplumber failed on %s: %s", pdf_path, exc)
        return ""


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
# these phrasings near the start:
#   "Deed of Trust ... executed by JOHN DOE AND JANE DOE"
#   "Deed of Trust executed by MELANIE SANDERS, AN UNMARRIED WOMAN ..."
#   "Mortgagor: JOHN DOE"
#   "Obligor: JOHN DOE"
#   "Grantor(s): JOHN DOE"
# We accept a stop at: a comma (preserves descriptors as a separate field),
# the word "and recorded/filed", "to <party>", "in favor of", "dated".
_RE_BORROWER = [
    re.compile(r"executed\s+by\s+([A-Z][A-Z\s&'.-]{3,80}?)"
                r"(?=\s*,|\s+(?:and\s+(?:recorded|filed)|to\s+\w+|"
                r"in\s+favor\s+of|dated))",
               re.IGNORECASE),
    re.compile(r"mortgagor[s]?(?:\(s\))?[\s:]+([A-Z][A-Z\s&'.-]{3,80}?)"
                r"(?=\s*,|\s+(?:to|in\s+favor|and|dated))",
               re.IGNORECASE),
    re.compile(r"obligor[s]?(?:\(s\))?[\s:]+([A-Z][A-Z\s&'.-]{3,80}?)"
                r"(?=\s*,|\s+(?:to|in\s+favor|and|dated))",
               re.IGNORECASE),
    re.compile(r"grantor[s]?(?:\(s\))?[\s:]+([A-Z][A-Z\s&'.-]{3,80}?)"
                r"(?=\s*,|\s+(?:to|in\s+favor|and|dated))",
               re.IGNORECASE),
    re.compile(r"debtor[s]?(?:\(s\))?[\s:]+([A-Z][A-Z\s&'.-]{3,80}?)"
                r"(?=\s*,|\s+(?:to|in\s+favor|and|dated))",
               re.IGNORECASE),
    re.compile(r"(?:property\s+of|record\s+owner)[s]?[\s:]+"
                r"([A-Z][A-Z\s&'.-]{3,80}?)(?=\s*,|\n)",
               re.IGNORECASE),
]

# Lender / mortgagee / beneficiary.
_RE_LENDER = [
    re.compile(r"in\s+favor\s+of\s+([A-Z][A-Za-z\s,&.'-]{3,80}?)"
                r"(?=\s*,\s*(?:its|a\s+\w+|as\s+|whose|located)|\.|\n|recorded)"),
    re.compile(r"lender[\s:]+([A-Z][A-Za-z\s,&.'-]{3,80}?)(?=\s*[,.\n])",
               re.IGNORECASE),
    re.compile(r"mortgagee[\s:]+([A-Z][A-Za-z\s,&.'-]{3,80}?)(?=\s*[,.\n])",
               re.IGNORECASE),
    re.compile(r"beneficiary[\s:]+([A-Z][A-Za-z\s,&.'-]{3,80}?)(?=\s*[,.\n])",
               re.IGNORECASE),
    # "current mortgagee" or "noteholder"
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

# Property street address — split into two stages to handle multi-line PDFs.
# Stage 1: number + street + suffix
_RE_STREET_LINE = re.compile(
    r"\b(\d{1,6})\s+([A-Z][A-Za-z0-9\s.,'#-]{3,60}?\b"
    r"(?:STREET|ST|AVENUE|AVE|ROAD|RD|DRIVE|DR|LANE|LN|"
    r"BOULEVARD|BLVD|COURT|CT|CIRCLE|CIR|PLACE|PL|"
    r"WAY|TRAIL|TR|PARKWAY|PKWY|HIGHWAY|HWY|TERRACE|TER)\b\.?)",
    re.IGNORECASE,
)
# Stage 2: city + TX + zip nearby
_RE_CITY_TX_ZIP = re.compile(
    r"\b(CORPUS\s+CHRISTI|ROBSTOWN|PORT\s+ARANSAS|BISHOP|DRISCOLL|"
    r"AGUA\s+DULCE|BANQUETE)\b[,\s]+(?:TX|TEXAS)\.?\s+(\d{5})",
    re.IGNORECASE,
)

# Legal description — Texas standard format (subdivision + lot + block).
_RE_LEGAL = re.compile(
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
            name = _clean_name(m.group(1))
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

    # --- Property street address (two-stage) ---
    street_match = _RE_STREET_LINE.search(text)
    if street_match:
        street = (street_match.group(1) + " " + street_match.group(2)).strip()
        # Look within 200 chars after the street for city/zip
        tail = text[street_match.end():street_match.end() + 300]
        cz = _RE_CITY_TX_ZIP.search(tail)
        out["prop_address"] = re.sub(r"\s+", " ", street).upper().strip(" ,.")
        out["prop_state"] = "TX"
        if cz:
            out["prop_city"] = re.sub(r"\s+", " ", cz.group(1)).upper()
            out["prop_zip"] = cz.group(2)

    # --- Legal description ---
    # Always extract (useful for cross-reference even if we have an address).
    m = _RE_LEGAL.search(text)
    if m:
        out["legal_lot"] = m.group(1).strip()
        out["legal_block"] = m.group(2).strip()
        out["legal_subdivision"] = _clean_name(m.group(3))

    return out


def _clean_name(s: str) -> str:
    """Trim whitespace and strip trailing punctuation/conjunctions."""
    if not s:
        return ""
    s = re.sub(r"\s+", " ", s).strip(" ,.;:&-")
    s = re.sub(r"\s+(and|to|in|the)\s*$", "", s, flags=re.IGNORECASE)
    return s.strip()


def _looks_like_lender(name: str) -> bool:
    """Crude heuristic: does this name look like a bank/financial institution
    rather than an individual? Used to reject false-positive borrower matches.
    """
    upper = name.upper()
    lender_tokens = ("BANK", "NA", "N.A.", "FEDERAL", "MORTGAGE", "FINANCIAL",
                     "CAPITAL", "TRUST COMPANY", "SAVINGS", "ASSOCIATION",
                     "FANNIE MAE", "FREDDIE MAC", "WELLS FARGO", "CHASE",
                     "CITIBANK", "BANK OF AMERICA")
    return any(tok in upper for tok in lender_tokens)


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
