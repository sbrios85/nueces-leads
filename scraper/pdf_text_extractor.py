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
    # "executed by NAME (without comma)" — fallback for Mackie Wolf.
    # Stops at "dated|to|in favor of|and (recorded|filed|payable)|
    # secures|securing" so templates like Robertson Anschutz ("executed
    # by NAMES and payable to the order of Lender"), Tromberg/De Cubas
    # ("executed by NAMES secures the repayment of a Note"), and Power
    # Default ("executed by) NAMES, securing the payment") all stop
    # cleanly. The `[\s)]+` after "executed by" tolerates OCR garbage
    # like a misplaced `)`. The char class allows commas and colons
    # so OCR slop like "NAME, : NAME" doesn't break the match.
    re.compile(r"executed\s+by[\s)]+([A-Z][A-Z0-9\s,&'.\[\]():-]+?)"
                r"(?=\s+(?:dated|to\s+\w+|in\s+favor\s+of|"
                r"and\s+(?:recorded|filed|payable)|"
                r"secures?\s+|securing\s+)|"
                r"\s*,\s*securing\s+)",
               re.IGNORECASE),
    # 'NAME ("Borrower"), executed and delivered' — Schmitt template
    re.compile(r"([A-Z][a-zA-Z\s&'.\[\]()-]{4,80}?)\s*"
                r"\(['\"]?Borrower['\"]?\)"),
    # 'ENTITY (hereinafter called the "Mortgagor")' — Plutus / Diann Bartek
    # template. The borrower is often an LLC or trust. Anchor to
    # "purposes," which precedes the borrower name in this template.
    # Allow curly quotes and a newline between "the" and "Mortgagor".
    re.compile(r"purposes,?\s+([A-Z][\w\s.,&'-]+?)\s+"
                r"\(hereinafter\s+called\s+the[\s\n]+"
                r"[\"'\u2018\u2019\u201C\u201D]?Mortgagor",
               re.IGNORECASE),
    # "Grantor: ENTITY, LLC, A ... LIABILITY COMPANY" — Jack O'Boyle
    # template (270). Captures the entity name + corporate suffix.
    re.compile(r"\bGrantor:\s+([A-Z][\w\s,.&'-]+?"
                r"(?:LIMITED\s+LIABILITY\s+COMPANY|LLC|INC|LP|CORP\.?))",
               re.IGNORECASE),
    # "Deed conveying title into NAME ("Obligor")" — Mark Gilbreath HOA
    # template (294).
    re.compile(r"\bDeed\s+conveying\s+title\s+into\s+"
                r"([A-Z][a-zA-Z\s.'-]+?)\s+"
                r"\([\"\u201C]?Obligor[\"\u201D]?\)",
               re.IGNORECASE),
    # "conveying the property described below to NAME" — Steptoe & Johnson
    # HOA template (295).
    re.compile(r"\bconveying\s+the\s+property\s+described\s+below\s+to\s+"
                r"([A-Z][a-zA-Z\s.'-]+?)[;,]"),
    # 'NAME, Trustee of ENTITY TRUST' — HOA assessment template (267)
    # Captures both the individual trustee and the trust entity name.
    re.compile(r"\bWHEREAS,\s+([A-Z][A-Z\s.]{3,40}?),?\s+Trustee\s+of\s+"
                r"([A-Z][A-Z\s\d.]+?\bTRUST\b)",
               re.IGNORECASE),
    # "WHEREAS, on DATE, BORROWERS, as Grantor(s)" — Robertson Anschutz
    # template (e.g. 291646802 RODRIGUEZ). Stops at the first comma
    # after the all-caps name list so "HUSBAND AND WIFE, WITH HER
    # JOINING HEREIN..." doesn't get appended.
    re.compile(r"WHEREAS,?\s+on\s+\w+\s+\d+,?\s+\d{4},?\s+"
                r"([A-Z][A-Z\s.&']+?)"
                r"(?=,\s+(?:as\s+Grantor|HUSBAND\s+AND\s+WIFE|"
                r"WIFE\s+AND\s+HUSBAND|AN?\s+(?:SINGLE|UNMARRIED|"
                r"MARRIED)|A\s+SINGLE\s+(?:MAN|WOMAN|PERSON)))",
               re.IGNORECASE),
    # "WHEREAS, NAMES, executed and delivered to" — Avots template (260)
    re.compile(r"WHEREAS,\s+([A-Z][A-Z\s.,&'-]+?)"
                r"[,\s]+executed\s+and\s+delivered\s+to",
               re.IGNORECASE),
    # "NAMES conveyed to <trustee>" — Arnold Gonzales template (261)
    re.compile(r"\b([A-Z][A-Z\s,&'.-]{4,100}?)\s+conveyed\s+to\s+\w",
               re.IGNORECASE),
    # "NAMES, as Grantor(s)" — Hughes Watters / SPS template (262)
    re.compile(r"\b([A-Z][A-Z\s.,&'-]{4,100}?)[,\s]+as\s+Grantor\(?s?\)?",
               re.IGNORECASE),
    # "executed by NAMES dated ..." — Schlanger / SCF Jake LP template (291)
    # Catches "executed by STEPHEN M. GARRETT and DENISE GARRETT dated"
    re.compile(r"\bexecuted\s+by\s+"
                r"([A-Z][A-Z0-9\s&'.,-]+?)\s+dated\s+"
                r"[A-Z][a-z]+\s+\d{1,2},?\s+\d{4}",
               re.IGNORECASE),
    # "executed by NAMES, provides" — Mackie Wolf template (292)
    # MUST come BEFORE the generic "Original Mortgagor/Grantor:" so we
    # match the borrower in the Mackie Wolf paragraph.
    re.compile(r"\bexecuted\s+by\s+"
                r"([A-Z][A-Z\s,&'.-]+?),\s+provides",
               re.IGNORECASE),
    # "Original Mortgagor/Grantor: NAMES" — Robertson Anschutz template (269)
    # Allow newlines inside the name (OCR sometimes wraps long names).
    re.compile(r"Original\s+Mortgagor/Grantor:\s+"
                r"([A-Z][A-Z\s.,\-]+?)"
                r"(?=\s*(?:Original|Current)\s+(?:Beneficiary|Mortgagee)|"
                r"\s*Recorded\s+in)",
               re.IGNORECASE),
    # "Grantor(s): NAMES" — explicit labeled form. Requires a colon
    # (or pipe from OCR garbage like "Grantor(s): | NAME") right after
    # the label, to avoid matching lowercase prose like "...as
    # grantor(s) and..." which would otherwise capture trailing junk.
    re.compile(r"\bGrantor\(?s?\)?\s*[:;|]+\s*\|?\s*"
                r"([A-Z][a-zA-Z0-9\s,&'.\[\]()-]{4,120}?)"
                r"(?=\s*\n|\s+Original\s+(?:Trustee|Mortgagee|Lender)|"
                r"\s+Current\s+(?:Mortgagee|Beneficiary)|"
                r"\s+Lender:|\s+Mortgage\s+Servicer:)"),
    # "Grantor: NAME" (single-line, simpler form) — McAllen attorney template (266)
    re.compile(r"^\s*Grantor:\s+([A-Z][a-zA-Z\s.'-]{3,60}?)\s*$",
               re.MULTILINE),
    # Header-line form: "MM/DD/YYYY NAME, MORE_DESCRIPTORS,"
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
    # "WHEREAS, NAME Association, Inc. (the "Association")" — HOA
    # assessment foreclosure template (267 Keller). Handles both straight
    # and curly quotes/apostrophes from OCR.
    re.compile(r"(?:^|\n)\s*WHEREAS,\s+"
                r"([A-Z][\w\s.&,'\u2019-]+?Association,?\s+Inc\.?)\s+"
                r"\(the\s+[\u201C\"']?Association[\u201D\"']?\)",
               re.IGNORECASE),
    # "The NAME Council of Co-Owners, Inc. (the "Council")" — Sand Dollar
    # II HOA template (294 Mark Gilbreath).
    re.compile(r"^The\s+([A-Z][\w\s.&,'\u2019-]+?Council\s+of\s+Co-Owners,?"
                r"\s+Inc\.?)\s+\(the\s+[\u201C\"']?Council[\u201D\"']?\)",
               re.MULTILINE | re.IGNORECASE),
    # "in favor of NAME Council of Co-Owners" — Leeward Isles HOA
    # template (295 Steptoe & Johnson).
    re.compile(r"in\s+favor\s+of\s+"
                r"([A-Z][\w\s.&,'\u2019-]+?Council\s+of\s+Co-Owners)",
               re.IGNORECASE),
    # "WHEREAS, NAME, a TEXAS limited partnership, is the legal owner"
    # — Schlanger / SCF Jake LP template (291)
    re.compile(r"WHEREAS,\s+"
                r"([A-Z][\w\s.,&'-]+?(?:LP|L\.?P\.?|LLC|INC|CORP)\.?,?\s+"
                r"a\s+\w+\s+"
                r"(?:limited\s+partnership|limited\s+liability\s+company|"
                r"company|corporation))",
               re.IGNORECASE),
    # "Lender: NAME" — Jack O'Boyle template (270) Closing Capital
    re.compile(r"^\s*Lender:\s+([A-Z][\w\s.,&'-]+?)\s*$",
               re.MULTILINE),
    # 'for the benefit of NAME ("Lender")' — Schmitt template
    re.compile(r"for\s+the\s+benefit\s+of\s+([A-Z][A-Za-z0-9\s,&.'-]+?)"
                r"\s*\(['\"]?Lender['\"]?\)"),
    # 'NAME (hereinafter called "Beneficiary")' — Plutus / Diann Bartek
    # template. Anchor STRICTLY to start of line via MULTILINE so we don't
    # pick up trailing words from the prior sentence ("Deed of Trust.\nSimmons Bank").
    # Strict char class excludes the period to refuse "Deed of Trust." prefix.
    # Supports both straight and curly quotes for "Beneficiary".
    re.compile(r"^([A-Z][a-zA-Z0-9\s&,'-]{2,40}?)\s+"
                r"\(hereinafter\s+called\s+"
                r"[\"'\u2018\u2019\u201C\u201D]?Beneficiary",
               re.MULTILINE | re.IGNORECASE),
    # "Because of that default, NAME, the owner and holder of the Note"
    # — Robertson Anschutz template (269)
    re.compile(r"Because\s+of\s+that\s+default,\s+"
                r"([A-Z][\w\s,&.'-]{3,60}?),\s+the\s*\n?\s*"
                r"owner\s+and\s+holder\s+of\s+the\s+Note",
               re.IGNORECASE),
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
    # "Deed of Trust\nDate: MM/DD/YYYY" — table format, McAllen
    # attorney template (266).
    re.compile(r"deed\s+of\s+trust\s*\n+\s*Date:\s+"
                r"([A-Za-z]+\s+\d{1,2},?\s+\d{4})",
               re.IGNORECASE),
    # "Deed of Trust Date: MM/DD/YYYY" — Robertson Anschutz (269)
    re.compile(r"deed\s+of\s+trust\s+Date:\s+"
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
    r"\(?\s*(?:(?:more\s+)?commonly\s+known\s+as|Property\s+Address|"
    r"Property\s+is\s+located\s+at|Reported\s+Address|Address)"
    r"[\s:;]+"
    r"(\d{1,5}[A-Z]?\s+[A-Z][A-Za-z0-9\s.]{2,60}?\b"
    r"(?:STREET|ST|AVENUE|AVE|ROAD|RD|DRIVE|DR|LANE|LN|"
    r"BOULEVARD|BLVD|COURT|CT|CIRCLE|CIR|PLACE|PL|"
    r"WAY|TRAIL|TR|PARKWAY|PKWY|LOOP|HIGHWAY|HWY|TERRACE|TER)\.?"
    r"(?:\s+\d+|,?\s+(?:Unit|Apt|Suite|Ste|#)\s*\d+)?)"
    r"[.,\s]+([A-Z][a-zA-Z\s!?]+?)"
    r"(?:[.,]\s+Nueces\s+County)?"
    r"[.,\s]+(?:TX|TEXAS)[.\s]+(\d{5})\)?",
    re.IGNORECASE,
)

