"""
CCLN owner classifier — decides if an owner name is "corporate"
(should be excluded) or "individual / family / estate" (keep).

================================================================
Why this exists
================================================================
The City of Corpus Christi files liens against properties. Many of
those properties are owned by LLCs, INC, corporations, churches,
HOAs, schools, etc. These entities almost never produce motivated-
seller deals — companies sell through brokers and procurement
processes, not response to direct outreach.

To keep the dashboard focused on actionable leads, we exclude these
ownership types at three places:

1. backfill_city_liens.py — when ingesting from the clerk portal,
   skip any record whose owner classifies as corporate.
2. extract_ccln_pdfs.py — when an uploaded PDF's owner is corporate,
   delete the corresponding record (PDF data confirms the company
   ownership beyond doubt).
3. One-shot cleanup tool — sweep the existing city_liens.json and
   delete any records that classify as corporate.

================================================================
Classification taxonomy
================================================================
KEEP:
  "individual"    — default; person's name (e.g. "SMITH JOHN")
  "estate"        — deceased owner; heirs may be motivated
                    (e.g. "ESTATE OF JOHN SMITH", "SMITH JANE DECD")
  "family_trust"  — estate-planning vehicle for individuals
                    (e.g. "JANE DOE LIVING TRUST",
                    "BRIDGES FAMILY TRUST")

EXCLUDE:
  "company"       — LLC, INC, CORP, LP, etc.
  "trust_inst"    — institutional trust (e.g. "ABC LAND TRUST")
  "religious"     — church, ministry, etc.
  "school"        — ISD, college, university
  "government"    — city, county, state, federal
  "nonprofit"     — foundation, association, society
  "hoa"           — homeowners association

The classify_owner() function returns (kind, keep) where kind is the
specific label above and keep is a boolean. Use ``keep`` for filter
logic; use ``kind`` for logging / dashboard tooltips so the user can
see WHY a record was filtered.

================================================================
False-positive handling
================================================================
A few common surnames happen to be company keywords ("CARLA BANK",
"JOHN BAPTIST JR"). The classifier uses a heuristic: if the name is
exactly 2 tokens (ignoring generational suffixes JR/SR/etc.) and one
of them is a collision-token, treat as individual. This protects
real-person surnames from being wrongly excluded.

For ambiguous edge cases at the boundary (e.g. "SMITH PROPERTIES"
where "PROPERTIES" could be a surname or a company suffix), the
classifier biases TOWARD exclusion. The bulk-cleanup tool's "hard
delete" mode means false exclusions are unrecoverable, so this is a
known trade-off: we accept losing a tiny number of real leads to
filter out a much larger number of corporate noise.
"""

from __future__ import annotations

import re
from typing import Tuple


# ----------------------------------------------------------------
# Pattern definitions (in priority order; first match wins)
# ----------------------------------------------------------------

# Estates dominate other markers — "ESTATE OF JOHN SMITH TRUST" is
# still an estate (someone died, heirs are likely the actual contacts).
RE_ESTATE = re.compile(
    r"\b(ESTATE\s+OF|DECEASED|DECD)\b",
    re.I,
)

# Family/personal trusts. The keywords here are SPECIFIC trust types
# associated with individuals' estate planning. Generic "TRUST" alone
# doesn't qualify — see RE_TRUST_ANY below.
RE_TRUST_FAMILY = re.compile(
    r"\b(REVOCABLE\s+LIVING\s+TRUST"
    r"|LIVING\s+TRUST"
    r"|FAMILY\s+TRUST"
    r"|REVOCABLE\s+TRUST"
    r"|TESTAMENTARY\s+TRUST"
    r"|INTER\s+VIVOS\s+TRUST"
    r"|MARITAL\s+TRUST"
    r"|BYPASS\s+TRUST"
    r"|SURVIVOR'?S?\s+TRUST)\b",
    re.I,
)

# Generic TRUST — after exhausting family-trust patterns, ANY remaining
# TRUST is treated as institutional and excluded.
RE_TRUST_ANY = re.compile(r"\bTRUST\b", re.I)

