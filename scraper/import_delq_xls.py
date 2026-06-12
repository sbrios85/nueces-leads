#!/usr/bin/env python3
# ============================================================
# Nueces County Delinquent Tax XLS importer
# ============================================================
# Reads a monthly delinquent-tax file from data/delq_uploads/,
# filters to residential-only, normalizes fields, and writes
# delq_records.json (dashboard) + delq_records.json (data) for
# the Delinquent Taxes dashboard tab.
#
# The Nueces County tax office (Sandra Rocha) publishes a fresh
# .xls file each month. We accept the .xls as-is (no manual
# conversion to CSV required). Columns are stable enough that
# the column-index mapping below works across months, but the
# importer also tolerates a header row to detect column moves
# gracefully.
#
# State property code filter: Texas Property Tax Code §1.04
# classifies real property by purpose. We keep only codes that
# represent residential property:
#   A1  single-family residence
#   A2  manufactured / mobile home
#   A4  condominium
#   B1-B9  multi-family residential (duplex/fourplex/apartment)
#   C1  vacant residential lot
# Everything else is dropped (commercial F1/F2, agricultural
# D1, rural residence E1-E5, real-property inventory O1/O2,
# personal property L1, blank-coded uncertain records).
#
# Merge model: see DELQ ingestion model in dashboard/index.html
# (search for "DELQ ingestion model"). Briefly: replace each
# month, but preserve CRM-store records by NCAD account #.
# Properties whose status was "new" and drop off the new file
# are silently removed. Properties with any other status that
# drop off the new file are KEPT (unusual, deserves user
# attention).
#
# Output JSON shape: see RECORD_KEYS comment block below.
# ============================================================
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from collections import OrderedDict
from datetime import date
from pathlib import Path
from typing import Any

try:
    import xlrd
except ImportError:
    sys.stderr.write("xlrd is required: pip install 'xlrd>=2.0,<3.0'\n")
    sys.exit(2)

# Reuse the CCLN owner classifier — it already knows how to detect
# LLCs, trusts, churches, HOAs, government, schools, religious orgs,
# nonprofits, etc. The same logic applies for delinquent-tax records:
# Sergio doesn't pursue corporate-owned leads. The module also handles
# tricky cases (ESTATE OF X LLP → keep as estate, FAMILY TRUST → keep,
# REV LVG TRUST → keep but Berkshire/etc institutional → drop).
#
# Hard import — if the classifier is missing, fail loudly rather than
# silently load corporate rows. The two scripts live in the same dir
# so a relative import works regardless of how the file is invoked.
try:
    from ccln_owner_filter import classify_owner
except ImportError:
    # Fallback for when the importer is run from outside the scraper/
    # dir (e.g. unit tests). Add the script's own dir to sys.path then
    # retry.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from ccln_owner_filter import classify_owner


# ------------------------------------------------------------
# Filter thresholds
# ------------------------------------------------------------
# Maximum market value Sergio will work. Properties at or above this
# threshold are dropped at ingestion. The $500K cap is set high
# enough to leave room for legitimate multi-family leads (B1-B9):
# a $400K duplex is two $200K units and a $480K fourplex is four
# $120K units — totally normal stock in older neighborhoods. The
# cap mostly catches luxury single-family homes (the $300-500K
# Padre Island and Country Club tier) which aren't a fit for the
# distressed-property strategy.
#
# Edit this number if the strategy changes; it's a one-line tweak
# that takes effect on next import.
MAX_MARKET_VALUE = 500_000