# Strategy 1b: header-style address — many BDF/Barrett Daffin templates
# put the address at the very top of page 1 in this format:
#   "1737 GALLOP TRAIL                    00000010585446"
#   "CORPUS CHRISTI, TX 78410"
# The street and city are on separate lines, with a 6+ digit loan
# number between them on the street line. There's no comma between
# street and city. Plain _RE_FULL_ADDRESS won't match this.
_RE_HEADER_ADDRESS = re.compile(
    r"^\s*(\d{1,5}[A-Z]?\s+[A-Z][A-Z0-9 .'-]{2,60}?\b"
    r"(?:STREET|ST|AVENUE|AVE|ROAD|RD|DRIVE|DR|LANE|LN|"
    r"BOULEVARD|BLVD|COURT|CT|CIRCLE|CIR|PLACE|PL|"
    r"WAY|TRAIL|TR|PARKWAY|PKWY|LOOP|HIGHWAY|HWY|TERRACE|TER)\.?)"
    r"(?:\s+(?:UNIT|APT|SUITE|STE|#)\s*[A-Z0-9-]+)?"
    r"\s+\d{6,}\s*\r?\n+\s*"
    r"([A-Z][A-Z\s!?]+?)[,\s]+(?:TX|TEXAS)\s+(\d{5})",
    re.IGNORECASE | re.MULTILINE,
)