# Government check goes first (before religious/nonprofit) because
# some government entities include words like "DEPARTMENT" or
# "ASSOCIATION" that would otherwise be caught by other patterns.
RE_GOVERNMENT = re.compile(
    r"\b(CITY\s+OF"
    r"|COUNTY\s+OF"
    r"|STATE\s+OF"
    r"|UNITED\s+STATES"
    r"|US\s+DEPT"
    r"|U\.?S\.?\s+DEPARTMENT"
    r"|FEDERAL\s+GOVERNMENT"
    r"|DEPT\s+OF"
    r"|DEPARTMENT\s+OF"
    r"|MUNICIPAL"
    # Added 2026-05-27 — "<X> COUNTY" pattern. Texas counties
    # (and others) own foreclosed properties through their
    # appointed trustee. "NUECES COUNTY TRUSTEE", "TRAVIS COUNTY",
    # etc. all need to be filtered. The pre-existing "COUNTY OF X"
    # caught some but not the more common "X COUNTY" ordering.
    # We use [A-Z]+ before COUNTY to avoid matching things like
    # "MY COUNTY HOME" (which doesn't have a real county name).
    r"|[A-Z]+\s+COUNTY\b"
    # Common government-trustee patterns. We do NOT match bare
    # "TRUSTEE" at end of name because in production data, many
    # such records are real people acting as private trustees
    # for family trusts (e.g. "BURKETT STEPHEN L TRUSTEE",
    # "ROSENBERG JULIUS M TRUSTEE") and ARE valid leads.
    r"|TAX\s+TRUSTEE"
    r")\b",
    re.I,
)

RE_SCHOOL = re.compile(
    r"\b(ISD|SCHOOL\s+DISTRICT|COLLEGE|UNIVERSITY|ACADEMY)\b",
    re.I,
)

RE_RELIGIOUS = re.compile(
    r"\b(CHURCH"
    r"|MINISTRY|MINISTRIES"
    r"|ASSEMBLY\s+OF\s+GOD"
    r"|BAPTIST|METHODIST|CATHOLIC|DIOCESE"
    r"|TEMPLE|SYNAGOGUE|MOSQUE"
    r"|PRESBYTERIAN|LUTHERAN|EPISCOPAL|PENTECOSTAL)\b",
    re.I,
)

# HOA checked before NONPROFIT (more specific match).
RE_HOA = re.compile(
    r"\b(HOA"
    r"|HOMEOWNERS\s+ASSOCIATION"
    r"|PROPERTY\s+OWNERS\s+ASSOCIATION"
    r"|CONDOMINIUM\s+ASSOCIATION"
    r"|CONDO\s+ASSOC)\b",
    re.I,
)

RE_NONPROFIT = re.compile(
    # First alternation: word-boundary-anchored keywords that look
    # like normal words (ASSOCIATION, FOUNDATION, etc).
    r"(?:\b(?:ASSOCIATION"
    r"|FOUNDATION"
    r"|INSTITUTE"
    r"|SOCIETY"
    r"|HABITAT"
    r"|NONPROFIT"
    r"|COMMUNITY\s+CENTER"
    r"|YMCA|YWCA"
    r"|GOODWILL"
    r"|SALVATION\s+ARMY"
    r"|TAX[\s-]?EXEMPT)\b"
    # Second alternation: IRS tax-exempt classifications. These
    # have literal parentheses which break \b word boundaries
    # (paren isn't a word char), so they sit outside the \b
    # wrapper. "501(C)(3)" / "501(C)(4)" / "501C3" all variants.
    # Added 2026-05-27. Example: "EVERHOME CREATIVE HOUSING A
    # 501(C)(3) NO" — clearly a nonprofit, was slipping by.
    r"|501\s*\(\s*C\s*\)"
    r"|\b501\s*C\s*3\b"
    r")",
    re.I,
)