# ------------------------------------------------------------
# Geographic filter — Corpus Christi city limits only
# ------------------------------------------------------------
# Sergio only works leads inside the City of Corpus Christi.
# Records with a prop_zip outside this allowlist are dropped
# at ingestion. As of 2026-05-27 the county XLS contained ~29%
# non-CC records (mostly Robstown 78380, Bishop 78343, Port
# Aransas 78373, plus smaller ZIPs in Banquete/Driscoll/Agua
# Dulce/Sandia/etc.) that are now excluded.
#
# Notable exclusion: 78410 (Calallen). Calallen residents often
# self-identify as Corpus Christi, but the area is outside the
# city limits proper and Sergio chose to exclude it.
#
# Blank prop_zip is KEPT (treated as unknown rather than non-CC).
# Some legitimate CC properties have missing ZIPs in the source
# file; next month's import usually populates them. Re-evaluate
# if this turns out to add too much noise.
#
# 78403 and 78419 are included for completeness even though the
# 2026-05 file had zero records in each — they're valid CC ZIPs
# (78403 = downtown PO boxes only, 78419 = Naval Air Station).
CC_ZIPS: frozenset[str] = frozenset({
    "78401", "78402", "78403", "78404", "78405", "78406", "78407",
    "78408", "78409", "78411", "78412", "78413", "78414", "78415",
    "78416", "78417", "78418", "78419",
})


# ------------------------------------------------------------
# Paths (resolved relative to repo root, which is the parent of
# the scraper/ dir when invoked via the GitHub Action).
# ------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
UPLOAD_DIR = REPO_ROOT / "data" / "delq_uploads"
DATA_OUT = REPO_ROOT / "data" / "delq_records.json"
DASH_OUT = REPO_ROOT / "dashboard" / "delq_records.json"


# ------------------------------------------------------------
# Residential filter — Texas Property Tax Code §1.04
# ------------------------------------------------------------
# Edit this set if Sergio's market preferences change. Adding a
# code (e.g. "E1" for rural homes) means residents in those
# categories will start appearing in the dashboard next import.
# Removing a code means those rows will be dropped next import
# even if they were tracked in CRM previously (CRM data is
# preserved in dashboard storage but the row stops appearing).
KEEP_CODES: frozenset[str] = frozenset({
    "A1",
    "B1", "B2", "B3", "B4", "B5", "B6", "B7", "B8", "B9",
    "C1",
})


# ------------------------------------------------------------
# Column index mapping for the Nueces County file format
# ------------------------------------------------------------
# These indices match the file Sandra Rocha publishes (verified
# 2026-05-18). The header is row 0; data starts at row 1.
# The importer cross-checks the actual header against EXPECTED_
# HEADER below before relying on the indices, so a column
# reordering or insertion in a future file will fail loudly
# rather than silently corrupting data.
COL_ACCOUNT_NUM = 0       # ACCOUNT # (tax-office ID, primary key)
COL_APPR_DIST_NUM = 1     # APPR DIST # (NCAD account, links to other systems)
COL_ROLL_CODE = 2         # ROLL CODE
COL_STATE_PROP = 3        # STATE PROP (residential filter)
COL_PROP_DESCR1 = 4       # PROP DESCR1
COL_PROP_DESCR2 = 5       # PROP DESCR2
COL_PROP_DESCR3 = 6       # PROP DESCR3
COL_PROP_ADDR = 8         # PROP ADDR (situs address)
COL_PROP_ZIP = 9          # ZIP (situs zip)
COL_ACRES = 10            # ACRES
COL_OWNER = 11            # OWNER NAME/ADDR 1
COL_ADDR2 = 12            # ADDR2 (mail line 2)
COL_ADDR3 = 13            # ADDR3 (mail street)
COL_ADDR4 = 14            # ADDR4 (mail suite)
COL_CITY = 15             # CITY (mail city)
COL_STATE = 16            # STATE (mail state)
COL_ZIP2 = 17             # ZIP2 (mail zip)
COL_HS_LAND = 18          # HS LAND VALUE (homestead land)
COL_HS_IMP = 19           # HS IMP VALUE (homestead improvement)
COL_NON_HS_LAND = 20      # NON HS LAND
COL_NON_HS_IMP = 21       # NON HS IMP
COL_AG_LAND = 22          # AG LAND
COL_AG_IMP = 23           # AG IMP
COL_SUIT_JUDGEMENT = 25   # SUIT/JUDGEMENT FLAG
COL_BANKRUPTCY = 26       # BANKR FLAG
COL_BAD_ADDR = 28         # BAD ADDR FLAG
COL_TAX_DEF = 29          # TAX DEF CODE
COL_PAY_AGREE = 30        # MOST CURR PAYM AGREE CODE
COL_EX_START = 31         # EX 1 (first of 6 consecutive exemption columns)
COL_EX_END = 37           # exclusive end (EX 1..EX 6 = cols 31..36)
COL_CURRENT_LEVY = 61     # 2025 CURR LEVY BAL (current-year levy owed)
COL_DEL_LEVY = 62         # DEL LEVY BAL (back-year delinquent owed)
COL_DEL_YEARS = 63        # DEL YEARS (e.g. "25,24,23,22")


