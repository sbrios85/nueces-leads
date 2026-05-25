"""
CCLN PDF extractor — pulls structured fields out of City of Corpus
Christi lien-affidavit PDFs and writes them onto the matching CCLN
records in ``data/city_liens.json``.

================================================================
WHY THIS EXISTS
================================================================
The Nueces County clerk portal exposes CCLN records via a list-view
API that gives us: doc_num, filed date, owner name (the grantee), and
the legal description. It does NOT give us the property address, the
mailing address, the NCAD account number, the lien reason, or the
work-completion date.

Phase 1 enrichment (legal-text regex) rarely works on CCLN because
the legal description on these records is just subdivision + lot/block
("BALDWIN PARK BLK 5 LOT 6") with no street address embedded.

Phase 2 enrichment (NCAD esearch by owner name) usually works, but
some owners can't be resolved with confidence (corroboration guard
rejects mismatches; ambiguous names like "JOHN GARCIA" hit dozens of
parcels).

The PDF the city files alongside the clerk record has EVERY field we
need printed at fixed positions on page 1. The CCLN affidavit
template is uniform — same form, same signatory (Tracey K Cantu),
same field positions every time. That makes OCR + regex extraction
reliable in a way the MFC pipeline (with its variable law-firm-
formatted PDFs) is not.

================================================================
WORKFLOW
================================================================
1. User downloads PDFs from the clerk portal (50 at a time per
   portal limit), uploads them to ``pdfs/ccln_pending/`` in this
   repo.
2. User clicks "Run workflow" on the GitHub Actions extraction job.
3. The job calls this module, which:
   a. For each PDF in ``pdfs/ccln_pending/``:
      - Try to match to a CCLN record by filename
        (e.g. ``2025027139.pdf`` → doc_num 2025027139).
      - If filename match fails, rasterize page 1, run OCR, parse
        the header bar for the doc_num.
      - If we still can't match, log and skip the PDF.
      - For matched PDFs: rasterize page 1 (if not already), OCR
        if not already done, then apply field extractors.
      - Run validation flags on each extracted field.
      - Write extracted fields + flags onto the JSON record.
      - Delete the PDF from ``pdfs/ccln_pending/``.
   b. Save the updated ``city_liens.json`` and mirror to
      ``dashboard/city_liens.json`` for Pages.
4. The workflow commits the changes (JSON updates + PDF deletions).

================================================================
CONFLICT RESOLUTION
================================================================
On conflicts between PDF-extracted data and the existing
clerk-portal data (e.g. PDF amount differs from clerk amount), the
clerk-portal data WINS — it's the system of record. We just flag the
mismatch so the user can spot-check. Reason: a typo in the PDF
shouldn't silently overwrite the canonical filed value.

================================================================
FLAGS
================================================================
Each record gets a ``ccln_pdf_flags`` list with any of these strings:

- ``bad_zip_format``       — extracted ZIP isn't 5 digits
- ``zip_outside_cc``       — ZIP doesn't start with 784xx (CC range)
- ``bad_ncad_account``     — account # doesn't match \\d{4}-\\d{4}-\\d{4}
- ``amount_mismatch``      — PDF amount differs from clerk amount
- ``owner_mismatch``       — PDF owner doesn't share last name with clerk owner
- ``bad_work_date``        — work date invalid or in the future
- ``incomplete_extraction``— one or more required fields missing
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import traceback
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Owner-classifier module — used to detect corporate owners on PDFs.
# Lives next to this file in scraper/. The classifier returns
# ("company"/"hoa"/etc, keep=False) for entities we want to filter
# out, ("individual"/"estate"/"family_trust", keep=True) for leads
# we want to retain. See ccln_owner_filter.py for the full taxonomy.
import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent))
from ccln_owner_filter import classify_owner, kind_label  # noqa: E402


# ----------------------------------------------------------------
# Logging setup
# ----------------------------------------------------------------
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("ccln-pdf-extractor")


# ----------------------------------------------------------------
# Paths
# ----------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent.parent
PENDING_DIR = REPO_ROOT / "pdfs" / "ccln_pending"
DATA_FILE = REPO_ROOT / "data" / "city_liens.json"
DASHBOARD_FILE = REPO_ROOT / "dashboard" / "city_liens.json"

# Temp directory for rasterized pages — under /tmp on the runner so it
# doesn't get committed accidentally.
TMP_DIR = Path("/tmp/ccln_pdf_work")


# ----------------------------------------------------------------
# Field-extraction regexes
# ----------------------------------------------------------------
# Each regex is anchored to invariant template text. Tested against
# real OCR output of a sample CCLN PDF before being added here.
# If a future PDF's OCR differs (different scan quality, slightly
# different OCR errors), the patterns should still match — they
# tolerate whitespace variations, optional punctuation, and case.

# Header bar: "2025 - 2025027139 07/30/2025 12:37 PM Page 1 of 2"
RE_HEADER = re.compile(
    r"^\s*\d{4}\s*-\s*(?P<doc>\d{10})\s+(?P<filed>\d{1,2}/\d{1,2}/\d{4})",
    re.M,
)

# Internal lien id: "AFFIDAVIT OF LIEN D70611"
RE_INTERNAL_ID = re.compile(
    r"AFFIDAVIT\s+OF\s+LIEN\s+(?P<id>[A-Z]\d+)",
    re.I,
)

# NCAD account: "Account No. 0386-0005-0060"
# Three OCR variabilities to tolerate:
#   1. "No." can OCR as "No," (period misread as comma)
#   2. Account # sometimes wraps across line breaks:
#        "Account No. 0481-\n1601-0380"
#        "Account No. 3515-\n0012-0040"
#   3. Extra punctuation appears mid-number from scan artifacts:
#        "Account No. 0481- .\n1601-0380"  (the city sometimes
#         prints with extra dots between segments when the number
#         wraps; OCR captures those)
# After matching, the caller strips all whitespace/dots to recover
# the clean "NNNN-NNNN-NNNN" form.
RE_NCAD_ACCT = re.compile(
    r"Account\s+No[.,]?\s+"
    r"(?P<acct>\d{4}\s*-?\s*[.\s]*\s*\d{4}\s*-?\s*[.\s]*\s*\d{4})",
    re.I,
)

# Legal description: anchored to a lot/block pattern after an
# ALL-CAPS subdivision name. CCLN affidavits use multiple syntaxes:
#   "BALDWIN PARK BLK 5 LOT 6"        — BLK first, LOT second
#   "MEADOW PARK BLK 8 LOT 6"          — same
#   "BOOTY AND ALLEN LT 15 BK 3"      — LT first, BK second (abbreviated)
#   "NUECES ACRES 2 LT 10 BK 3"       — same, with subdivision suffix number
# The regex accepts all four orderings: BLK..LOT, LOT..BLK, LT..BK, BK..LT.
# Stops at the comma that begins the trailing ", and further described..."
# clause to avoid swallowing the boilerplate.
RE_LEGAL = re.compile(
    r"^(?P<legal>[A-Z][A-Z0-9 ]+?(?:"
    r"BLK\s+\d+\s+LOT\s+\d+(?:[,\s]+\d+)*"
    r"|LOT\s+\d+(?:[,\s]+\d+)*\s+BLK\s+\d+"
    r"|LT\s+\d+(?:[,\s]+\d+)*\s+BK\s+\d+"
    r"|BK\s+\d+\s+LT\s+\d+(?:[,\s]+\d+)*"
    r"))",
    re.M,
)

# Property street: extracted from end of the legal/account line.
# Format: "{LEGAL}, and further described in whole or in part by
# Account No. {ACCOUNT}. {STREET}"
# OCR variabilities to handle:
#   1. "No." vs "No," after "Account" (period misread as comma).
#   2. Separator after account # can be "." OR "," .
#   3. Account # itself can wrap across line breaks with extra
#      punctuation (see RE_NCAD_ACCT comment for examples).
#   4. STREET may wrap to the next line when long:
#        "Account No. 0867-0003-0150, 1529 7TH\nST"
#        "Account No. 5000-0008-0060. 121 PUEBLO\nAVE"
#   5. STREET on the line AFTER the wrapped account #:
#        "Account No. 0481-\n1601-0380. 1414 S 19TH ST"
#   6. STREET continuation line sometimes starts with a digit when
#      OCR mis-prepends a stray character or when the address itself
#      begins the continuation with a number (numbered streets):
#        "Account No. 0481-1401-0630. 1462\n417TH ST"
#        (truth here is "1462 17TH ST" — OCR added a stray "4" but
#         we still want to capture the full line as a single street.)
#      The continuation pattern accepts [A-Z0-9] as the first char
#      and tightly limits length to avoid swallowing the next
#      paragraph ("That said work was completed..."). Length cap is
#      15 chars which covers street suffixes (ST/AVE/PKWY/FRONTAGE
#      RD/etc.) plus a short street name.
RE_PROP_STREET = re.compile(
    r"Account\s+No[.,]?\s+"
    r"\d{4}\s*-?\s*[.\s]*\s*\d{4}\s*-?\s*[.\s]*\s*\d{4}"
    r"[.,]?\s+"
    r"(?P<street>[A-Z0-9][^\n]+(?:\n[A-Z0-9][A-Z0-9 ]{1,15})?)\s*(?:\n|$)",
    re.I,
)

# Street-suffix tokens — used by the address-quality validator to
# decide if an extracted street looks like a real address.
# (We don't reject these from the extraction itself, just flag.)
STREET_SUFFIX_TOKENS = {
    "ST", "AVE", "DR", "BLVD", "RD", "LN", "CT", "PL", "CIR",
    "WAY", "PKWY", "TER", "TRL", "HWY", "ROW", "RUN", "PATH",
    "PASS", "LOOP", "BEND", "GLEN", "PARK", "PLAZA", "POINT",
    "RIDGE", "SQUARE", "WALK", "STREET", "AVENUE", "DRIVE",
    "BOULEVARD", "ROAD", "LANE", "COURT", "PLACE", "CIRCLE",
    "HIGHWAY", "TERRACE", "TRAIL", "PARKWAY",
}

# Work date + lien amount:
#   "completed on or about 6/13/2025, at a total cost of $2,399.00"
RE_WORK_AMOUNT = re.compile(
    r"completed\s+on\s+or\s+about\s+(?P<date>\d{1,2}/\d{1,2}/\d{4})"
    r".*?total\s+cost\s+of\s+\$(?P<amount>[\d,]+\.\d{2})",
    re.I,
)

# Owner block: 3 lines after "record owner...is:". The block is
# terminated by the next "That said" paragraph (the legal-language
# section that follows).
# OCR variabilities to handle:
#   1. The colon after "is" may be dropped/missing on some scans.
#   2. "That said" sometimes OCRs as "That sald" (i→l confusion)
#      because the cursive signature in the same line area smudges
#      letters.
#   3. A stray non-word character may prepend "That" (smart-quote
#      artifact from OCR, e.g. "'That said").
RE_OWNER_BLOCK = re.compile(
    r"record\s+owner.*?is:?\s*\n\s*\n?"
    r"(?P<block>.+?)"
    r"\n\s*\n?\W?That\s+s[ai][il]d",
    re.S | re.I,
)

# Spouse markers in the owner string. CCLN PDFs use various forms:
#   "AND WF MARIA SERRATA"
#   "AND HUSBAND JOHN SMITH"
#   "AND SPOUSE PAT JONES"
RE_SPOUSE = re.compile(
    r"^(?P<primary>.+?)\s+AND\s+(?:WF|WIFE|HUS|HUSBAND|SPOUSE)\s+(?P<spouse>.+)$",
    re.I,
)

# City/state/zip on a single line: "CORPUS CHRISTI, TX 78404"
# Allows optional zip+4 ("78404-1234") and optional trailing
# punctuation (some OCR runs append a stray comma at end of the
# line, especially when the scan picked up the period at end of
# sentence on the next line).
RE_CITY_STATE_ZIP = re.compile(
    r"^(?P<city>.+?),\s*(?P<state>[A-Z]{2})\s+(?P<zip>\d{5})(?:-\d{4})?[.,]?\s*$",
)

# Lien reason — the boilerplate sentence describes which violations
# the city addressed. We tag keywords found in the sentence.
RE_REASON_SENTENCE = re.compile(
    r"removing,?\s+cleaning,?\s+clearing(?P<body>.+?)matter",
    re.S | re.I,
)

REASON_KEYWORDS = [
    # (keyword to search for, tag to record)
    ("substandard buildings", "substandard_building"),
    ("filth, carrion",        "carrion"),
    ("filth", "filth"),
    ("weeds, rubbish",         "weeds"),
    ("weeds",                  "weeds"),
    ("rubbish",                "rubbish"),
    ("brush",                  "brush"),
    ("demolishing",            "demolition"),
]


# ----------------------------------------------------------------
# Data class for extracted fields
# ----------------------------------------------------------------
@dataclass
class Extracted:
    """Output of one PDF's extraction. All fields optional — anything
    we couldn't pull is left as None, and incomplete_extraction is set."""
    doc_num: Optional[str] = None
    pdf_filed_date: Optional[str] = None        # ISO format
    internal_lien_id: Optional[str] = None
    ncad_account_num: Optional[str] = None
    legal_from_pdf: Optional[str] = None
    prop_address: Optional[str] = None
    prop_city: Optional[str] = None             # always "CORPUS CHRISTI"
    prop_state: Optional[str] = None            # always "TX"
    prop_zip: Optional[str] = None              # inferred from mailing if same
    pdf_owner: Optional[str] = None             # primary owner name
    pdf_spouse: Optional[str] = None            # spouse name if "AND WF ..." present
    mail_address: Optional[str] = None
    mail_city: Optional[str] = None
    mail_state: Optional[str] = None
    mail_zip: Optional[str] = None
    work_date: Optional[str] = None             # ISO format
    pdf_amount: Optional[float] = None
    reason_tags: List[str] = field(default_factory=list)
    flags: List[str] = field(default_factory=list)