# Companies. Word-boundary anchored to avoid false matches like
# "BANKHEAD" → BANK, "CORPORALE" → CORP, "INCH" → INC.
RE_COMPANY = re.compile(
    r"\b("
    r"LLC|L\.L\.C\.?|L\s+L\s+C"
    r"|INC|INC\.|INCORPORATED"
    r"|CORP|CORP\.|CORPORATION"
    r"|LTD|LTD\.|LIMITED"
    r"|LP|L\.P\.?|LLP|L\.L\.P\.?"
    r"|PLLC|P\.L\.L\.C\.?"
    r"|ENTERPRISES?"
    r"|HOLDINGS?"
    r"|PROPERTIES"
    r"|GROUP"
    r"|COMPANY"
    r"|&\s*CO\.?|CO\."
    r"|BANK"
    r"|CREDIT\s+UNION"
    r"|FEDERAL\s+SAVINGS"
    r"|PARTNERSHIP"
    # Added 2026-05-27 — patterns observed across CCLN+DELQ data
    # that were slipping through classification as "individual"
    # because they don't end in LLC/INC/CORP. All of these were
    # documented in the "deferred classifier improvements" memory
    # before being batched into this update.
    r"|INVESTMENTS?"          # MARKMAN BROTHERS INVESTMENTS, HLN INVESTMENTS
    r"|INVESTORS?"            # G&G TEXAS INVESTORS
    r"|REALTY|REALITY"        # BAY AREA REALTY (and the OCR typo "REALITY")
    r"|RAILROAD"              # UNION PACIFIC RAILROAD
    r"|MAC\s+SLST"            # FREDDIE MAC SLST 2022-2 (Freddie Mac
                              # securitization trusts) — be specific
                              # so we don't trigger on names like
                              # "MAC SMITH" (a person's name).
    r"|LODGE"                 # FENIX LODGE 24 (fraternal orgs)
    r"|TRAILER\s+PARK"        # SAN SIMEON TRAILER PARK
    r"|MOBILE\s+HOME\s+PARK"  # variant for completeness
    r"|COMMUNITY\s+IMPROVEMENT"  # CC COMMUNITY IMPROVEMENT
    # Added 2026-05-27 — additional slip-through fixes:
    # Fused INC suffix (data-entry error pattern in the county
    # files): "OPERATINGINC" without a space. Match any 5+ char
    # word ending in INC — the 5-char minimum avoids matching
    # short personal names that happen to contain "inc" letters
    # (VINCE, PRINCE, MINNIE are all <5 chars before the "inc",
    # and don't actually END with INC anyway).
    r"|\w{5,}INC\b"
    # Industry-CO patterns: bare "CO" at the end of names like
    # "ABTEX BRINKERHOFF OIL CO" without a period. Adding a
    # generic \bCO\b would false-positive surnames (FELICIA CO),
    # so we anchor on common industry/trade words before CO.
    r"|\b(?:OIL|GAS|ENERGY|TRADING|SUPPLY|RANCH|CATTLE|TIMBER"
    r"|DRILLING|MINING|LUMBER|LAND|REAL\s+ESTATE)\s+CO\b"
    r")\b",
    re.I,
)


# ----------------------------------------------------------------
# Surname-collision allowlist
# ----------------------------------------------------------------
# Words that ARE company keywords but ALSO appear as real surnames.
# When a name is exactly 2 tokens (a typical "FIRSTNAME LASTNAME" or
# "LASTNAME FIRSTNAME" shape) and one token is in this set, treat
# the name as personal regardless of keyword match. This prevents
# false-positive exclusion of "CARLA BANK" or "JOHN BAPTIST JR".
SURNAME_COLLISION_TOKENS = {
    "BANK", "BANKS",
    "COMPANY",
    "BAPTIST",
    "CHURCH",
    "TEMPLE",
    "ACADEMY",
    "BISHOP",
    # Added 2026-05-27 alongside the new company keywords:
    # "LODGE" is a real surname (Henry Cabot Lodge), so we keep
    # 2-token names like "LODGE CHARLES" as individuals.
    #
    # INVESTMENT/INVESTOR/REALTY are NOT in this allowlist on
    # purpose — they're functionally never first/last names. A
    # 2-token name like "HLN INVESTMENTS" is overwhelmingly a
    # company, not a person. Adding them here was tried and
    # produced false negatives (HLN INVESTMENTS got kept). If
    # you ever find a real person whose name is "INVESTMENT
    # WILLIAMS" or similar, mark them as Dead in the dashboard
    # — it's far rarer than the companies these patterns catch.
    "LODGE",
}