# Expected header text (first column) used as a sanity check.
# The header row in the actual file uses inconsistent trailing
# spaces, so we strip both sides before comparing.
EXPECTED_HEADER: tuple[tuple[int, str], ...] = (
    (COL_ACCOUNT_NUM,     "ACCOUNT #"),
    (COL_APPR_DIST_NUM,   "APPR DIST #"),
    (COL_STATE_PROP,      "STATE PROP"),
    (COL_PROP_ADDR,       "PROP ADDR"),
    (COL_OWNER,           "OWNER NAME/ADDR 1"),
    (COL_CURRENT_LEVY,    "2025 CURR LEVY BAL"),   # year prefix may
                                                   # change across files;
                                                   # we tolerate that
                                                   # below.
    (COL_DEL_LEVY,        "DEL LEVY BAL"),
    (COL_DEL_YEARS,       "DEL YEARS"),
)


# ------------------------------------------------------------
# Output record shape — written to delq_records.json
# ------------------------------------------------------------
# RECORD_KEYS (for reference / documentation only):
#
#   ncad_account_num     "0487-0009-0160" 12-digit canonical (col 0)
#   ncad_prop_id         "237084"         NCAD internal PID  (col 1)
#   state_prop_code      "A1"             residential subcategory
#   owner                "STARTZ DANIEL AND CHRISTIAN STARTZ"
#   prop_address         "15326 BONASSE CT-703"
#   prop_zip             "78418"
#   mail_address         "13842 MIZZEN ST"
#   mail_address2        ""              suite/unit if present
#   mail_city            "CORPUS CHRISTI"
#   mail_state           "TX"
#   mail_zip             "78418-6955"
#   legal                "ADMIRALS POINT CONDO, UNIT 703 BLDG B..."
#   acres                0.0548          float, 0 if blank
#   current_levy_owed    2960.49         dollars, this year's bill
#   back_levy_owed       19682.65        dollars, prior years
#   total_owed           22643.14        derived sum (rounded to cents)
#   del_years            [25, 24, 23, 22]  list of int years behind
#   years_behind         4               derived = len(del_years)
#   oldest_year          22              derived = min(del_years), used
#                                        for chronicity sorting
#   market_value         302095          sum of HS/NHS/AG land+imp
#   exemptions           ["HOMESTEAD", "OVER 65"]  list of strings
#   flags                {               compact status flags
#     "in_suit": True,                   lawsuit filed (col 25 = 'L')
#     "has_judgment": False,             judgment entered (col 25 = 'J')
#     "bad_address": False,
#     "tax_deferral": False,
#     "payment_agreement": "AC"          empty string if no agreement
#   }
#   _first_seen          "2026-05-18"    set on first import; preserved
#                                        across re-imports
#   _last_seen           "2026-05-18"    updated every import
#
# Note: bankruptcy records are KEPT and carry a "bankruptcy":True
# flag (like suit/judgment/deferral) so they surface with context
# rather than being silently dropped.
# Adjust _process_row if that ever changes.
#
# Note: the county XLS labels column 0 as "ACCOUNT #" — this is
# the tax-office's working form of the NCAD account number, NOT
# a separate identifier. We canonicalize it into ncad_account_num
# (12-digit dashed). Verified by cross-reference with CCLN data
# for DREIER LAWRENCE properties. No second tax-office ID exists
# in this file; column 1 ("APPR DIST #") is NCAD's internal PID.
# ------------------------------------------------------------