# ----------------------------------------------------------------
# PDF → image → OCR pipeline
# ----------------------------------------------------------------
def rasterize_page1(pdf_path: Path, out_dir: Path) -> Optional[Path]:
    """Rasterize the FIRST PAGE of a PDF to a JPEG at 150 DPI.

    Page 1 is all we need — page 2 is the standard clerk certification
    page with no extractable data we care about. Skipping page 2 cuts
    OCR time roughly in half per PDF.

    Returns the path to the resulting JPEG, or None if rasterization
    failed. ``out_dir`` is created if missing.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    out_prefix = out_dir / pdf_path.stem
    try:
        subprocess.run(
            ["pdftoppm", "-jpeg", "-r", "150",
             "-f", "1", "-l", "1",
             str(pdf_path), str(out_prefix)],
            check=True, capture_output=True, timeout=30,
        )
    except subprocess.CalledProcessError as exc:
        log.error("pdftoppm failed for %s: %s",
                  pdf_path.name, exc.stderr.decode("utf-8", "replace"))
        return None
    except subprocess.TimeoutExpired:
        log.error("pdftoppm timeout on %s", pdf_path.name)
        return None
    # pdftoppm pads the suffix based on total page count:
    #   1-page PDF  → {stem}-1.jpg
    #   2-page PDF  → {stem}-1.jpg, {stem}-2.jpg
    #   100+ pages  → {stem}-001.jpg etc.
    # CCLN PDFs are 2 pages so the suffix is "-1".
    for candidate in (f"{pdf_path.stem}-1.jpg",
                      f"{pdf_path.stem}-01.jpg",
                      f"{pdf_path.stem}-001.jpg"):
        c = out_dir / candidate
        if c.exists():
            return c
    log.error("rasterized output not found for %s", pdf_path.name)
    return None


def run_ocr(jpeg_path: Path) -> Optional[str]:
    """Run tesseract OCR on a rasterized page and return the text.

    Returns None on tesseract failure. tesseract is invoked with
    default English model; CCLN PDFs are clean enough not to need
    any non-default tuning.
    """
    try:
        result = subprocess.run(
            ["tesseract", str(jpeg_path), "-"],
            check=True, capture_output=True, timeout=60,
        )
        return result.stdout.decode("utf-8", "replace")
    except subprocess.CalledProcessError as exc:
        log.error("tesseract failed on %s: %s",
                  jpeg_path.name, exc.stderr.decode("utf-8", "replace"))
        return None
    except subprocess.TimeoutExpired:
        log.error("tesseract timeout on %s", jpeg_path.name)
        return None


# ----------------------------------------------------------------
# Filename → doc_num matcher
# ----------------------------------------------------------------
# Recognizes:
#   "2025027139.pdf"          → 2025027139
#   "2025027139 SERRATA.pdf"  → 2025027139
#   "doc_2025027139.pdf"      → 2025027139
# Rejects:
#   "17024705_244962255_docimage_actual.pdf"
#     (no 10-digit token starting with 20xx — clerk portal IDs are 8 digits)
RE_FILENAME_DOC = re.compile(r"\b(20\d{8})\b")


def match_filename_to_doc(filename: str) -> Optional[str]:
    """Try to extract a CCLN doc_num from a filename.

    Returns the doc_num string (e.g. "2025027139") if found, None
    otherwise. The caller should fall back to OCR header parsing if
    this returns None.
    """
    m = RE_FILENAME_DOC.search(filename)
    return m.group(1) if m else None


# ----------------------------------------------------------------
# OCR text → structured fields
# ----------------------------------------------------------------
def parse_ocr_text(ocr: str) -> Extracted:
    """Apply every field extractor to OCR text and return Extracted."""
    out = Extracted()

    # Header → doc_num + PDF-stated filed date
    m = RE_HEADER.search(ocr)
    if m:
        out.doc_num = m.group("doc")
        try:
            d = datetime.strptime(m.group("filed"), "%m/%d/%Y").date()
            out.pdf_filed_date = d.isoformat()
        except ValueError:
            pass

    # Internal city lien id
    m = RE_INTERNAL_ID.search(ocr)
    if m:
        out.internal_lien_id = m.group("id")

    # NCAD account — normalize OCR-wrapped format back to canonical
    # NNNN-NNNN-NNNN. The regex tolerates whitespace, newlines, and
    # stray dots BETWEEN digit groups (see RE_NCAD_ACCT for why).
    # Strip all non-digit characters, then re-insert the two dashes
    # at fixed positions for the 12-digit account number.
    m = RE_NCAD_ACCT.search(ocr)
    if m:
        digits = re.sub(r"\D", "", m.group("acct"))
        if len(digits) == 12:
            out.ncad_account_num = f"{digits[:4]}-{digits[4:8]}-{digits[8:]}"
        else:
            # Pass through whatever we captured — validator will flag.
            out.ncad_account_num = m.group("acct").strip()

    # Legal description
    m = RE_LEGAL.search(ocr)
    if m:
        out.legal_from_pdf = m.group("legal").strip()

    # Property street. The regex may capture across two lines when
    # the city wraps the address — normalize newlines + extra spaces
    # into a single space to get the full "1822 KEYS" or "1529 7TH ST".
    m = RE_PROP_STREET.search(ocr)
    if m:
        street = re.sub(r"\s+", " ", m.group("street").strip())
        # Some PDFs have NO property street address — the legal/account
        # line is followed directly by the next paragraph ("That said
        # work was completed..."). My regex greedily captures the
        # next non-blank content, which in those cases is body text.
        # Detect that case here and discard the capture rather than
        # storing a paragraph as an address.
        #
        # Triggers:
        #   - Starts with "That said" or its OCR variant "That sald"
        #     (i↔l confusion documented elsewhere).
        #   - Contains too many words to be a real street (>8 tokens).
        #   - Looks like sentence text (contains "was completed" or
        #     "at a total cost" or "invoiced").
        #
        # When we discard, prop_address stays None and the validator
        # flags the record with `incomplete_extraction`. The record
        # is still usable via NCAD account number — addresses that
        # the city doesn't have on file can be manually added later
        # if the user looks up the legal description.
        looks_like_paragraph = (
            re.match(r"^That\s+s[ai][il]d\b", street, re.I) or
            len(street.split()) > 8 or
            re.search(r"\b(was\s+completed|at\s+a\s+total\s+cost|"
                      r"which\s+was\s+invoiced)\b", street, re.I)
        )
        if looks_like_paragraph:
            log.info("  street capture rejected (looks like body text): "
                     "%r — PDF likely has no property address", street[:60])
            # Don't set prop_address; record will use NCAD only.
        else:
            out.prop_address = street
            out.prop_city = "CORPUS CHRISTI"
            out.prop_state = "TX"

    # Work date + amount
    m = RE_WORK_AMOUNT.search(ocr)
    if m:
        try:
            d = datetime.strptime(m.group("date"), "%m/%d/%Y").date()
            out.work_date = d.isoformat()
        except ValueError:
            pass
        try:
            out.pdf_amount = float(m.group("amount").replace(",", ""))
        except ValueError:
            pass

    # Owner block. Variable line count:
    #   Owner-occupied (most common, 3 lines):
    #     OWNER NAME
    #     STREET
    #     CITY, ST ZIP
    #   LLC / corporate with suite (4+ lines):
    #     COMPANY NAME LLC
    #     STREET
    #     STE 952       ← variable number of intermediate lines
    #     CITY, ST ZIP
    # Strategy: walk from the END of the block to find the city/state/
    # zip line. Everything between line[0] (owner name) and that line
    # joins together as the mailing street. This handles both shapes
    # without needing to detect "STE"/"#"/etc. explicitly.
    m = RE_OWNER_BLOCK.search(ocr)
    if m:
        lines = [ln.strip() for ln in m.group("block").split("\n")
                 if ln.strip()]
        if lines:
            # Line 0: owner name. Try to split off a spouse if "AND
            # WF/WIFE/HUS/HUSBAND/SPOUSE" appears.
            primary = lines[0]
            sm = RE_SPOUSE.match(primary)
            if sm:
                out.pdf_owner = sm.group("primary").strip()
                out.pdf_spouse = sm.group("spouse").strip()
            else:
                out.pdf_owner = primary

        # Walk backward to find the city/state/zip line. We only
        # consider lines AFTER line[0] so the owner name can't be
        # mistakenly parsed as an address.
        csz_idx = None
        for i in range(len(lines) - 1, 0, -1):
            csm = RE_CITY_STATE_ZIP.match(lines[i])
            if csm:
                csz_idx = i
                out.mail_city = csm.group("city").strip()
                out.mail_state = csm.group("state")
                out.mail_zip = csm.group("zip")
                break

        # Mailing street: everything between owner (line 0) and the
        # city/state/zip line. Join with spaces (e.g. "440 LOUISIANA
        # ST STE 952" for a corporate suite). For a 3-line block
        # this is just lines[1].
        if csz_idx is not None and csz_idx > 1:
            out.mail_address = " ".join(lines[1:csz_idx])
        elif len(lines) >= 2:
            out.mail_address = lines[1]

        # If property address matches mailing address, the property
        # zip is the same as mailing zip. Common case for owner-
        # occupied (1822 KEYS = 1822 KEYS). For absentee owners the
        # property zip stays None and gets filled in later via NCAD
        # or another source.
        if (out.prop_address and out.mail_address and out.mail_zip
                and out.prop_address.upper().strip()
                    == out.mail_address.upper().strip()):
            out.prop_zip = out.mail_zip

    # Lien reason — scan the boilerplate sentence for tagged keywords.
    m = RE_REASON_SENTENCE.search(ocr)
    if m:
        body_lower = m.group("body").lower()
        seen_tags: set = set()
        for kw, tag in REASON_KEYWORDS:
            if kw in body_lower and tag not in seen_tags:
                out.reason_tags.append(tag)
                seen_tags.add(tag)

    return out


# ----------------------------------------------------------------
# Validation flags
# ----------------------------------------------------------------
def validate(ex: Extracted, clerk_record: Dict[str, Any]) -> None:
    """Run validators against the extracted fields and the existing
    clerk-portal record. Mutates ``ex.flags`` in place.

    Validators are listed in priority order — the most-actionable
    flags appear first when the UI lists them.
    """
    # ZIP format
    if ex.mail_zip:
        if not re.match(r"^\d{5}$", ex.mail_zip):
            ex.flags.append("bad_zip_format")
        elif not ex.mail_zip.startswith("784"):
            # Nueces County ZIPs are 78401-78480. Outside that range
            # means the owner mails to somewhere else — could be
            # absentee owner (legitimate) or could be OCR error.
            # We flag it for review either way; the user can confirm
            # whether it's a real out-of-town address.
            ex.flags.append("zip_outside_cc")

    # Property address quality — flag if it's suspiciously short or
    # lacks a recognized street suffix. CCLN PDFs sometimes truncate
    # the address when OCR wraps mid-word; partial extractions like
    # "11515" (just a number, no street name) or "PUEBLO" (street
    # name without number) get caught here.
    if ex.prop_address:
        clean = ex.prop_address.strip().upper()
        tokens = clean.split()
        has_number = any(t.replace("-", "").isdigit() for t in tokens)
        has_suffix = any(t.rstrip(".,") in STREET_SUFFIX_TOKENS
                         for t in tokens)
        # Real addresses look like "1822 KEYS DR" or "11515 HAVEN DR" —
        # have both a number AND a recognizable street suffix. If we
        # don't have a suffix it might be a "named property" without
        # a street type (rare but real, e.g. "1822 KEYS" with no DR/ST),
        # but if we don't even have a NUMBER something definitely went
        # wrong. We use a 2-tier check: missing both → strong flag;
        # missing just suffix → softer flag.
        if not has_number or len(clean) < 6:
            ex.flags.append("suspicious_short_address")

    # Absentee-owner signal — not really an "error" flag, but useful
    # lead-quality info: when prop_address differs from mail_address,
    # the owner doesn't live at the lien property. Often correlates
    # with stronger leads (out-of-state landlords, inherited estates,
    # vacant properties). The dashboard can use this for sorting/
    # filtering.
    if (ex.prop_address and ex.mail_address
            and ex.prop_address.strip().upper()
                != ex.mail_address.strip().upper()):
        ex.flags.append("absentee_owner")

    # NCAD account format
    if ex.ncad_account_num:
        if not re.match(r"^\d{4}-\d{4}-\d{4}$", ex.ncad_account_num):
            ex.flags.append("bad_ncad_account")

    # Amount mismatch — compare PDF amount vs clerk-portal amount
    # within a $0.01 tolerance (float comparison).
    if ex.pdf_amount is not None and clerk_record.get("amount"):
        try:
            clerk_amt = float(clerk_record["amount"])
            if abs(ex.pdf_amount - clerk_amt) > 0.01:
                ex.flags.append("amount_mismatch")
        except (TypeError, ValueError):
            pass

    # Owner mismatch — compare PDF owner name to clerk-portal owner.
    # We're permissive here: if ANY token (>= 3 chars) from the PDF
    # owner appears in the clerk owner, consider it a match. This
    # tolerates name-order swaps ("SMITH JOHN" vs "JOHN SMITH"),
    # title differences ("DBA", "TRUST"), and partial matches.
    if ex.pdf_owner and clerk_record.get("owner"):
        pdf_tokens = {t for t in re.findall(r"\w+", ex.pdf_owner.upper())
                      if len(t) >= 3}
        clerk_upper = clerk_record["owner"].upper()
        overlap = any(t in clerk_upper for t in pdf_tokens)
        if not overlap:
            ex.flags.append("owner_mismatch")

    # Work date sanity
    if ex.work_date:
        try:
            d = date.fromisoformat(ex.work_date)
            if d > date.today():
                ex.flags.append("bad_work_date")
        except ValueError:
            ex.flags.append("bad_work_date")

    # Incomplete extraction — every PDF should produce at least these
    # fields. If any is missing, the OCR or parse went wrong.
    required = [ex.ncad_account_num, ex.prop_address, ex.pdf_owner,
                ex.mail_zip]
    if not all(required):
        ex.flags.append("incomplete_extraction")


# ----------------------------------------------------------------
# Apply extracted fields to a CCLN record
# ----------------------------------------------------------------
# The clerk-portal data is the system of record. PDF-extracted fields
# fill in MISSING data only — they don't overwrite existing values.
# The two exceptions are:
#   - mail_* fields: clerk portal rarely has these populated for CCLN,
#     and the PDF is the authoritative source.
#   - prop_address (when empty): the PDF gives us a real street address
#     for records where Phase 1/2 enrichment failed.
# Mismatches don't overwrite either way; they just set a flag.

# Fields the CLERK PORTAL populates. PDF extraction never overwrites
# these — even on force=true. Clerk portal is the system of record
# for these. Mismatches are surfaced as flags, not overwrites.
CLERK_PROTECTED_FIELDS = {
    "owner", "amount", "filed", "doc_num", "doc_type",
    "cat", "cat_label", "grantee", "clerk_url", "score",
    # 'legal' is special: the clerk portal has it, but the PDF
    # version may be cleaner. We don't overwrite the clerk 'legal'
    # field directly — we write the PDF version to 'legal_from_pdf'
    # so the dashboard can pick whichever it wants to display.
    "legal",
}

# Address fields that BOTH the clerk portal and the PDF may have.
# Treatment depends on context:
#   - If the record has no PDF extraction yet (ccln_pdf_processed
#     is missing): write only if the existing value is empty.
#     This is the "first time" case — don't trample any clerk data.
#   - If the record HAS been PDF-processed (ccln_pdf_processed is
#     True): the existing value is from a previous PDF run, so it's
#     safe to overwrite with the current PDF run's value. This is
#     what makes force=true useful for fixing bad extractions.
SHARED_ADDRESS_FIELDS = {
    "prop_address", "prop_city", "prop_state", "prop_zip",
    "mail_address", "mail_city", "mail_state", "mail_zip",
}

# Pure PDF-source fields. The clerk portal never has these. Always
# overwrite with the current extraction — re-runs are how you fix
# bad regex output.
PDF_ONLY_FIELDS = {
    "ncad_account_num", "internal_lien_id",
    "pdf_owner", "pdf_spouse",
    "work_date", "legal_from_pdf",
    "pdf_filed_date", "pdf_amount",
}


def apply_to_record(record: Dict[str, Any], ex: Extracted,
                    force: bool = False) -> None:
    """Merge extracted fields into a CCLN JSON record in place.

    Field-write policy:

    - Clerk-portal fields (owner, amount, filed, legal, etc.):
      NEVER written by the PDF extractor. Mismatches between clerk
      and PDF are surfaced via flags only.

    - PDF-only fields (ncad_account_num, pdf_owner, pdf_spouse,
      work_date, etc.): always overwritten on every run. These
      come from one source (the PDF), so re-running with improved
      regexes is how you fix bad data.

    - Shared address fields (prop_address, mail_*, etc.): handled
      based on context:
        * force=true: overwrite everything. This is what makes
          force=true useful for fixing bad extractions from prior
          runs that didn't get marked with ccln_pdf_processed
          (or were marked but had truncated regex output).
        * Otherwise was_processed=true: same — prior value is
          ours, safe to refresh.
        * Otherwise (first time, not forced): write only if cell
          is empty. Preserves any clerk-portal-sourced data.
    """
    was_processed = bool(record.get("ccln_pdf_processed"))
    overwrite_shared = force or was_processed

    # 1) PDF-only fields: write whatever we extracted (skip empties).
    for key in PDF_ONLY_FIELDS:
        val = getattr(ex, key, None)
        if val in (None, "", 0, 0.0):
            continue
        record[key] = val

    # 2) Shared address fields: depends on whether we've touched
    #    this record before OR force is set.
    for key in SHARED_ADDRESS_FIELDS:
        val = getattr(ex, key, None)
        if val in (None, "", 0, 0.0):
            continue
        if overwrite_shared or not record.get(key):
            record[key] = val

    # 3) reason_tags: list, written fresh whenever we extract some.
    if ex.reason_tags:
        record["reason_tags"] = ex.reason_tags

    # 4) Flags: always written fresh — a re-extraction may have
    #    raised new flags or cleared old ones.
    record["ccln_pdf_flags"] = ex.flags

    # 5) Markers — set unconditionally on any successful processing.
    #    The dashboard reads ccln_pdf_processed to render the ✓ badge.
    record["ccln_pdf_processed"] = True
    record["ccln_pdf_processed_at"] = (
        datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))


# ----------------------------------------------------------------
# Main pipeline
# ----------------------------------------------------------------
def load_records() -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Read city_liens.json. Returns (records_list, original_envelope).

    The file format from fetch.py is a wrapped dict:
        {"fetched_at": ..., "source": ..., "total": N, "records": [...]}

    We return both the records list (for processing) AND a copy of
    the envelope metadata (for re-saving in the same format). If the
    file is a bare list (older format), envelope is an empty dict.
    """
    if not DATA_FILE.exists():
        log.warning("data file not found: %s", DATA_FILE)
        return [], {}
    try:
        with DATA_FILE.open("r", encoding="utf-8") as f:
            raw = json.load(f)
        # New format: dict with "records" key + metadata wrapper.
        if isinstance(raw, dict) and "records" in raw:
            envelope = {k: v for k, v in raw.items() if k != "records"}
            return raw["records"], envelope
        # Legacy format: bare list.
        if isinstance(raw, list):
            return raw, {}
        log.error("unexpected format in %s: top-level is %s",
                  DATA_FILE, type(raw).__name__)
        return [], {}
    except Exception as exc:
        log.error("failed to load %s: %s", DATA_FILE, exc)
        return [], {}