# Generational suffix — stripped before counting tokens so
# "JOHN BAPTIST JR" still matches the 2-token shape.
RE_GEN_SUFFIX = re.compile(
    r"\s+(JR|SR|II|III|IV|V|JR\.|SR\.)$",
    re.I,
)


def _looks_like_personal_name_with_collision_surname(upper: str) -> bool:
    """Heuristic override for 2-token names whose surname matches a
    company keyword. Clerk-portal owner names are typically 2 tokens
    ("LASTNAME FIRSTNAME"); real company names are usually 3+ tokens
    with the suffix at the end ("XYZ HOLDINGS COMPANY"). Returns True
    if the name fits the personal-name shape AND contains a known
    collision token."""
    stripped = RE_GEN_SUFFIX.sub("", upper)
    tokens = stripped.split()
    if len(tokens) != 2:
        return False
    return any(t in SURNAME_COLLISION_TOKENS for t in tokens)


# ----------------------------------------------------------------
# Public classifier
# ----------------------------------------------------------------
KEEP_KINDS = {"individual", "estate", "family_trust"}
EXCLUDE_KINDS = {"company", "trust_inst", "religious", "school",
                 "government", "nonprofit", "hoa"}


def classify_owner(name: str) -> Tuple[str, bool]:
    """Classify an owner name. Returns (kind, keep) where:
      kind: one of the labels above (estate / family_trust / company /
            religious / school / government / nonprofit / hoa /
            trust_inst / individual)
      keep: True if this owner is a viable lead, False if corporate

    Empty / unrecognized names default to (individual, True) — safer
    to keep than to delete on uncertainty.
    """
    if not name or not name.strip():
        return ("individual", True)
    upper = name.upper().strip()

    # 1) Estates dominate every other classification
    if RE_ESTATE.search(upper):
        return ("estate", True)

    # 2) Family trusts (specific patterns) before generic trust
    if RE_TRUST_FAMILY.search(upper):
        return ("family_trust", True)

    # 3) Government / school first — these need to be tagged with
    #    the more specific reason even if they happen to contain
    #    keywords from other categories.
    if RE_GOVERNMENT.search(upper):
        return ("government", False)
    if RE_SCHOOL.search(upper):
        return ("school", False)

    # 4) Personal-name collision override — catches 2-token names
    #    with collision surnames BEFORE they hit religious/company.
    if _looks_like_personal_name_with_collision_surname(upper):
        return ("individual", True)

    # 5) Specific organization types
    if RE_RELIGIOUS.search(upper):
        return ("religious", False)
    if RE_HOA.search(upper):
        return ("hoa", False)
    if RE_NONPROFIT.search(upper):
        return ("nonprofit", False)

    # 6) Generic companies
    if RE_COMPANY.search(upper):
        return ("company", False)

    # 7) Institutional trusts (anything with "TRUST" that didn't
    #    match the family-trust patterns above)
    if RE_TRUST_ANY.search(upper):
        return ("trust_inst", False)

    # 8) Default — individual person
    return ("individual", True)


def should_exclude(name: str) -> bool:
    """Convenience wrapper: True if this owner should be filtered out."""
    _kind, keep = classify_owner(name)
    return not keep


def kind_label(kind: str) -> str:
    """Human-readable label for logs and dashboard tooltips."""
    return {
        "individual":   "Individual",
        "estate":       "Estate (deceased owner)",
        "family_trust": "Family/personal trust",
        "company":      "Company (LLC/INC/CORP/etc.)",
        "trust_inst":   "Institutional trust",
        "religious":    "Religious organization",
        "school":       "School / educational institution",
        "government":   "Government body",
        "nonprofit":    "Nonprofit organization",
        "hoa":          "Homeowners association",
    }.get(kind, kind)


# ----------------------------------------------------------------
# CLI for ad-hoc testing
# ----------------------------------------------------------------
if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("usage: python ccln_owner_filter.py <owner_name> [<owner_name> ...]")
        sys.exit(1)
    for name in sys.argv[1:]:
        kind, keep = classify_owner(name)
        marker = "KEEP " if keep else "SKIP"
        print(f"{marker}  {kind:14}  {name}")