# ------------------------------------------------------------
# Logger
# ------------------------------------------------------------
log = logging.getLogger("import_delq_xls")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)


# ------------------------------------------------------------
# Helpers
# ------------------------------------------------------------
def _clean(v: Any) -> str:
    """Strip strings, '' for blank cells, ints/floats stringified."""
    if v is None:
        return ""
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def _num(v: Any) -> float:
    """Coerce numeric cells to float; return 0.0 for non-numeric."""
    if isinstance(v, (int, float)):
        return float(v)
    s = _clean(v).replace(",", "").replace("$", "")
    try:
        return float(s)
    except (ValueError, TypeError):
        return 0.0


def _ncad_canonical(raw: Any) -> str:
    """
    Convert NCAD account number to canonical 12-digit dashed form
    'NNNN-NNNN-NNNN' used everywhere else in the dashboard.

    The XLS stores ACCOUNT # (column 0) as the tax-office's working
    form of the NCAD account number — an int between 11-12 digits
    stored as a float (e.g. 48700090160.0 → '48700090160' → padded
    to 12 digits = '0487-0009-0160'). Verified 2026-05-26 by cross-
    referencing with known DREIER LAWRENCE properties whose NCAD
    accounts we already track in city_liens.json:
        5310 WILLIAMS DR → 2711-0012-0140  ✓ matches CCLN data
        7013 EDGEBROOK DR → 1653-0001-0160  ✓ matches CCLN data
        909 BROCK DR → 6320-0007-0110  ✓ matches CCLN data
    """
    s = _clean(raw)
    if not s:
        return ""
    digits = re.sub(r"\D", "", s)
    if not digits:
        return ""
    # Pad to 12 digits from the LEFT.
    digits = digits.zfill(12)
    if len(digits) != 12:
        log.warning("NCAD account %r has %d digits, expected 12 — "
                    "truncating to last 12", s, len(digits))
        digits = digits[-12:]
    return f"{digits[0:4]}-{digits[4:8]}-{digits[8:12]}"


def _ncad_prop_id(raw: Any) -> str:
    """
    Convert the APPR DIST # column (NCAD's internal property ID,
    used in NCAD URLs like /Property/View/{prop_id}) to a stripped
    integer string. Distinct from the NCAD account number — this
    is just NCAD's row-level primary key in their own database.
    Example: 237084 → '237084'.
    """
    s = _clean(raw)
    if not s:
        return ""
    digits = re.sub(r"\D", "", s)
    return digits or ""


def _market_value(row) -> int:
    """
    Sum HS_LAND + HS_IMP + NON_HS_LAND + NON_HS_IMP + AG_LAND +
    AG_IMP, then round to the nearest $100.

    Why round: tax-assessed market values are estimates with
    inherent +/- $1000 uncertainty anyway, so the trailing 1-2
    digits ($302,095 vs $302,100) are noise. Rounding to $100
    makes the dashboard easier to scan visually AND shrinks the
    JSON file by ~50 KB across 11.5k records (every market_value
    value becomes 1-2 chars shorter on average). Smaller numbers
    also compress better when the dashboard is served over the
    wire.
    """
    total = 0.0
    for col in (COL_HS_LAND, COL_HS_IMP, COL_NON_HS_LAND,
                COL_NON_HS_IMP, COL_AG_LAND, COL_AG_IMP):
        total += _num(row[col])
    # Round to nearest $100. int(round(...)) handles the .5 case
    # consistently (banker's rounding) which is fine here.
    return int(round(total / 100.0)) * 100


def _legal_description(row) -> str:
    """Concatenate PROP_DESCR1..3 with single spaces, strip blanks."""
    parts = [_clean(row[c]) for c in
             (COL_PROP_DESCR1, COL_PROP_DESCR2, COL_PROP_DESCR3)]
    return " ".join(p for p in parts if p)