def save_records(records: List[Dict[str, Any]],
                 envelope: Dict[str, Any]) -> None:
    """Write city_liens.json AND mirror to dashboard/city_liens.json.

    Preserves the wrapped-dict format from fetch.py (the source/
    fetched_at/total envelope). Updates the total count and refreshes
    fetched_at to the current time so dashboard "last updated" shows
    when the PDF enrichment was last applied.
    """
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DASHBOARD_FILE.parent.mkdir(parents=True, exist_ok=True)
    out_envelope = dict(envelope) if envelope else {}
    out_envelope["fetched_at"] = (datetime.now(timezone.utc).isoformat()
                                  .replace("+00:00", "+00:00"))
    out_envelope.setdefault("source",
                            "Nueces County Clerk — CCLN cumulative")
    out_envelope["total"] = len(records)
    out_envelope["records"] = records
    payload = json.dumps(out_envelope, indent=2, default=str,
                         ensure_ascii=False)
    DATA_FILE.write_text(payload, encoding="utf-8")
    DASHBOARD_FILE.write_text(payload, encoding="utf-8")
    log.info("saved %d records to %s + %s",
             len(records), DATA_FILE, DASHBOARD_FILE)


def process_pdf(pdf_path: Path,
                records_by_doc: Dict[str, Dict[str, Any]],
                force: bool = False) -> Tuple[bool, str]:
    """Process a single PDF. Returns (success, message).

    On success the PDF can be deleted by the caller. On failure
    (no record match, OCR fail, etc.) the PDF is LEFT IN PLACE so
    the user can investigate.
    """
    log.info("processing %s", pdf_path.name)

    # Step 1: filename match attempt
    doc_num = match_filename_to_doc(pdf_path.name)
    if doc_num and doc_num in records_by_doc:
        log.info("  filename match → doc_num %s", doc_num)
    else:
        doc_num = None  # we'll set it after OCR

    # Step 2: rasterize page 1 and run OCR (always needed for full
    # extraction even if filename matched)
    jpeg = rasterize_page1(pdf_path, TMP_DIR)
    if jpeg is None:
        return False, "rasterize failed"
    ocr = run_ocr(jpeg)
    if ocr is None:
        return False, "OCR failed"

    # Step 3: parse all fields out of OCR text
    ex = parse_ocr_text(ocr)

    # Step 4: reconcile filename doc_num vs OCR doc_num. The PDF's
    # own header is authoritative — if the filename and OCR disagree,
    # trust OCR. This catches PDFs that were renamed incorrectly
    # before upload (e.g. user typoed the doc_num when renaming).
    if doc_num is not None and ex.doc_num and ex.doc_num != doc_num:
        log.warning("  filename doc_num %s differs from PDF header %s — "
                    "using PDF header (authoritative)", doc_num, ex.doc_num)
        doc_num = ex.doc_num
    if doc_num is None:
        if not ex.doc_num:
            return False, "no doc_num found in filename or OCR header"
        doc_num = ex.doc_num
        log.info("  OCR header match → doc_num %s", doc_num)

    # Step 5: find the matching CCLN record
    record = records_by_doc.get(doc_num)
    if record is None:
        # Record not found in city_liens.json. Two possible reasons:
        # (a) The record was filtered out by the corporate-owner
        #     cleanup workflow (cleanup_corporate_ccln.py removed 155
        #     records that classified as LLC/INC/CORP/etc.). PDFs
        #     for those records are expected to "fail to match" here
        #     because the records no longer exist. We should silently
        #     delete the PDF, not error out.
        # (b) A real bug — record went missing from city_liens.json
        #     but the PDF is for a legitimate individual lead. We
        #     should keep the PDF for investigation in this case.
        #
        # To distinguish, classify the OCR'd owner from the PDF. If
        # it's corporate, treat as expected; otherwise, treat as
        # real failure. Return a "SKIP_PDF" signal that the main
        # loop will translate into PDF deletion + non-error path.
        pdf_owner = ex.pdf_owner or ""
        if pdf_owner:
            kind, keep = classify_owner(pdf_owner)
            if not keep:
                log.info("  no CCLN record for doc_num %s — owner is "
                         "%s (%s), record was filtered out by the "
                         "corporate cleanup; deleting PDF",
                         doc_num, pdf_owner, kind)
                # Signal: PDF should be deleted, no record changes,
                # NOT counted as a failure.
                return True, f"SKIP_PDF:{doc_num} (corporate, no record exists)"
        return False, f"no CCLN record for doc_num {doc_num}"

    # Step 6: skip if already processed (unless force)
    if record.get("ccln_pdf_processed") and not force:
        return True, f"already processed (skipped — use force=true to redo)"

    # Step 7: check corporate-owner filter. Once OCR has confirmed the
    # owner on the PDF, we have the most reliable owner name available
    # (clerk-portal data sometimes mis-spells or truncates the owner).
    # If the PDF says corporate, delete the record and the PDF.
    # ``kind`` is one of: company, religious, school, government,
    # nonprofit, hoa, trust_inst (all → keep=False); individual,
    # estate, family_trust (keep=True).
    #
    # We check BOTH the PDF-extracted owner AND the clerk-portal owner.
    # If EITHER says corporate, we exclude — this catches cases where
    # OCR couldn't read the PDF owner clearly but the clerk record
    # already had a clear corporate name.
    pdf_owner = ex.pdf_owner or ""
    clerk_owner = record.get("owner") or ""
    pdf_kind, pdf_keep = classify_owner(pdf_owner) if pdf_owner else ("", True)
    clerk_kind, clerk_keep = classify_owner(clerk_owner)
    if not pdf_keep or not clerk_keep:
        # Pick the more-actionable kind for the log message:
        # prefer the PDF-derived classification when both flagged.
        kind = pdf_kind if (pdf_owner and not pdf_keep) else clerk_kind
        log.info("  corporate owner detected (%s = %s) — deleting record",
                 kind, kind_label(kind))
        # Signal deletion to the caller via the message convention.
        # The caller looks for "DELETE_RECORD:{doc_num}" and removes
        # the record from the records list (NOT just the lookup dict).
        return True, f"DELETE_RECORD:{doc_num} (corporate: {kind})"

    # Step 8: run validation flags
    validate(ex, record)
    if ex.flags:
        log.info("  flags: %s", ", ".join(ex.flags))

    # Step 9: write fields onto the record
    apply_to_record(record, ex, force=force)
    log.info("  extracted: addr=%r ncad=%r owner=%r reason=%r",
             ex.prop_address, ex.ncad_account_num,
             ex.pdf_owner, ex.reason_tags)

    return True, "ok"


