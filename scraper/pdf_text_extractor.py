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
    # The clerk header line — unambiguous. Format:
    #   "2026 - 2026000183 03/26/2026 10:03 AM Page 1 of 3"
    # Some OCR runs prepend noise like ". ." or ". '" before the year,
    # so we allow any non-digit, non-newline characters before the
    # 4-digit year (instead of just whitespace).
    re.compile(r"^[^\d\n]*\d{4}\s*[-–]\s*(\d{10})\s+\d{1,2}/\d{1,2}/\d{2,4}",
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
    # 'executed by NAMES, ("Mortgagor")' / 'executed by NAMES
    # ("Borrower")' — Upton Mickits & Heymann template (248). The name
    # list ends at an optional comma then a parenthesised
    # "Mortgagor"/"Borrower"/"Grantor" role label. Allows multiple
    # names joined by AND/&/commas. Must come before the looser
    # "executed by" patterns so it wins on this phrasing.
    re.compile(r"executed\s+by\s+([A-Z][A-Z0-9\s,&'.\[\]()-]+?)"
                r"\s*,?\s*\(['\"\u2018\u2019\u201C\u201D]?"
                r"(?:Mortgagor|Borrower|Grantor)s?"
                r"['\"\u2018\u2019\u201C\u201D]?\)",
               re.IGNORECASE),
    # Two-column header table:  "Grantor(s)/Mortgagor(s):" on one line,
    # the borrower name on the FOLLOWING line — McCarthy & Holthus /
    # power-of-sale "table" template (254). The next line is often
    # prefixed with the Deed-of-Trust date ("6/20/2008 KAREN CHAVEZ"),
    # so skip an optional leading date. OCR frequently mangles the
    # name: a curly apostrophe (U+2019) replaces letters
    # ("CHAVEZ" -> "CHA'") and the marital descriptor glues on
    # ("ASINGLE WOMAN"). Capture letters/spaces plus straight AND
    # curly apostrophes, stopping before the descriptor (with optional
    # apostrophe/comma immediately before it) or end of line.
    re.compile(r"Grantor\(s\)\s*/?\s*Mortgagor\(s\)\s*:?[^\n]*\n"
                r"\s*(?:\d{1,2}/\d{1,2}/\d{2,4}\s+)?"
                r"([A-Z][A-Za-z.'\u2018\u2019\u201C\u201D\- ]+?)"
                r"[\u2018\u2019']?\s*"
                r"(?=(?:A\s*SINGLE|ASINGLE|AN?\s*UNMARRIED|"
                r"A\s*MARRIED|UNMARRIED|HUSBAND|WIFE|AND\s+(?:WIFE|"
                r"HUSBAND)|,|\n)|\s*$)",
               re.IGNORECASE | re.MULTILINE),
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
    # "Grantor(s): NAMES" — explicit labeled form. Requires a colon/
    # semicolon right after the label, then tolerates OCR junk before
    # the name: stray underscores, pipes, spaces (real scans produce
    # "Grantor(s):_ | NAME"). The name terminates at a newline OR a
    # marital descriptor ("Husband and Wife", "A SINGLE…", "UNMARRIED")
    # OR an OCR-merged location tail ("NUECES COUNTY") OR the next
    # labeled field. This handles three real OCR failure modes that
    # previously fell through to boilerplate:
    #   226: "Grantor(s): IRMA … SAENZ NUECES COUNTY, TEXAS"  (merged loc)
    #   230: "Grantor(s):_ | JOHN … GRAY, HUSBAND AND WIFE"   (_| junk)
    #   231: "Grantor(s): Englebert Devera …, Husband and Wife" (precedence)
    # Keeps the colon requirement so lowercase prose ("…as grantor(s)
    # and…") still can't match and over-capture.
    re.compile(r"\bGrantor\(?s?\)?\s*[:;]+[\s_|]*"
                r"([A-Z][a-zA-Z0-9\s,&'.\[\]()-]{4,120}?"
                r"(?:\s+A/?K/?A\s+[A-Z][a-zA-Z\s.]+?)?)"
                r"(?=\s*\n|"
                r"\s*,?\s*(?:HUSBAND\s+AND\s+WIFE|Husband\s+and\s+Wife|"
                r"husband\s+and\s+wife)|"
                r"\s+NUECES\s+COUNTY|"
                r"\s+Original\s+(?:Trustee|Mortgagee|Mortgage|Lender)|"
                r"\s+Current\s+(?:Mortgagee|Beneficiary)|"
                r"\s+Lender:|\s+Mortgage\s+Servicer:|"
                r"\s*,?\s*(?:A\s+SINGLE|AN?\s+UNMARRIED|UNMARRIED|SINGLE))",
                re.IGNORECASE),
    # "Grantor: NAME" (single-line, simpler form) — McAllen attorney template (266)
    re.compile(r"^\s*Grantor:\s+([A-Z][a-zA-Z\s.'-]{3,60}?)\s*$",
               re.MULTILINE),
    # Header-line form: "MM/DD/YYYY NAME, MORE_DESCRIPTORS,"
    # Used by McCarthy & Holthus table format. Stops at the comma
    # before descriptors like "AN UNMARRIED WOMAN", "HUSBAND AND
    # WIFE", "UNMARRIED MAN", "A SINGLE PERSON" etc. The bare forms
    # (UNMARRIED|SINGLE|MARRIED without preceding AN/A) handle OCR
    # that dropped the article.
    re.compile(r"^\s*\d{1,2}/\d{1,2}/\d{4}\s+"
                r"([A-Z][A-Z\s&'.\[\]()-]{4,80}?)"
                r"(?=,\s*(?:AN?\s+|"
                r"HUSBAND|WIFE|"
                r"A\s+(?:SINGLE|MARRIED)|"
                r"UNMARRIED|SINGLE|MARRIED)"
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

# Loan-document recording / instrument number — the doc number under
# which the ORIGINAL deed of trust was recorded with the county clerk.
# Sometimes labeled "Instrument No.", "Document No.", "Clerk's File
# No.", or "County Clerk's File No.".
#
# IMPORTANT: a foreclosure PDF often cites SEVERAL recording numbers:
# the original deed of trust (what we want), plus loan modifications
# and assignments ("further recorded ... in Instrument no. XXXX").
# To get the ORIGINAL, the anchored patterns below tie the number to
# the deed-of-trust recording clause specifically. The looser
# fallback is only used when no anchored match is found, and it takes
# the FIRST match (modifications/assignments are described later in
# the document, after the original).
#
# Modern Nueces numbers are 10 digits (YYYY######, e.g. 2012023281);
# older records use 5-7 digit clerk's file numbers (e.g. 944313). We
# accept 5-12 digits to cover both eras.
_RE_LOAN_DOC = [
    # Anchored: "Deed of Trust dated <date> and recorded in
    # Document/Instrument/Clerk's File No. <num>". The date portion is
    # skipped over with a non-greedy gap so we land on the recording
    # number that belongs to the deed of trust itself.
    re.compile(
        r"deed\s+of\s+trust\b[^.]{0,120}?"
        r"(?:recorded|filed|secorded|racorded)\b[^.]{0,60}?"
        r"(?:document|instrument|clerk(?:'|\u2019)?s?\s+file"
        r"|county\s+clerk(?:'|\u2019)?s?\s+(?:file|document))\s*"
        r"(?:no\.?|number|#)?\s*:?\s*"
        r"(\d{5,12})",
        re.IGNORECASE),
    # Table-template format (McAllen / Selene-style): the document is
    # laid out as labeled rows. The deed-of-trust recording number
    # sits in a "Recorded in: ... Instrument No: <num>" block that
    # follows a "Deed of Trust Date:" label. The gap between the two
    # labels can be ~400 chars of mortgagor/beneficiary text and may
    # contain periods (e.g. "INC.,"), so we use a wide,
    # period-tolerant but still bounded gap and require the
    # "Recorded in:" field label to immediately precede the number.
    re.compile(
        r"deed\s+of\s+trust\s+date\s*:.{0,600}?"
        r"recorded\s+in\s*:.{0,120}?"
        r"\binstrument\s+no\.?\s*:?\s*"
        r"(\d{5,12})",
        re.IGNORECASE),
    # Table field block: "Recorded in: ... Instrument No: <num>".
    # This is the recording-info field present in McAllen/RAS/Selene
    # table templates. Strong standalone signal — the only number that
    # follows this exact field label is the deed-of-trust recording
    # number. We DON'T require a nearby deed-of-trust-date label here
    # because some templates phrase the date differently; the
    # self-doc-number guard downstream still prevents grabbing the
    # notice's own 2026###### number.
    re.compile(
        r"recorded\s+in\s*:.{0,150}?"
        r"\binstrument\s+no\.?\s*:?\s*"
        r"(\d{5,12})",
        re.IGNORECASE),
    # "Recording Information: Instrument <num>" (no "No." token) —
    # First Community Bank / older RAS template.
    re.compile(
        r"recording\s+information\s*:?\s*"
        r"(?:document|instrument|clerk(?:'|\u2019)?s?\s+file)\s*"
        r"(?:no\.?|number|#)?\s*:?\s*"
        r"(\d{5,12})",
        re.IGNORECASE),
    # Looser fallback: the first "recorded/filed under
    # Document/Instrument/Clerk's File No. <num>" anywhere. First match
    # wins (original precedes modifications in the document body).
    re.compile(
        r"(?:recorded|filed|secorded|racorded)\b[^.]{0,40}?"
        r"(?:under|as|in|at)\s+"
        r"(?:document|instrument|clerk(?:'|\u2019)?s?\s+file"
        r"|county\s+clerk(?:'|\u2019)?s?\s+(?:file|document))\s*"
        r"(?:no\.?|number|#)?\s*:?\s*"
        r"(\d{5,12})",
        re.IGNORECASE),
]

# Loan-modification detection.
#
# A foreclosure PDF sometimes references the original deed of trust
# AND one or more subsequent loan modifications, e.g.:
#   "...recorded ... under County Clerk's File No 2014036856 ...
#    modified by Loan Modification recorded as Instrument no.
#    2018044065 ... and further recorded on 08/30/2022 in Instrument
#    no. 2022041020 ..."
#
# When a modification is present, the originally-scraped loan amount
# is usually stale (the mod changed the terms but the notice rarely
# restates the new balance). We flag these so the dashboard can show
# a "LOAN MOD" badge prompting the user to verify the amount by hand,
# and so the displayed "loan date" reflects the most-recent
# modification rather than the original deed-of-trust date.
#
# _RE_LOAN_MOD_PRESENT: does the document mention a loan modification
# at all? Deliberately specific phrases so unrelated uses of
# "modified" don't trigger a false positive.
_RE_LOAN_MOD_PRESENT = re.compile(
    r"(?:modified\s+by\s+(?:a\s+)?loan\s+modification"
    r"|loan\s+modification\s+(?:agreement\s+)?"
    r"(?:recorded|dated|filed)"
    r"|modified\s+by\s+(?:that\s+certain\s+)?modification"
    r"|(?:and\s+)?further\s+(?:recorded|modified)"
    r"|loan\s+modification\s+agreement)",
    re.IGNORECASE)

# _RE_MOD_DATE: dates attached to a modification event. We collect ALL
# matches and keep the most recent. Two flavors: an explicit
# "Loan Modification ... dated <date>" and the "further recorded on
# <date>" continuation form seen in the chained example above.
_RE_MOD_DATE = [
    re.compile(
        r"loan\s+modification[^.]{0,80}?"
        r"(?:dated|recorded\s+on|effective)\s+"
        r"(\d{1,2}/\d{1,2}/\d{2,4}|[A-Za-z]+\s+\d{1,2},?\s+\d{4})",
        re.IGNORECASE),
    re.compile(
        r"(?:and\s+)?further\s+recorded\s+on\s+"
        r"(\d{1,2}/\d{1,2}/\d{2,4}|[A-Za-z]+\s+\d{1,2},?\s+\d{4})",
        re.IGNORECASE),
    re.compile(
        r"modification[^.]{0,60}?"
        r"(?:dated|recorded\s+on|effective)\s+"
        r"(\d{1,2}/\d{1,2}/\d{2,4}|[A-Za-z]+\s+\d{1,2},?\s+\d{4})",
        re.IGNORECASE),
]

# _RE_MOD_DOC: instrument / document numbers attached to a loan
# modification. Real notices use several phrasings (all observed in
# Nueces FC notices):
#   "modified by Loan Modification recorded as Instrument no. 2018044065
#       and further recorded ... in Instrument no. 2022041020 ..."
#   "as modified by Modification of Deed of Trust dated ... recorded
#       under Instrument No. 2025021606"
#   "AS AFFECTED BY LOAN MODIFICATION AGREEMENTS INSTRUMENT NO(S)
#       2023013999, 2023043047 AND 2024019039"
#   "Loan Modification recorded as Instrument No. 2024033399 recorded
#       on ... and Loan modification recorded as Instrument No. 2024033400"
# Strategy: collect EVERY instrument number that sits in a modification
# context, then keep the most recent (numerically largest — Nueces
# instrument numbers are sequential by recording order, same basis we
# use for picking the newest mod date). A Nueces instrument number is
# a 9–12 digit run, often YYYY###### (e.g. 2024020964).
_RE_MOD_DOC_NUM = re.compile(r"\b(\d{9,12})\b")
# Anchor phrases that introduce a block of modification instrument
# numbers. We search WITHIN the matched span for the numbers so an
# unrelated instrument number elsewhere in the notice can't leak in.
_RE_MOD_DOC_BLOCK = [
    # "...modified by Loan Modification recorded as Instrument no. X
    #   and (further) recorded ... in Instrument no. Y ..." — captures
    #   the whole chained run up to the records clause.
    re.compile(
        r"modified\s+by\s+(?:that\s+certain\s+)?"
        r"(?:loan\s+)?modification.*?"
        r"(?:in\s+the\s+(?:real|official)\s+(?:property\s+)?"
        r"(?:public\s+)?records|\bP\b|$)",
        re.IGNORECASE | re.DOTALL),
    # "AS AFFECTED BY LOAN MODIFICATION AGREEMENT(S) INSTRUMENT NO(S)
    #   X, Y AND Z"
    re.compile(
        r"as\s+affected\s+by\s+loan\s+modification\s+agreement"
        r"s?\s+instrument\s+no\(?s?\)?\.?\s*"
        r"[\d,\s and]+",
        re.IGNORECASE),
    # "as modified by Modification of Deed of Trust dated ... recorded
    #   under Instrument No. X"
    re.compile(
        r"as\s+modified\s+by\s+modification\s+of\s+deed\s+of\s+"
        r"trust[^.]{0,120}?recorded\s+under\s+instrument\s+no\.?\s*"
        r"\d{9,12}",
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
    r"[.,\s]+(?:TX|TEXAS)[.,\s]+(\d{5})(?:-\d{4})?\)?",
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
    r"[.,\s]+([A-Z][A-Z\s!?]+?)[.,\s]+(?:TX|TEXAS)[.,\s]+(\d{5})(?:-\d{4})?",
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
    # LOT: word form with optional comma before the parenthesised digit
    # ("LOT NINE (9)" OR "LOT NINE, (9)") OR plain digit ("LOT 9").
    r"LOT[S]?\s+(?:[\w-]+(?:\s+[\w-]+)*?\s*,?\s*\((?P<lotw>\d+)\)"
    r"|(?P<lotd>\d+))[,\s]+"
    # BLOCK: same flexibility — word-paren OR plain digit. Notices mix
    # these freely ("LOT NINE, (9), BLOCK 1" -> doc 2026000296).
    r"(?:BLOCK|BLK\.?)\s+(?:[\w-]+(?:\s+[\w-]+)*?\s*,?\s*"
    r"\((?P<blkw>\d+)\)|(?P<blkd>\d+))[,\s]+"
    r"(?P<sub>[A-Z][A-Z0-9\s.&'-]+?)"
    # UNIT optional: word-paren ("UNIT SEVEN (7)") OR plain ("UNIT 1").
    r"(?:[,.]?\s+UNIT\s+(?:[\w-]+(?:\s+[\w-]+)*?\s*,?\s*"
    r"\((?P<unitw>\d+)\)|(?P<unitd>\d+)))?"
    r"\s*,?\s*(?:AN?\s+)?(?:SUBDIVISION|ADDITION|ACCORDING\s+TO"
    r"|CITY\s+OF|,)",
    re.IGNORECASE | re.DOTALL,
)

# Fallback legal description — digit-only form: "Lot 5, Block 12, BAY OAKS..."
# Tolerates "NAME, A SUBDIVISION" / "NAME ADDITION" / "NAME UNIT 3"
# (the optional ",? (a|an)?" before the keyword handles "GRANGE PARK,
# A SUBDIVISION" which the stricter form missed -> doc 2026000296).
_RE_LEGAL_DIGITS = re.compile(
    r"(?:Lot[s]?\s+)?(?P<lotd>[\d,A-Z-]+)[\s,]+(?:Block|Blk\.?)\s+"
    r"(?P<blkd>[\dA-Z-]+)[,\s]+(?:of\s+)?(?P<sub>[A-Z][A-Z\s\d&.'-]+?)"
    r"\s*,?\s*(?:an?\s+)?(?:Subdivision|Addition|Unit|Section|Phase)",
    re.IGNORECASE,
)


# --------------------------------------------------------------------------- #
# Date normalization
# --------------------------------------------------------------------------- #
# Foreclosure PDFs contain deed-of-trust dates in many formats:
#   "December 16, 2024", "12/16/2024", "12-16-2024", "16th of December, 2024"
# We capture whatever the PDF says verbatim, then run it through this
# normalizer so the stored value is always ISO ("2024-12-16"). The
# dashboard reformats for display; ISO sorts correctly, exports to CRMs
# cleanly, and survives round-trips through JSON without ambiguity.
#
# If the input can't be confidently parsed, we return the original
# string unchanged — better to keep imperfect data than to corrupt the
# record by guessing wrong about an ambiguous date like "01/02/2024"
# (Jan 2 vs Feb 1). All Texas legal documents use the US convention so
# we resolve MM/DD/YYYY without warning, but anything that fails to
# parse is preserved verbatim and the caller can log it.

_MONTH_NAMES = {
    "january":1, "february":2, "march":3, "april":4, "may":5, "june":6,
    "july":7, "august":8, "september":9, "october":10, "november":11,
    "december":12,
    "jan":1, "feb":2, "mar":3, "apr":4, "jun":6, "jul":7, "aug":8,
    "sept":9, "sep":9, "oct":10, "nov":11, "dec":12,
}

def normalize_date_string(raw: str) -> str:
    """Convert a free-form date string to ISO (YYYY-MM-DD).

    Returns the original string unchanged if parsing fails. Never
    raises. Examples that should succeed:
      "December 16, 2024"      → "2024-12-16"
      "12/16/2024"             → "2024-12-16"
      "12-16-2024"             → "2024-12-16"
      "16th of December, 2024" → "2024-12-16"
      "December 16th, 2024"    → "2024-12-16"
      " 5/8/2018 "             → "2018-05-08"
    Examples that should fail safely (return input unchanged):
      "TBD", "see attached", "13/45/2024" (impossible date)
    """
    if not raw or not isinstance(raw, str):
        return raw
    s = raw.strip()
    if not s:
        return raw

    # Already ISO? Validate and return as-is.
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1900 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31:
            return s
        return raw

    # Pattern 1: numeric M/D/YYYY or MM/DD/YYYY (US convention — all
    # Texas legal docs follow this). Also accept dashes as separators.
    m = re.fullmatch(r"(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})", s)
    if m:
        mo, d, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if y < 100:
            y += 2000 if y < 50 else 1900   # 2-digit year handling
        if 1900 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}-{d:02d}"
        return raw

    # Pattern 2: "Month DD, YYYY" or "Month DDth, YYYY" — the most
    # common form in foreclosure-notice prose. Strip ordinal suffixes
    # (st/nd/rd/th) so the day reads cleanly.
    m = re.fullmatch(
        r"([A-Za-z]+)\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})",
        s, re.IGNORECASE)
    if m:
        mo_name = m.group(1).lower().rstrip(".")
        d = int(m.group(2))
        y = int(m.group(3))
        mo = _MONTH_NAMES.get(mo_name)
        if mo and 1 <= d <= 31 and 1900 <= y <= 2100:
            return f"{y:04d}-{mo:02d}-{d:02d}"
        return raw

    # Pattern 3: "DDth of Month, YYYY" — occasionally seen in older
    # title-company templates ("the 16th of December, 2024").
    m = re.fullmatch(
        r"(\d{1,2})(?:st|nd|rd|th)?\s+(?:day\s+)?of\s+([A-Za-z]+),?\s+(\d{4})",
        s, re.IGNORECASE)
    if m:
        d = int(m.group(1))
        mo_name = m.group(2).lower()
        y = int(m.group(3))
        mo = _MONTH_NAMES.get(mo_name)
        if mo and 1 <= d <= 31 and 1900 <= y <= 2100:
            return f"{y:04d}-{mo:02d}-{d:02d}"
        return raw

    # Pattern 4: "DD Month YYYY" (no comma) — rare but seen.
    m = re.fullmatch(r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", s)
    if m:
        d = int(m.group(1))
        mo_name = m.group(2).lower()
        y = int(m.group(3))
        mo = _MONTH_NAMES.get(mo_name)
        if mo and 1 <= d <= 31 and 1900 <= y <= 2100:
            return f"{y:04d}-{mo:02d}-{d:02d}"
        return raw

    # Couldn't confidently parse — preserve original.
    return raw


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
    # NOTE: indices shifted +2 when the Upton-Mickits "executed by
    # NAMES (Mortgagor)" pattern and the McCarthy "Grantor(s)/
    # Mortgagor(s):" table pattern were prepended to _RE_BORROWER.
    # Old {3,4,7} -> {5,6,9}.
    ENTITY_BORROWER_PATTERN_INDICES = {5, 6, 9}  # Mortgagor / Grantor:LLC / Trustee-of-Trust
    for idx, rx in enumerate(_RE_BORROWER):
        m = rx.search(text)
        if m:
            name = _clean_name_ocr(m.group(1))
            # Strip trailing descriptors like ", AN UNMARRIED MAN" etc.
            # (but not for entity borrowers — they keep their suffix)
            if idx not in ENTITY_BORROWER_PATTERN_INDICES:
                name = _strip_borrower_descriptors(name)
            else:
                # Entity borrower: keep the LLC/LP/INC suffix but trim
                # trailing ", a Texas limited liability company" /
                # bare ", a" boilerplate OCR dragged in.
                name = _strip_entity_boilerplate(name)
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
            # Normalize to ISO (YYYY-MM-DD) so storage is uniform.
            # If parsing fails, normalize_date_string returns the input
            # unchanged — we keep imperfect data over wrong data.
            out["deed_date"] = normalize_date_string(d)
            break

    # --- Loan modification detection ---
    # Only act when the document explicitly references a loan
    # modification. When it does:
    #   * set has_loan_mod = True (drives the dashboard "LOAN MOD"
    #     badge + flags the scraped loan amount as needing manual
    #     verification)
    #   * if we can find modification date(s), replace the displayed
    #     deed_date with the MOST RECENT one (newest mod). The
    #     original deed date is still recoverable via loan_doc.
    # When there's no modification language, behavior is unchanged.
    flat_text = re.sub(r"\s+", " ", text)
    if _RE_LOAN_MOD_PRESENT.search(flat_text):
        out["has_loan_mod"] = True
        # Gather every modification date we can find; keep the latest.
        mod_iso_dates = []
        for rx in _RE_MOD_DATE:
            for mm in rx.finditer(flat_text):
                raw = re.sub(r"\s+", " ", mm.group(1)).strip()
                iso = normalize_date_string(raw)
                # Only keep values that normalized to real ISO dates
                # (YYYY-MM-DD). Unparseable strings are ignored for
                # the "most recent" comparison so a junk capture can't
                # win.
                if re.fullmatch(r"\d{4}-\d{2}-\d{2}", iso):
                    mod_iso_dates.append(iso)
        if mod_iso_dates:
            # Preserve the ORIGINAL deed-of-trust date before we swap
            # in the newest modification date, so the dashboard can
            # show "orig: <date>" as a reference line. Only set this
            # when we actually have an original to preserve AND a mod
            # date to replace it with — otherwise there's nothing to
            # compare and the field stays absent.
            orig = out.get("deed_date")
            newest = max(mod_iso_dates)  # ISO sorts == chronological
            if orig and orig != newest:
                out["deed_date_original"] = orig
            out["deed_date"] = newest

        # Modification instrument number(s). Find every number that
        # sits inside a modification-context block, keep the most
        # recent (numerically largest). Stays absent if none found.
        mod_doc_nums = []
        _own = str(out.get("doc_number") or "").strip()
        for brx in _RE_MOD_DOC_BLOCK:
            for bm in brx.finditer(flat_text):
                block = bm.group(0)
                for nm in _RE_MOD_DOC_NUM.finditer(block):
                    n = nm.group(1)
                    # Guard: never let the foreclosure notice's own
                    # doc number be mistaken for a modification doc.
                    if _own and n == _own:
                        continue
                    mod_doc_nums.append(n)
        if mod_doc_nums:
            # Largest = most recently recorded (Nueces instrument
            # numbers increase with recording order, same basis as
            # picking the newest mod date).
            out["loan_mod_doc"] = max(mod_doc_nums, key=lambda s: (len(s), s))

    # --- Loan document (deed-of-trust recording / instrument number) ---
    # We collapse whitespace first so OCR line breaks inside the
    # phrase ("recorded in\nDocument\n2012023281") don't defeat the
    # bounded-gap patterns. Try the deed-of-trust-anchored pattern
    # first; only fall back to the loose first-match pattern if the
    # anchored one misses. This keeps modifications/assignments from
    # being mistaken for the original loan instrument.
    #
    # Guard: the foreclosure notice has its OWN instrument/document
    # number (the 2026###### the clerk assigned when this notice was
    # filed). That number also appears in the text ("Document Number:
    # 2026000269 ... Record and Return To"). We must not capture it as
    # the loan doc. If a candidate equals the record's own doc_num, or
    # carries the current/next filing year prefix while a different
    # older-looking instrument number is also present, skip it.
    flat = re.sub(r"\s+", " ", text)
    own_doc = str(out.get("doc_num") or "").strip()
    for rx in _RE_LOAN_DOC:
        for m in rx.finditer(flat):
            cand = m.group(1).strip()
            # Reject the notice's own recording number.
            if own_doc and cand == own_doc:
                continue
            out["loan_doc"] = cand
            break
        if out.get("loan_doc"):
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
        gd = m.groupdict()
        lot = gd.get("lotw") or gd.get("lotd") or ""
        blk = gd.get("blkw") or gd.get("blkd") or ""
        out["legal_lot"] = lot.strip(" ,.")
        out["legal_block"] = blk.strip(" ,.")
        sub = _clean_name(gd.get("sub") or "")
        # Collapse newlines/whitespace and remove stray periods from
        # OCR-introduced line breaks within multi-word subdivision names
        # (e.g. "COUNTRY CLUB.\nESTATES" → "COUNTRY CLUB ESTATES").
        sub = re.sub(r"\s+", " ", sub).strip(" .,")
        sub = re.sub(r"\.\s+", " ", sub).strip()
        out["legal_subdivision"] = sub
        # UNIT is optional; only _RE_LEGAL_PARENS has unit groups
        # (word-form "UNIT SEVEN (7)" -> unitw, plain "UNIT 1" -> unitd).
        unit = gd.get("unitw") or gd.get("unitd")
        if unit:
            out["legal_unit"] = unit.strip()

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


# Trailing legal-entity boilerplate that OCR drags into entity borrower
# names: "Nextlots Now, LLC, a Texas limited [liability company]",
# "GRANADO ESTATES, L.P., a", "... LLC, a". We keep the entity name +
# its core suffix (LLC / L.P. / INC / CORP / LTD) but cut everything
# from the dangling ", a [Texas] [limited] [liability company /
# partnership]" onward, including a bare trailing ", a".
_ENTITY_TRAILING_BOILERPLATE = re.compile(
    r"(?P<core>.*?\b(?:L\.?L\.?C\.?|L\.?P\.?|INC\.?|CORP\.?|"
    r"LTD\.?|LIMITED\s+LIABILITY\s+COMPANY|LIMITED\s+PARTNERSHIP))"
    r"\s*,?\s*(?:a\b.*)?$",
    re.IGNORECASE | re.DOTALL,
)


def _strip_entity_boilerplate(s: str) -> str:
    """Trim trailing ', a Texas limited liability company/partnership'
    and bare ', a' boilerplate from entity borrower names while
    preserving the entity's core suffix (LLC, L.P., INC, etc.).
    Returns the original if the trim would leave nothing useful."""
    if not s:
        return s
    m = _ENTITY_TRAILING_BOILERPLATE.match(s.strip())
    if not m:
        return s.strip(" ,;:&-")
    core = m.group("core").strip(" ,;:&-")
    # The core regex may end right at the suffix and the .strip() above
    # could have eaten a legitimate trailing period (L.P. -> L.P).
    # Restore it only if the matched core in the ORIGINAL text was
    # immediately followed by a period — never invent one (LLC stays
    # LLC, not "LLC.").
    end = m.end("core")
    src = s.strip()
    if (not core.endswith(".") and end < len(src)
            and src[end:end + 1] == "."):
        core = core + "."
    return core if len(core) >= 3 else s.strip(" ,;:&-")


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