def _parse_del_years(raw: Any) -> list[int]:
    """
    Parse the DEL YEARS comma list (e.g. '25,24,23,22') into a
    sorted descending list of int years. Returns [] if blank or
    if the field is a single 0 (some rows have 0 as a placeholder).
    """
    s = _clean(raw)
    if not s or s in ("0", "0.0"):
        return []
    parts = [p.strip() for p in s.split(",") if p.strip()]
    years: list[int] = []
    for p in parts:
        # Strip non-digit (e.g. trailing space artifacts) and parse.
        digits = re.sub(r"\D", "", p)
        if digits:
            try:
                years.append(int(digits))
            except ValueError:
                pass
    # Deduplicate and sort descending (most recent year first).
    return sorted(set(years), reverse=True)


def _exemptions(row) -> list[str]:
    """Collect non-blank exemption fields (EX 1..EX 6)."""
    out: list[str] = []
    for col in range(COL_EX_START, COL_EX_END):
        v = _clean(row[col])
        if v:
            # Some cells are padded with trailing spaces of the
            # column width; collapse internal whitespace.
            v = re.sub(r"\s+", " ", v)
            out.append(v)
    return out


def _flags(row) -> dict[str, Any]:
    """
    Build the compact flags dict from various flag/code columns.

    SUIT/JUDGEMENT FLAG (col 25) carries 3 distinct states in the
    Nueces County file format, encoded as a single character:
        'L' = lawsuit filed (county has sued, awaiting judgment)
        'J' = judgment entered (county won; foreclosure imminent)
        'A' = unclear (only 2 records in current file)
        blank = no suit
    We split L/J into two separate flags because the judgment case
    is significantly more actionable (closer to tax sale) than a
    pending suit. 'A' is too rare to model — we ignore it.

    Note: "bankruptcy" is included here. Bankruptcy rows are KEPT
    (not dropped) and flagged so the operator can see a property
    owes taxes even while it's in bankruptcy, and decide how to
    handle it. See _process_row.
    """
    suit_flag = _clean(row[COL_SUIT_JUDGEMENT]).upper()
    return {
        "in_suit":           suit_flag == "L",
        "has_judgment":      suit_flag == "J",
        "bad_address":       bool(_clean(row[COL_BAD_ADDR])),
        "tax_deferral":      bool(_clean(row[COL_TAX_DEF])),
        "payment_agreement": _clean(row[COL_PAY_AGREE]),
        "bankruptcy":        bool(_clean(row[COL_BANKRUPTCY])),
    }


def _validate_header(actual_header: list[Any]) -> None:
    """
    Sanity-check that the column positions still match what the
    importer expects. Raises ValueError on mismatch. The
    "2025 CURR LEVY BAL" header may shift year-to-year (will be
    "2026 CURR LEVY BAL" next year), so for that column we only
    require the substring "CURR LEVY BAL".
    """
    for col_idx, expected in EXPECTED_HEADER:
        if col_idx >= len(actual_header):
            raise ValueError(
                f"Column index {col_idx} (expected {expected!r}) is past "
                f"end of header (header has {len(actual_header)} cols)."
            )
        actual = _clean(actual_header[col_idx]).upper()
        expected_upper = expected.upper()

        # Tolerate year drift in the current-levy column header.
        if "CURR LEVY BAL" in expected_upper:
            if "CURR LEVY BAL" not in actual:
                raise ValueError(
                    f"Column {col_idx}: expected header containing "
                    f"'CURR LEVY BAL', got {actual!r}."
                )
            continue

        if actual != expected_upper:
            raise ValueError(
                f"Column {col_idx}: expected {expected!r}, got "
                f"{actual_header[col_idx]!r}. The county may have "
                f"changed the file format; review column indices."
            )