def main() -> int:
    """Entry point — process every PDF in pdfs/ccln_pending/."""
    force = os.environ.get("FORCE", "").lower() in ("1", "true", "yes")
    if force:
        log.info("FORCE=true: reprocessing PDFs even for already-extracted records")

    if not PENDING_DIR.exists():
        log.warning("pending dir does not exist: %s", PENDING_DIR)
        log.warning("nothing to process — create %s and upload PDFs there",
                    PENDING_DIR)
        return 0

    pdfs = sorted(PENDING_DIR.glob("*.pdf")) + sorted(PENDING_DIR.glob("*.PDF"))
    if not pdfs:
        log.info("no PDFs found in %s", PENDING_DIR)
        return 0
    log.info("found %d PDFs to process", len(pdfs))

    records, envelope = load_records()
    if not records:
        log.error("no CCLN records in city_liens.json — run backfill first")
        return 1

    # Index by doc_num for fast lookup.
    records_by_doc: Dict[str, Dict[str, Any]] = {
        str(r.get("doc_num")): r for r in records if r.get("doc_num")
    }
    log.info("indexed %d CCLN records by doc_num", len(records_by_doc))

    n_ok = 0
    n_fail = 0
    n_skipped = 0
    n_deleted = 0
    # Track doc_nums marked for corporate deletion. We can't remove
    # them mid-loop (we're iterating records_by_doc indirectly), so
    # we collect them and apply the deletions after the loop.
    delete_doc_nums: List[str] = []
    for pdf in pdfs:
        try:
            ok, msg = process_pdf(pdf, records_by_doc, force=force)
        except Exception as exc:
            log.error("unexpected error on %s: %s\n%s",
                      pdf.name, exc, traceback.format_exc())
            ok, msg = False, f"exception: {exc}"

        if ok:
            if msg.startswith("DELETE_RECORD:"):
                # Corporate-owner case. Extract the doc_num after the
                # prefix and queue it for removal.
                #   "DELETE_RECORD:2025027154 (corporate: company)"
                rest = msg[len("DELETE_RECORD:"):].strip()
                doc_to_delete = rest.split(" ", 1)[0]
                delete_doc_nums.append(doc_to_delete)
                n_deleted += 1
                log.info("  → record %s marked for deletion (%s)",
                         doc_to_delete, rest)
                # PDF gets deleted too — no reason to keep it.
                try:
                    pdf.unlink()
                    log.info("  PDF deleted: %s", pdf.name)
                except OSError as exc:
                    log.warning("could not delete %s: %s", pdf.name, exc)
            elif msg.startswith("SKIP_PDF:"):
                # Record doesn't exist in city_liens.json AND the PDF's
                # owner is corporate — meaning the record was already
                # filtered out by the corporate-cleanup workflow. No
                # error, no data change; just clear the PDF so it
                # doesn't pile up in pdfs/ccln_pending/. Counted as a
                # skip (not a failure, not a fresh extraction).
                n_skipped += 1
                log.info("  → %s", msg)
                try:
                    pdf.unlink()
                    log.info("  PDF deleted: %s", pdf.name)
                except OSError as exc:
                    log.warning("could not delete %s: %s", pdf.name, exc)
            elif "already processed" in msg:
                n_skipped += 1
                log.info("  → %s", msg)
                # For already-processed: delete the PDF anyway so the
                # pending folder doesn't accumulate. The record is
                # already updated; keeping the PDF here serves no
                # purpose. (User can re-upload + force=true if they
                # really want to reprocess.)
                try:
                    pdf.unlink()
                    log.info("  PDF deleted: %s", pdf.name)
                except OSError as exc:
                    log.warning("could not delete %s: %s", pdf.name, exc)
            else:
                n_ok += 1
                log.info("  → %s", msg)
                # Delete the processed PDF.
                try:
                    pdf.unlink()
                    log.info("  PDF deleted: %s", pdf.name)
                except OSError as exc:
                    log.warning("could not delete %s: %s", pdf.name, exc)
        else:
            n_fail += 1
            log.error("  → FAILED: %s (PDF kept for investigation)", msg)

    # Clean up rasterized scratch files.
    if TMP_DIR.exists():
        shutil.rmtree(TMP_DIR, ignore_errors=True)

    # Apply queued corporate-owner deletions. The records list still
    # contains the original record objects; we filter it down to
    # only those whose doc_num is NOT in the delete set.
    if delete_doc_nums:
        delete_set = set(delete_doc_nums)
        before = len(records)
        records[:] = [r for r in records
                      if str(r.get("doc_num")) not in delete_set]
        after = len(records)
        log.info("removed %d corporate records from JSON (%d → %d)",
                 before - after, before, after)

    log.info("=== summary: %d ok, %d already-done, %d corporate-deleted, "
             "%d failed (of %d) ===",
             n_ok, n_skipped, n_deleted, n_fail, len(pdfs))

    # Save updated records when anything changed: a fresh extraction,
    # a force-flag override, OR a corporate deletion.
    if n_ok > 0 or n_deleted > 0 or force:
        save_records(records, envelope)
    else:
        log.info("no changes — skipping save")

    return 0 if n_fail == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
