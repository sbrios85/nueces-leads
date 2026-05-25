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
RE_NCAD_ACCT = re.compile(
    r"Account\s+No\.?\s+(?P<acct>\d{4}-\d{4}-\d{4})",
    re.I,
)

# Legal description: starts a line, ALL CAPS subdivision name +
# "BLK N LOT N(,N)*" pattern. We anchor to BLK/LOT to avoid false
# positives — those keywords are reliable markers on CCLN forms.
RE_LEGAL = re.compile(
    r"^(?P<legal>[A-Z][A-Z0-9 ]+?BLK\s+\d+\s+LOT\s+\d+(?:[,\s]+\d+)*)",
    re.M,
)

# Property street comes at end of the legal/account line:
#   "...Account No. 0386-0005-0060. 1822 KEYS"
RE_PROP_STREET = re.compile(
    r"Account\s+No\.?\s+\d{4}-\d{4}-\d{4}\.?\s+(?P<street>[A-Z0-9][^\n]+?)\s*$",
    re.M | re.I,
)

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
RE_OWNER_BLOCK = re.compile(
    r"record\s+owner.*?is:\s*\n\s*\n?(?P<block>.+?)\n\s*\n?That\s+said",
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
RE_CITY_STATE_ZIP = re.compile(
    r"^(?P<city>.+?),\s*(?P<state>[A-Z]{2})\s+(?P<zip>\d{5})(?:-\d{4})?$",
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

    # NCAD account
    m = RE_NCAD_ACCT.search(ocr)
    if m:
        out.ncad_account_num = m.group("acct")

    # Legal description
    m = RE_LEGAL.search(ocr)
    if m:
        out.legal_from_pdf = m.group("legal").strip()

    # Property street
    m = RE_PROP_STREET.search(ocr)
    if m:
        out.prop_address = m.group("street").strip()
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

    # Owner block (3 lines: name, street, city/state/zip)
    m = RE_OWNER_BLOCK.search(ocr)
    if m:
        lines = [ln.strip() for ln in m.group("block").split("\n")
                 if ln.strip()]
        if lines:
            # Line 1 is the owner. Try to split off a spouse name.
            primary = lines[0]
            sm = RE_SPOUSE.match(primary)
            if sm:
                out.pdf_owner = sm.group("primary").strip()
                out.pdf_spouse = sm.group("spouse").strip()
            else:
                out.pdf_owner = primary
        if len(lines) >= 2:
            out.mail_address = lines[1]
        if len(lines) >= 3:
            csm = RE_CITY_STATE_ZIP.match(lines[2])
            if csm:
                out.mail_city = csm.group("city").strip()
                out.mail_state = csm.group("state")
                out.mail_zip = csm.group("zip")
                # If property address matches mailing address, the
                # property ZIP is the same as mailing ZIP. That's the
                # common case for owner-occupied (1822 KEYS = 1822 KEYS).
                if (out.prop_address and out.mail_address
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

# Fields we ADD (only when clerk doesn't have a value):
ADDITIVE_FIELDS = [
    "ncad_account_num",
    "internal_lien_id",
    "prop_address",
    "prop_city",
    "prop_state",
    "prop_zip",
    "mail_address",
    "mail_city",
    "mail_state",
    "mail_zip",
    "pdf_owner",
    "pdf_spouse",
    "work_date",
    "legal_from_pdf",
    "pdf_filed_date",
    "pdf_amount",
]


def apply_to_record(record: Dict[str, Any], ex: Extracted) -> None:
    """Merge extracted fields into a CCLN JSON record in place.

    Fields are added when the record's existing value is falsy (empty
    string, None, 0). Existing data is never overwritten — clerk-
    portal data wins on conflict, and conflicts are surfaced via the
    flags list.
    """
    for key in ADDITIVE_FIELDS:
        val = getattr(ex, key, None)
        if val in (None, "", 0, 0.0):
            continue
        if not record.get(key):
            record[key] = val

    # reason_tags: write fresh (it's a list, not a scalar). Empty
    # list means we couldn't detect any keywords — leave any existing
    # value alone.
    if ex.reason_tags:
        record["reason_tags"] = ex.reason_tags

    # flags: write fresh; previous run's flags shouldn't persist if
    # a re-extraction clears them.
    record["ccln_pdf_flags"] = ex.flags

    # Marker so the dashboard can show ✓ on processed records, and so
    # this module can skip already-processed records on re-run.
    record["ccln_pdf_processed"] = True
    record["ccln_pdf_processed_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


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
        return False, f"no CCLN record for doc_num {doc_num}"

    # Step 6: skip if already processed (unless force)
    if record.get("ccln_pdf_processed") and not force:
        return True, f"already processed (skipped — use force=true to redo)"

    # Step 7: run validation flags
    validate(ex, record)
    if ex.flags:
        log.info("  flags: %s", ", ".join(ex.flags))

    # Step 8: write fields onto the record
    apply_to_record(record, ex)
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
    for pdf in pdfs:
        try:
            ok, msg = process_pdf(pdf, records_by_doc, force=force)
        except Exception as exc:
            log.error("unexpected error on %s: %s\n%s",
                      pdf.name, exc, traceback.format_exc())
            ok, msg = False, f"exception: {exc}"

        if ok:
            if "already processed" in msg:
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

    log.info("=== summary: %d ok, %d already-done, %d failed (of %d) ===",
             n_ok, n_skipped, n_fail, len(pdfs))

    # Save updated records only if we changed something.
    if n_ok > 0 or force:
        save_records(records, envelope)
    else:
        log.info("no changes — skipping save")

    return 0 if n_fail == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