def _process_row(row: list[Any]) -> dict[str, Any] | None:
    """
    Convert one raw XLS row into a slim JSON record, OR return
    None if the row should be filtered out:
      - non-residential state code (per KEEP_CODES)
      - missing NCAD account number (can't track)
      - non-CC ZIP code (per CC_ZIPS; blank ZIP is kept)
      - corporate / government / institutional owner (per CCLN
        owner classifier — same rules as CCLN ingestion)
      - market value at or above MAX_MARKET_VALUE (too expensive
        for the distressed-property strategy)
    """
    state_prop = _clean(row[COL_STATE_PROP]).upper()
    if state_prop not in KEEP_CODES:
        return None

    ncad = _ncad_canonical(row[COL_ACCOUNT_NUM])
    if not ncad:
        return None

    # Geographic filter — keep only City of Corpus Christi ZIPs.
    # Blank ZIPs are kept (unknown, not non-CC). See CC_ZIPS comment.
    # ~29% of the residential file in 2026-05 (Robstown / Bishop /
    # Port Aransas / Calallen / Banquete / Driscoll / smaller).
    prop_zip = _clean(row[COL_PROP_ZIP])
    if prop_zip and prop_zip not in CC_ZIPS:
        return None

    # Corporate-owner filter — reuses the CCLN classifier (same
    # rules across the dashboard so behavior is consistent).
    # classify_owner returns (kind, keep): we just need `keep`.
    # Drops LLCs, INCs, churches, HOAs, government, schools,
    # nonprofits, institutional trusts. ~1,219 records out of 11.5k
    # residential in 2026-05 (10.6% of file).
    owner = _clean(row[COL_OWNER])
    if owner:
        _, keep_owner = classify_owner(owner)
        if not keep_owner:
            return None

    del_years = _parse_del_years(row[COL_DEL_YEARS])
    current_levy = _num(row[COL_CURRENT_LEVY])
    back_levy = _num(row[COL_DEL_LEVY])
    market_value = _market_value(row)

    # Market-value cap — drop high-value properties before building
    # the full record. ~7-8% of the residential file. See comment
    # on MAX_MARKET_VALUE above for rationale.
    if market_value >= MAX_MARKET_VALUE:
        return None

    rec = {
        "ncad_account_num":   ncad,
        "ncad_prop_id":       _ncad_prop_id(row[COL_APPR_DIST_NUM]),
        "state_prop_code":    state_prop,
        "owner":              owner,
        "prop_address":       _clean(row[COL_PROP_ADDR]),
        "prop_zip":           _clean(row[COL_PROP_ZIP]),
        "mail_address":       _clean(row[COL_ADDR3]),
        "mail_address2":      _clean(row[COL_ADDR4]),
        "mail_city":          _clean(row[COL_CITY]),
        "mail_state":         _clean(row[COL_STATE]),
        "mail_zip":           _clean(row[COL_ZIP2]),
        "legal":              _legal_description(row),
        "acres":              _num(row[COL_ACRES]),
        "current_levy_owed":  round(current_levy, 2),
        "back_levy_owed":     round(back_levy, 2),
        "total_owed":         round(current_levy + back_levy, 2),
        "del_years":          del_years,
        "years_behind":       len(del_years),
        "oldest_year":        min(del_years) if del_years else None,
        "market_value":       market_value,
        "exemptions":         _exemptions(row),
        "flags":              _flags(row),
    }
    return rec