# Strategy 1c: bare-line address — some templates have a free-floating
# line "STREET, CORPUS CHRISTI, TX ZIP" near the top with no label
# (e.g. Tromberg/De Cubas). _RE_FULL_ADDRESS would match this too, but
# this pattern is anchored to start-of-line which lets us prioritize it
# over any street that appears elsewhere in the document body.
_RE_LINE_ADDRESS = re.compile(
    r"^\s*(\d{1,5}[A-Z]?\s+[A-Z][A-Z0-9 .'-]{2,60}?\b"
    r"(?:STREET|ST|AVENUE|AVE|ROAD|RD|DRIVE|DR|LANE|LN|"
    r"BOULEVARD|BLVD|COURT|CT|CIRCLE|CIR|PLACE|PL|"
    r"WAY|TRAIL|TR|PARKWAY|PKWY|LOOP|HIGHWAY|HWY|TERRACE|TER)\.?)"
    r"\s*,\s*([A-Z][A-Z\s!?]+?)\s*,\s*(?:TX|TEXAS)\s+(\d{5})",
    re.IGNORECASE | re.MULTILINE,
)

# Strategy 2: full address pattern — number + street + suffix + city + TX + zip
# in one shot. Allows OCR slop in city names.
_RE_FULL_ADDRESS = re.compile(
    r"(\d{1,5}[A-Z]?\s+[A-Z][A-Z0-9\s.]{3,60}?\b"
    r"(?:STREET|ST|AVENUE|AVE|ROAD|RD|DRIVE|DR|LANE|LN|"
    r"BOULEVARD|BLVD|COURT|CT|CIRCLE|CIR|PLACE|PL|"
    r"WAY|TRAIL|TR|PARKWAY|PKWY|LOOP|HIGHWAY|HWY|TERRACE|TER)\.?)"
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
    # Some patterns explicitly capture entity borrowers (LLCs, trusts).
    # For those we skip the "_looks_like_lender" check, since the entity
    # is legitimately the borrower (e.g. Plutus Properties, LLC, or the
    # Adam Keller 2016 Trust).
    ENTITY_BORROWER_PATTERN_INDICES = {3, 4, 7}  # Mortgagor / Grantor:LLC / Trustee-of-Trust
    for idx, rx in enumerate(_RE_BORROWER):
        m = rx.search(text)
        if m:
            name = _clean_name_ocr(m.group(1))
            # Strip trailing descriptors like ", AN UNMARRIED MAN" etc.
            # (but not for entity borrowers — they keep their suffix)
            if idx not in ENTITY_BORROWER_PATTERN_INDICES:
                name = _strip_borrower_descriptors(name)
            if not name or len(name) < 4:
                continue
            if (idx not in ENTITY_BORROWER_PATTERN_INDICES
                    and _looks_like_lender(name)):
                continue
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

    # First try: explicit label like "Property Address:" or "Commonly
    # known as:" or "Reported Address:" or "(Address: ...)". These are
    # unambiguous when present. Still verify the city is in Nueces
    # County — labels sometimes refer to the trustee's mailing address.
    m_lbl = _RE_LABELED_ADDRESS.search(text)
    if m_lbl:
        labeled_city = m_lbl.group(2).upper().strip()
        if any(c in labeled_city for c in _NUECES_CITIES):
            chosen_street = m_lbl.group(1)
            chosen_city = m_lbl.group(2)
            chosen_zip = m_lbl.group(3)

    # Second try: header-style — address at top of page with the loan
    # number on the street line and the city on the next line. Common
    # in Barrett Daffin / BDF templates. Anchored to ^ via MULTILINE so
    # this only matches start-of-line, where headers live.
    if not chosen_street:
        m_hdr = _RE_HEADER_ADDRESS.search(text)
        if m_hdr:
            hdr_city = m_hdr.group(2).upper().strip()
            if any(c in hdr_city for c in _NUECES_CITIES):
                chosen_street = m_hdr.group(1)
                chosen_city = m_hdr.group(2)
                chosen_zip = m_hdr.group(3)

    # Third try: bare-line address — free-floating "STREET, CITY, TX
    # ZIP" line near the top of the doc (Tromberg/De Cubas templates).
    # Higher priority than the generic body-scan since these address
    # lines tend to be the property address.
    if not chosen_street:
        m_line = _RE_LINE_ADDRESS.search(text)
        if m_line:
            line_city = m_line.group(2).upper().strip()
            if any(c in line_city for c in _NUECES_CITIES):
                chosen_street = m_line.group(1)
                chosen_city = m_line.group(2)
                chosen_zip = m_line.group(3)

    # Fourth try: scan all addresses, accept ONLY those in Nueces-area cities.
    # We do NOT fall back to any other city — the property is by definition
    # in Nueces County, and any non-Nueces address found in the PDF is
    # a law-firm/trustee/courthouse address that must NOT be picked up.
    if not chosen_street:
        all_addrs = list(_RE_FULL_ADDRESS.finditer(text))
        for m in all_addrs:
            street_upper = re.sub(r"\s+", " ",
                                    m.group(1)).upper().strip(" ,.")
            if any(bl in street_upper for bl in _BLACKLIST_ADDRESSES):
                continue
            city_upper = m.group(2).upper().strip()
            if any(c in city_upper for c in _NUECES_CITIES):
                chosen_street = m.group(1)
                chosen_city = m.group(2)
                chosen_zip = m.group(3)
                break

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


# Suffix descriptors that frequently follow a borrower's name in
# foreclosure notices but aren't part of the name itself. The match is
# case-insensitive and anchored to a trailing comma + descriptor pattern
# so we don't accidentally strip parts of the actual name.
_BORROWER_DESCRIPTOR_SUFFIXES = re.compile(
    r"[,\s]+(?:"
    r"an?\s+unmarried\s+(?:man|woman|person)|"
    r"an?\s+(?:single|married)\s+(?:man|woman|person)|"
    r"unmarried\s+(?:man|woman|person)|"
    r"husband\s+and\s+wife|wife\s+and\s+husband|"
    r"his\s+wife|her\s+husband|a\s+married\s+couple|"
    r"as\s+community\s+property|as\s+(?:tenants|joint\s+tenants)|"
    r"a\s+single\s+(?:man|woman|person)|"
    r"an?\s+unmarried\s+(?:man|woman)"
    r")\s*$",
    re.IGNORECASE,
)


def _strip_borrower_descriptors(s: str) -> str:
    """Strip trailing descriptors like ", AN UNMARRIED MAN", ",
    HUSBAND AND WIFE", ", A SINGLE PERSON", ", AS COMMUNITY PROPERTY"
    that some templates append to the borrower name. Applied
    iteratively in case there are stacked descriptors. Returns the
    cleaned name; if everything got stripped, returns the original.
    """
    if not s:
        return s
    original = s
    for _ in range(3):  # at most a few iterations
        new_s = _BORROWER_DESCRIPTOR_SUFFIXES.sub("", s).strip(" ,.;:&-")
        if new_s == s:
            break
        s = new_s
    return s if len(s) >= 3 else original


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