# ------------------------------------------------------------
# Import driver
# ------------------------------------------------------------
def _find_xls() -> Path:
    """
    Find the .xls file in data/delq_uploads/. If there's more
    than one, pick the most-recently-modified (the workflow
    convention is "drop the new file in, run import — the older
    one will be picked up next month or pruned manually").
    Raises FileNotFoundError if none exist.
    """
    if not UPLOAD_DIR.exists():
        raise FileNotFoundError(
            f"Upload directory does not exist: {UPLOAD_DIR}\n"
            "Create it and drop the monthly .xls file into it."
        )
    candidates = sorted(
        list(UPLOAD_DIR.glob("*.xls")) + list(UPLOAD_DIR.glob("*.xlsx")),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise FileNotFoundError(
            f"No .xls or .xlsx file found in {UPLOAD_DIR}. Upload the "
            "county's monthly delinquent-tax file there first."
        )
    if len(candidates) > 1:
        log.warning("Multiple XLS files found in upload dir; using "
                    "most-recent: %s", candidates[0].name)
    return candidates[0]


def _read_xls(path: Path) -> list[dict[str, Any]]:
    """Read the XLS and return the filtered, normalized record list."""
    log.info("Opening %s (size: %.1f MB)", path.name,
             path.stat().st_size / 1024 / 1024)
    book = xlrd.open_workbook(str(path))
    if book.nsheets != 1:
        log.warning("Expected 1 sheet, got %d — using sheet 0 (%s)",
                    book.nsheets, book.sheet_by_index(0).name)
    sheet = book.sheet_by_index(0)
    log.info("Sheet '%s': %d rows × %d cols", sheet.name,
             sheet.nrows, sheet.ncols)

    if sheet.nrows < 2:
        log.error("Sheet has fewer than 2 rows — no data to import")
        return []

    header = sheet.row_values(0)
    _validate_header(header)
    log.info("Header validated")

    records: list[dict[str, Any]] = []
    # Per-reason drop counters — surfaced in the log line at the end
    # so the operator can see at a glance WHY rows were dropped each
    # month. Counters are mutually exclusive (each row counts toward
    # at most one bucket, evaluated in priority order matching the
    # filter checks in _process_row).
    drop_non_residential = 0
    drop_no_ncad = 0
    drop_non_cc_zip = 0
    drop_corporate = 0
    drop_high_value = 0
    kept_bankruptcy = 0   # informational: KEPT records carrying the bankruptcy flag

    for r in range(1, sheet.nrows):
        row = sheet.row_values(r)
        rec = _process_row(row)
        if rec is not None:
            records.append(rec)
            if _clean(row[COL_BANKRUPTCY]):
                kept_bankruptcy += 1
            continue

        # Row was filtered — figure out why so we can report it.
        # Mirrors the checks in _process_row in the same priority
        # order. Cheap re-checks; no field parsing.
        state_prop = _clean(row[COL_STATE_PROP]).upper()
        if state_prop not in KEEP_CODES:
            drop_non_residential += 1
            continue
        if not _ncad_canonical(row[COL_ACCOUNT_NUM]):
            drop_no_ncad += 1
            continue
        prop_zip = _clean(row[COL_PROP_ZIP])
        if prop_zip and prop_zip not in CC_ZIPS:
            drop_non_cc_zip += 1
            continue
        owner = _clean(row[COL_OWNER])
        if owner:
            _, keep_owner = classify_owner(owner)
            if not keep_owner:
                drop_corporate += 1
                continue
        # If we got here, the only reason left is the value cap.
        if _market_value(row) >= MAX_MARKET_VALUE:
            drop_high_value += 1
            continue
        # Theoretically unreachable: _process_row returned None but
        # none of the above filters triggered. Count it generically.
        drop_non_residential += 1   # closest catch-all bucket

    total_dropped = (drop_non_residential +
                     drop_no_ncad + drop_non_cc_zip +
                     drop_corporate + drop_high_value)
    log.info("Processed %d data rows: %d kept (%d flagged bankruptcy)",
             sheet.nrows - 1, len(records), kept_bankruptcy)
    log.info("  dropped: non-residential=%d, "
             "no_ncad=%d, non_cc_zip=%d, corporate=%d, "
             "market_value>=$%d=%d  (total=%d)",
             drop_non_residential, drop_no_ncad,
             drop_non_cc_zip, drop_corporate,
             MAX_MARKET_VALUE, drop_high_value,
             total_dropped)
    return records


def _load_existing(path: Path) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """
    Load the existing delq_records.json if present, return
    (by_ncad_account, metadata). Returns ({}, {}) if file is
    missing or unreadable — first import is normal.
    """
    if not path.exists():
        return {}, {}
    try:
        with path.open() as f:
            payload = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        log.warning("Could not read existing %s (%s); treating as first "
                    "import", path, e)
        return {}, {}
    records = payload.get("records") if isinstance(payload, dict) else None
    if not isinstance(records, list):
        log.warning("Existing file has unexpected shape; treating as "
                    "first import")
        return {}, {}
    by_id = {r["ncad_account_num"]: r for r in records
             if isinstance(r, dict) and r.get("ncad_account_num")}
    meta = {k: v for k, v in payload.items() if k != "records"}
    log.info("Loaded existing snapshot: %d records", len(by_id))
    return by_id, meta


def _merge_with_existing(
    new_records: list[dict[str, Any]],
    existing_by_id: dict[str, dict[str, Any]],
    today_iso: str,
) -> list[dict[str, Any]]:
    """
    Apply the merge model: see "DELQ ingestion model" docs.

    Rules:
      - Every record from the new XLS appears in the output.
      - If the record exists in the prior snapshot, copy forward
        `_first_seen` (so we keep the original first-seen date)
        and stamp `_last_seen` = today.
      - If the record is brand new (no prior snapshot entry),
        stamp both `_first_seen` and `_last_seen` = today.
      - Status/notes/contacts live in dashboard storage (not in
        this file), keyed by ncad_account_num. Nothing to do here.
      - Records that exist in the OLD snapshot but NOT in the new
        XLS are silently dropped (their CRM data in dashboard
        storage is preserved; the dashboard's display logic
        handles the "was working it, now off the list" case).

    The "kept because status was non-new" case (model rule 3b)
    is handled in the dashboard, not here — this importer cannot
    see the CRM store. The dashboard's renderer merges its CRM
    store with the JSON output and decides what to show.
    """
    merged: list[dict[str, Any]] = []
    new_count = 0
    seen_again_count = 0
    new_ids = {r["ncad_account_num"] for r in new_records}
    for rec in new_records:
        ncad = rec["ncad_account_num"]
        prior = existing_by_id.get(ncad)
        if prior:
            rec["_first_seen"] = prior.get("_first_seen", today_iso)
            seen_again_count += 1
        else:
            rec["_first_seen"] = today_iso
            new_count += 1
        rec["_last_seen"] = today_iso
        merged.append(rec)

    # Diagnostic counts for the operator.
    dropped_count = sum(1 for ncad in existing_by_id if ncad not in new_ids)
    log.info(
        "Merge summary: %d new this month, %d still delinquent, "
        "%d dropped off (paid or otherwise removed by the county)",
        new_count, seen_again_count, dropped_count,
    )
    return merged


def _write_output(records: list[dict[str, Any]],
                  src_filename: str,
                  today_iso: str) -> None:
    """Write delq_records.json to both data/ and dashboard/ paths."""
    payload = OrderedDict([
        ("source_filename",  src_filename),
        ("import_date",      today_iso),
        ("record_count",     len(records)),
        ("total_owed",       round(sum(r["total_owed"] for r in records), 2)),
        ("filter_codes",     sorted(KEEP_CODES)),
        ("records",          records),
    ])
    # Use compact JSON (no indentation) to save ~25% of file size.
    # The file is consumed by the dashboard at runtime, not edited
    # by hand, so readability isn't important. At ~12k records,
    # indented JSON is ~12 MB; compact is ~9 MB — a meaningful
    # difference for page-load speed.
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    for out_path in (DATA_OUT, DASH_OUT):
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text)
        size_kb = out_path.stat().st_size / 1024
        log.info("Wrote %s (%.1f KB)", out_path, size_kb)


# ------------------------------------------------------------
# CLI
# ------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--dry-run", action="store_true",
        help="Process the XLS and report stats, but don't write the JSON.",
    )
    p.add_argument(
        "--file", default=None,
        help="Path to a specific .xls file. Default: most recent in "
             "data/delq_uploads/",
    )
    args = p.parse_args(argv)

    src = Path(args.file) if args.file else _find_xls()
    new_records = _read_xls(src)
    if not new_records:
        log.error("No records to write")
        return 1

    today_iso = date.today().isoformat()

    existing_by_id, _ = _load_existing(DATA_OUT)
    merged = _merge_with_existing(new_records, existing_by_id, today_iso)

    if args.dry_run:
        total_owed = sum(r["total_owed"] for r in merged)
        log.info("DRY RUN — would write %d records totaling $%s",
                 len(merged), f"{total_owed:,.2f}")
        return 0

    _write_output(merged, src.name, today_iso)
    log.info("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
