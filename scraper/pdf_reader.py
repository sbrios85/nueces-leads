"""
Foreclosure PDF reader for the Nueces County clerk portal.
==========================================================

Workflow per document:
  1. Authenticate (once per Playwright session)
  2. Navigate to the doc's detail page on the portal
  3. Click the download button — receive the actual PDF file
  4. Extract text via pdfplumber
  5. Parse fields via regex (borrower name, loan amount, address, etc.)
  6. Optionally cross-reference legal description against NCAD when
     the PDF didn't expose a street address

Rate limiting (configurable via constants below):
  * Hard cap of N downloads per workflow run
  * Random 30-90 sec delays between downloads (anti-fingerprint)
  * Stops on any 429 / 503 / "please slow down" indicator
  * Persistent cache at .cache/pdf_extractions.json so re-runs skip
    already-processed docs

This module is imported by:
  * scraper/extract_foreclosure_pdfs.py — the dedicated workflow runner
  * scraper/fetch.py — could be invoked as a final daily step (TBD)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

log = logging.getLogger("nueces-pdf-reader")

# --------------------------------------------------------------------------- #
# Knobs
# --------------------------------------------------------------------------- #

CLERK_BASE = "https://nueces.tx.publicsearch.us"
SIGNIN_URL = f"{CLERK_BASE}/signin"
PDF_EXTRACTION_CACHE = ".cache/pdf_extractions.json"

# These get overridden by the workflow runner. Defaults are conservative.
PDF_DOWNLOADS_PER_RUN = 10
PDF_MIN_DELAY_SEC = 30
PDF_MAX_DELAY_SEC = 90
PDF_PHASE_BUDGET_SEC = 50 * 60   # 50 minutes hard cap per run

# User-Agent for the authenticated browser context — looks like a real Chrome.
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


# --------------------------------------------------------------------------- #
# Cache
# --------------------------------------------------------------------------- #

def _cache_path(root_dir: Path) -> Path:
    return root_dir / PDF_EXTRACTION_CACHE


def load_pdf_cache(root_dir: Path) -> Dict[str, Dict[str, Any]]:
    """Load the persistent doc_num → extraction-result cache."""
    p = _cache_path(root_dir)
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("could not load PDF cache: %s", exc)
        return {}
    if isinstance(raw, dict) and raw.get("_version") == "v1":
        return raw.get("data", {})
    log.info("legacy PDF cache (version=%r) — discarding",
             raw.get("_version") if isinstance(raw, dict) else None)
    return {}


def save_pdf_cache(root_dir: Path, cache: Dict[str, Dict[str, Any]]) -> None:
    p = _cache_path(root_dir)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        envelope = {"_version": "v1", "data": cache}
        p.write_text(json.dumps(envelope, indent=2, default=str),
                      encoding="utf-8")
        log.info("PDF cache: %d entries written", len(cache))
    except Exception as exc:
        log.warning("could not save PDF cache: %s", exc)


# --------------------------------------------------------------------------- #
# Authentication
# --------------------------------------------------------------------------- #

async def _login(page) -> bool:
    """Submit username + password from env vars to the portal's signin form.

    Returns True on success. Failure (wrong creds, 2FA challenge, layout
    change) returns False; the caller aborts the run.
    """
    username = os.environ.get("CLERK_USERNAME") or ""
    password = os.environ.get("CLERK_PASSWORD") or ""
    if not username or not password:
        log.error("CLERK_USERNAME / CLERK_PASSWORD not set — cannot login")
        return False

    log.info("logging in as %s...", username)
    try:
        await page.goto(SIGNIN_URL, wait_until="domcontentloaded",
                         timeout=30_000)
        await page.wait_for_timeout(800)

        # The portal's sign-in form fields. We use multiple selectors
        # because BIS layouts vary. Try the most common ones in order.
        username_filled = False
        for sel in ("input[name='email']", "input[type='email']",
                     "input[name='username']", "input[id*='email' i]",
                     "input[id*='username' i]", "input[autocomplete='username']"):
            try:
                el = await page.query_selector(sel)
                if el:
                    await el.fill(username)
                    username_filled = True
                    break
            except Exception:
                continue
        if not username_filled:
            log.error("could not find username field on sign-in page")
            return False

        password_filled = False
        for sel in ("input[name='password']", "input[type='password']",
                     "input[id*='password' i]", "input[autocomplete='current-password']"):
            try:
                el = await page.query_selector(sel)
                if el:
                    await el.fill(password)
                    password_filled = True
                    break
            except Exception:
                continue
        if not password_filled:
            log.error("could not find password field on sign-in page")
            return False

        # Submit the form. Try clicking a button first; fall back to Enter.
        submitted = False
        for sel in ("button[type='submit']", "input[type='submit']",
                     "button:has-text('Sign In')", "button:has-text('Login')",
                     "button:has-text('Log In')"):
            try:
                btn = await page.query_selector(sel)
                if btn:
                    await btn.click()
                    submitted = True
                    break
            except Exception:
                continue
        if not submitted:
            try:
                await page.keyboard.press("Enter")
                submitted = True
            except Exception:
                pass

        # Wait for navigation to complete. We're logged in when the URL
        # is no longer /signin OR when we see a "Sign Out" link.
        try:
            await page.wait_for_function(
                """() => {
                    if (window.location.pathname === '/signin') return false;
                    // Check for sign-out link as positive confirmation
                    const links = document.querySelectorAll('a, button');
                    for (const l of links) {
                        const t = (l.textContent || '').toLowerCase();
                        if (t.includes('sign out') || t.includes('logout') ||
                            t.includes('log out')) return true;
                    }
                    return window.location.pathname !== '/signin';
                }""",
                timeout=15_000,
            )
        except Exception:
            pass

        # Final check: are we off the sign-in page?
        cur = page.url or ""
        if "/signin" in cur:
            log.error("login appears to have failed — still on /signin")
            # Try to capture any error message visible on the page
            try:
                err_text = await page.evaluate("""() => {
                    const errs = document.querySelectorAll(
                        '[role=alert], [class*=error], [class*=Error]');
                    return Array.from(errs)
                        .map(e => (e.textContent || '').trim())
                        .filter(Boolean).slice(0, 3).join(' | ');
                }""")
                if err_text:
                    log.error("portal error text: %s", err_text[:300])
            except Exception:
                pass
            return False

        log.info("login successful (now at %s)", cur[:100])
        return True
    except Exception as exc:
        log.error("login flow crashed: %s\n%s", exc, traceback.format_exc())
        return False


# --------------------------------------------------------------------------- #
# Per-document fetch
# --------------------------------------------------------------------------- #

async def _navigate_to_doc(page, doc_num: str,
                            debug_dir: Optional[Path] = None) -> bool:
    """Navigate Playwright to the document's detail page.

    The clerk portal SPA may defer XHR data-loading when it detects
    automation (headless Chrome, missing fingerprints). This function
    uses multiple anti-detection strategies + extended waits to coax
    the data into loading:

      1. Real navigation (not just direct URL) — go via homepage if needed
      2. Wait for networkidle (all XHRs complete, not just DOM ready)
      3. Simulate human interaction (mouse moves, scrolls) to trigger
         lazy-loaded data
      4. Wait up to 30s for Redux workspace to populate
      5. Read internal doc_id from Redux and navigate directly
      6. Save extensive diagnostics on failure
    """
    from urllib.parse import urlencode
    search_url = (f"{CLERK_BASE}/results?"
                  + urlencode({
                      "department": "FC",
                      "searchType": "quickSearch",
                      "searchValue": doc_num,
                      "instrumentDateRange": "18000101,20991231",
                      "limit": 50,
                      "offset": 0,
                      "keywordSearch": "false",
                      "searchOcrText": "false",
                  }))

    try:
        # Use networkidle wait — gives the SPA time to fire its data XHRs.
        await page.goto(search_url, wait_until="networkidle", timeout=45_000)
    except Exception as exc:
        # networkidle may timeout if there's a long-poll connection;
        # fall back to domcontentloaded and rely on the explicit waits.
        log.debug("networkidle timed out for %s, falling back: %s",
                  doc_num, exc)
        try:
            await page.goto(search_url, wait_until="domcontentloaded",
                             timeout=25_000)
        except Exception as exc2:
            log.warning("search nav failed for %s: %s", doc_num, exc2)
            return False

    # Diagnostic: capture XHR traffic so we can see what data calls
    # the SPA is making (and whether they're failing). Only logs API-ish
    # URLs to avoid spam from analytics + static resources.
    xhr_log: List[Dict[str, Any]] = []

    async def _on_response(resp):
        try:
            url_l = resp.url.lower()
            if not any(tok in url_l for tok in
                       ("/api/", "/document", "/results", "/search",
                        "/workspaces", "/record", "/instrument", "graphql")):
                return
            status = resp.status
            ct = resp.headers.get("content-type", "")
            body_preview = ""
            if "json" in ct.lower() and status < 400:
                try:
                    body = await resp.json()
                    body_preview = json.dumps(body, default=str)[:300]
                except Exception:
                    body_preview = "(unparseable JSON)"
            elif status >= 400:
                try:
                    body_preview = (await resp.text())[:300]
                except Exception:
                    body_preview = "(unreadable body)"
            xhr_log.append({
                "url": resp.url,
                "status": status,
                "content_type": ct,
                "body_preview": body_preview,
            })
        except Exception:
            pass

    page.on("response", lambda r: asyncio.create_task(_on_response(r)))

    # Simulate human interaction to coax lazy data-loading.
    # Some SPAs only fire data XHRs after a user-event like mousemove.
    try:
        await page.mouse.move(400, 300)
        await page.wait_for_timeout(200)
        await page.mouse.move(600, 400)
        await page.evaluate("window.scrollBy(0, 100)")
        await page.wait_for_timeout(300)
        await page.evaluate("window.scrollBy(0, -50)")
        await page.wait_for_timeout(200)
    except Exception:
        pass

    # Wait for Redux workspace data to populate. Now with 30-second budget.
    workspace_loaded = False
    try:
        await page.wait_for_function(
            """() => {
                try {
                    const d = (window.__data || {}).documents;
                    if (!d || !d.workspaces) return false;
                    for (const ws of Object.values(d.workspaces)) {
                        if (!ws) continue;
                        const bh = (ws.data || {}).byHash || {};
                        if (Object.keys(bh).length > 0) return true;
                    }
                    return false;
                } catch (e) { return false; }
            }""",
            timeout=30_000,
        )
        workspace_loaded = True
    except Exception as exc:
        log.warning("workspace load timeout for %s after 30s: %s",
                    doc_num, exc)

    # Whether or not the workspace loaded, try to extract the doc_id.
    # Sometimes the data lives elsewhere in the state tree.
    doc_id = None
    try:
        doc_id = await page.evaluate("""(dn) => {
            try {
                const d = (window.__data || {}).documents;
                if (!d) return null;
                // Look in all workspaces' byHash
                if (d.workspaces) {
                    for (const ws of Object.values(d.workspaces)) {
                        if (!ws || !ws.data) continue;
                        const bh = ws.data.byHash || {};
                        for (const rec of Object.values(bh)) {
                            if (!rec) continue;
                            if (rec.docNumber === dn ||
                                rec.doc_number === dn ||
                                rec.instrumentNumber === dn ||
                                rec.instrument_number === dn) {
                                return rec.id || rec.docId ||
                                       rec.documentId || rec._id || null;
                            }
                        }
                    }
                }
                // Also look in document preview state (sometimes pre-loaded)
                const dp = (window.__data || {}).docPreview;
                if (dp && dp.document && dp.document.id) {
                    return dp.document.id;
                }
            } catch (e) {}
            return null;
        }""", doc_num)
    except Exception:
        doc_id = None

    if doc_id:
        log.info("  resolved doc_id=%s for %s", doc_id, doc_num)
        try:
            await page.goto(f"{CLERK_BASE}/doc/{doc_id}",
                             wait_until="networkidle", timeout=30_000)
            if "/doc/" in (page.url or ""):
                return True
        except Exception as exc:
            log.warning("doc nav failed for %s (id=%s): %s",
                        doc_num, doc_id, exc)

    # Fallback: native click on the row (Playwright handles JS click handlers).
    try:
        row = page.locator("table tbody tr",
                            has=page.locator(f"td:text-is('{doc_num}')"))
        if await row.count() > 0:
            log.info("  trying row.click() fallback for %s", doc_num)
            await row.first.click(timeout=10_000, force=True)
            for _ in range(40):
                await page.wait_for_timeout(250)
                if "/doc/" in (page.url or ""):
                    return True
    except Exception as exc:
        log.debug("row click failed: %s", exc)

    # All strategies failed — save diagnostics with extra context.
    if debug_dir is not None:
        try:
            debug_dir.mkdir(parents=True, exist_ok=True)
            # Save HTML
            diag_path = debug_dir / f"nav_failure_{doc_num}.html"
            if not diag_path.exists():
                html = await page.content()
                diag_path.write_text(html, encoding="utf-8")
            # Save Redux state snapshot (small JSON)
            state_path = debug_dir / f"nav_state_{doc_num}.json"
            if not state_path.exists():
                state_snapshot = await page.evaluate("""() => {
                    const out = {};
                    try {
                        const d = (window.__data || {}).documents;
                        if (d) {
                            out.documents = {
                                workspaces: {}
                            };
                            for (const [k, ws] of Object.entries(
                                d.workspaces || {})) {
                                out.documents.workspaces[k] = {
                                    hasFetched: ws.hasFetched,
                                    isLoading: ws.isLoading,
                                    errors: ws.errors,
                                    byHashKeys: Object.keys(
                                        (ws.data || {}).byHash || {}),
                                    byHashSample: Object.values(
                                        (ws.data || {}).byHash || {})[0] || null,
                                    meta: ws.meta
                                };
                            }
                        }
                        out.user_loggedIn = ((window.__data || {}).user || {}).loggedIn;
                    } catch (e) {
                        out.error = e.toString();
                    }
                    return out;
                }""")
                # Include the captured XHR log
                state_snapshot["xhr_log"] = xhr_log
                state_path.write_text(
                    json.dumps(state_snapshot, indent=2, default=str),
                    encoding="utf-8")
            log.info("saved nav diagnostics: nav_failure_%s.html + "
                     "nav_state_%s.json (workspace_loaded=%s)",
                     doc_num, doc_num, workspace_loaded)
        except Exception as exc:
            log.debug("diagnostic save failed: %s", exc)

    return False


async def _download_pdf(page, doc_num: str,
                         download_dir: Path) -> Optional[Path]:
    """Trigger PDF download via the portal's 5-step purchase flow.

    The clerk portal doesn't offer direct PDF downloads — instead, every
    document must go through the cart. For foreclosure notices the total
    is always $0.00 (free), but the flow is the same as a paid order:

      1. On the document detail page, click "Add to Cart"
      2. A modal pops up — keep "All pages" selected and click "Add"
      3. Navigate to the cart (URL = /cart/contents)
      4. Click "Place Your Order"
      5. On the checkout-complete page, click "Download PDF" — this
         triggers the actual file download captured by expect_download

    Caller must already be on the /doc/<id> page when this is invoked.
    """
    download_dir.mkdir(parents=True, exist_ok=True)
    out_path = download_dir / f"{doc_num}.pdf"

    # Step 1: click "Add to Cart" on the document detail page.
    try:
        await page.locator("button:has-text('Add to Cart')").first.click(
            timeout=10_000)
    except Exception as exc:
        log.warning("step 1 (Add to Cart click) failed for %s: %s", doc_num, exc)
        return None

    # Step 2: in the "Add to Cart" modal, click the "Add" button.
    # "All pages" is the default radio so we leave it. Wait for modal
    # to render first.
    try:
        await page.wait_for_selector("button:has-text('Add')",
                                      timeout=8_000)
        # Filter to the modal's Add button (avoid hitting "Add to Cart"
        # which has "Add" in it too).
        await page.evaluate("""() => {
            const btns = Array.from(document.querySelectorAll('button'));
            for (const b of btns) {
                const t = (b.textContent || '').trim().toLowerCase();
                if (t === 'add') { b.click(); return true; }
            }
            return false;
        }""")
        await page.wait_for_timeout(800)
    except Exception as exc:
        log.warning("step 2 (modal Add click) failed for %s: %s", doc_num, exc)
        return None

    # Step 3: navigate to the cart.
    try:
        await page.goto(f"{CLERK_BASE}/cart/contents",
                         wait_until="domcontentloaded", timeout=20_000)
        await page.wait_for_timeout(800)
    except Exception as exc:
        log.warning("step 3 (cart nav) failed for %s: %s", doc_num, exc)
        return None

    # Verify our doc is actually in the cart (the Add might have silently
    # failed for some reason).
    try:
        in_cart = await page.evaluate("""(dn) => {
            return (document.body.innerText || '').includes(dn);
        }""", doc_num)
    except Exception:
        in_cart = False
    if not in_cart:
        log.warning("step 3 — doc %s not in cart after Add", doc_num)
        return None

    # Step 4: click "Place Your Order".
    try:
        await page.wait_for_selector("button:has-text('Place Your Order')",
                                      timeout=8_000)
        await page.locator("button:has-text('Place Your Order')").first.click(
            timeout=10_000)
        # Wait for redirect to checkout-complete page.
        completed = False
        for _ in range(40):
            await page.wait_for_timeout(250)
            if "checkout-complete" in (page.url or ""):
                completed = True
                break
        if not completed:
            log.warning("step 4 — never redirected to checkout-complete "
                        "for %s (still at %s)",
                        doc_num, (page.url or "")[:80])
            return None
    except Exception as exc:
        log.warning("step 4 (Place Your Order) failed for %s: %s", doc_num, exc)
        return None

    # Step 5: click "Download PDF" — this triggers the actual file
    # download. Use Playwright's expect_download to capture it.
    try:
        async with page.expect_download(timeout=30_000) as dl_info:
            await page.locator("button:has-text('Download PDF')").first.click(
                timeout=10_000)
            download = await dl_info.value
        await download.save_as(str(out_path))
        if out_path.exists() and out_path.stat().st_size > 100:
            log.info("  ✓ downloaded PDF for %s (%d bytes)", doc_num, out_path.stat().st_size)
            return out_path
        log.warning("PDF download for %s came out tiny/empty (%d bytes)",
                    doc_num, out_path.stat().st_size if out_path.exists() else 0)
        return None
    except Exception as exc:
        log.warning("step 5 (Download PDF) failed for %s: %s", doc_num, exc)
        return None


# --------------------------------------------------------------------------- #
# PDF text extraction + field parsing
# --------------------------------------------------------------------------- #

def _extract_text(pdf_path: Path) -> str:
    """Extract all text from a PDF using pdfplumber. Returns "" on failure."""
    try:
        import pdfplumber  # type: ignore
    except ImportError:
        log.error("pdfplumber not installed — cannot extract PDF text")
        return ""
    try:
        text_parts: List[str] = []
        with pdfplumber.open(str(pdf_path)) as pdf:
            for page in pdf.pages:
                t = page.extract_text() or ""
                if t.strip():
                    text_parts.append(t)
        return "\n\n".join(text_parts)
    except Exception as exc:
        log.warning("pdfplumber failed on %s: %s", pdf_path, exc)
        return ""


# Texas foreclosure-notice text varies but follows recognizable patterns.
# These regexes are tuned for the most common templates used by Nueces
# County trustees / lenders.

_RE_BORROWER = [
    # "executed by JOHN DOE AND JANE DOE" / "by JOHN DOE"
    re.compile(r"executed\s+by\s+([A-Z][A-Z\s,&.'-]+?)(?=\s+(?:and|to|in favor of|payable|on|at)\b|,\s*[a-z])",
               re.IGNORECASE),
    # "Mortgagor(s): JOHN DOE"
    re.compile(r"mortgagor[s]?[\s:]*([A-Z][A-Z\s,&.'-]+?)(?=\s*(?:to|in favor of|and|,\s*[a-z]))",
               re.IGNORECASE),
    # "Borrower(s): JOHN DOE"
    re.compile(r"borrower[s]?[\s:]*([A-Z][A-Z\s,&.'-]+?)(?=\s*(?:to|in favor of|and|,\s*[a-z]))",
               re.IGNORECASE),
    # "Debtor(s): JOHN DOE"
    re.compile(r"debtor[s]?[\s:]*([A-Z][A-Z\s,&.'-]+?)(?=\s*(?:to|in favor of|and|,\s*[a-z]))",
               re.IGNORECASE),
]

_RE_LENDER = [
    # "in favor of WELLS FARGO BANK NA"
    re.compile(r"in\s+favor\s+of\s+([A-Z][A-Z\s,&.'-]+?)(?=\s*(?:,\s*its successors|,?\s*as|,\s*and|\.|recorded))",
               re.IGNORECASE),
    # "Lender: WELLS FARGO BANK NA"
    re.compile(r"lender[\s:]+([A-Z][A-Z\s,&.'-]+?)(?=\s*(?:,|\.|\n))",
               re.IGNORECASE),
    # "Mortgagee: ..."
    re.compile(r"mortgagee[\s:]+([A-Z][A-Z\s,&.'-]+?)(?=\s*(?:,|\.|\n))",
               re.IGNORECASE),
    # "Beneficiary: ..."
    re.compile(r"beneficiary[\s:]+([A-Z][A-Z\s,&.'-]+?)(?=\s*(?:,|\.|\n))",
               re.IGNORECASE),
]

_RE_LOAN_AMOUNT = [
    # "Original principal balance: $123,456.78"
    re.compile(r"original\s+principal\s+(?:balance|amount)[:\s]+\$?\s*([\d,]+(?:\.\d{2})?)",
               re.IGNORECASE),
    # "principal sum of $123,456.78"
    re.compile(r"principal\s+(?:sum|amount)\s+of\s+\$?\s*([\d,]+(?:\.\d{2})?)",
               re.IGNORECASE),
    # "in the amount of $123,456.78"
    re.compile(r"in\s+the\s+amount\s+of\s+\$?\s*([\d,]+(?:\.\d{2})?)",
               re.IGNORECASE),
    # Any "$NNN,NNN" near "loan"
    re.compile(r"loan[^.]{0,40}?\$\s*([\d,]+(?:\.\d{2})?)",
               re.IGNORECASE),
]

_RE_DEED_DATE = [
    re.compile(r"deed\s+of\s+trust\s+dated\s+([A-Za-z]+\s+\d{1,2},?\s+\d{4})",
               re.IGNORECASE),
    re.compile(r"dated\s+([A-Za-z]+\s+\d{1,2},?\s+\d{4})",
               re.IGNORECASE),
]

# Texas street-address pattern. Two-stage approach: find a number+street
# pattern, then look ahead for city + TX + zip nearby. Simpler than
# trying to do everything in one regex.
_RE_STREET_LINE = re.compile(
    r"\b(\d{1,6})\s+([A-Z][A-Za-z0-9\s.,'#-]{3,60}?\b"
    r"(?:STREET|ST|AVENUE|AVE|ROAD|RD|DRIVE|DR|LANE|LN|"
    r"BOULEVARD|BLVD|COURT|CT|CIRCLE|CIR|PLACE|PL|"
    r"WAY|TRAIL|TR|PARKWAY|PKWY|HIGHWAY|HWY|TERRACE|TER)\b\.?)",
    re.IGNORECASE,
)
_RE_CITY_TX_ZIP = re.compile(
    r"\b(CORPUS\s+CHRISTI|ROBSTOWN|PORT\s+ARANSAS|BISHOP|DRISCOLL|"
    r"AGUA\s+DULCE|BANQUETE)\b[,\s]+(?:TX|TEXAS)\s+(\d{5})",
    re.IGNORECASE,
)

# Legal description — Texas standard format. Captures subdivision name
# + lot + block (+ optional unit). Used for cross-referencing against
# NCAD when the PDF doesn't have a street address.
_RE_LEGAL = re.compile(
    r"(?:Lot[s]?\s+)?([\d,A-Z-]+)[\s,]+(?:Block|Blk\.?)\s+([\dA-Z-]+)"
    r"[,\s]+(?:of\s+)?([A-Z][A-Z\s\d&.'-]+?)\s+(?:Subdivision|Addition|"
    r"Unit|Section|Phase)",
    re.IGNORECASE,
)


def parse_foreclosure_pdf_text(text: str) -> Dict[str, Any]:
    """Apply regex patterns to extract structured fields from a
    foreclosure PDF's text. Returns a dict with whatever was found.

    None of the fields are required — partial extraction is normal.
    """
    if not text:
        return {}

    out: Dict[str, Any] = {}

    # Borrower
    for rx in _RE_BORROWER:
        m = rx.search(text)
        if m:
            name = _clean_name(m.group(1))
            if name and len(name) >= 4:
                out["borrower"] = name
                break

    # Lender
    for rx in _RE_LENDER:
        m = rx.search(text)
        if m:
            name = _clean_name(m.group(1))
            if name and len(name) >= 3:
                out["lender"] = name
                break

    # Loan amount
    for rx in _RE_LOAN_AMOUNT:
        m = rx.search(text)
        if m:
            try:
                amt = float(m.group(1).replace(",", ""))
                if 1000 < amt < 10_000_000:   # sanity: $1k to $10M
                    out["loan_amount"] = amt
                    break
            except ValueError:
                continue

    # Deed-of-trust date
    for rx in _RE_DEED_DATE:
        m = rx.search(text)
        if m:
            out["deed_date_raw"] = m.group(1).strip()
            break

    # Property street address — two-stage match.
    # 1. Find the street part (number + street name + suffix)
    # 2. Look in nearby text for city + TX + zip
    street_match = _RE_STREET_LINE.search(text)
    if street_match:
        street = (street_match.group(1) + " " + street_match.group(2)).strip()
        # Look within 200 chars after the street for a city + zip
        tail_start = street_match.end()
        tail = text[tail_start:tail_start + 300]
        cz = _RE_CITY_TX_ZIP.search(tail)
        if cz:
            out["prop_address"] = re.sub(r"\s+", " ", street).upper().strip(" ,.")
            out["prop_city"] = re.sub(r"\s+", " ", cz.group(1)).upper()
            out["prop_state"] = "TX"
            out["prop_zip"] = cz.group(2)
        else:
            # Street found but no city/zip nearby — still record the street.
            out["prop_address"] = re.sub(r"\s+", " ", street).upper().strip(" ,.")
            out["prop_state"] = "TX"

    # Legal description (subdivision + lot + block)
    if not out.get("prop_address"):
        # Only bother extracting legal if we don't already have a street
        # address — legal is the cross-reference fallback path.
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
    # Strip trailing common words that get caught by greedy regex
    s = re.sub(r"\s+(and|to|in|the)\s*$", "", s, flags=re.IGNORECASE)
    return s.strip()


# --------------------------------------------------------------------------- #
# Legal-description normalization for cross-referencing
# --------------------------------------------------------------------------- #

def normalize_legal_for_match(legal: str) -> Tuple[str, str, str]:
    """Normalize a legal description into (subdivision, lot, block) for
    matching across portals. Returns lowercased tokens, with the
    subdivision name reduced to a comparable form.

    Examples:
      'LOT 9 BLOCK 2 DOUGLAS UNIT TWO ADDITION'
        → ('douglas unit 2', '9', '2')
      'Subdivision- Name: DOUGLAS UNIT 2 Lot: 9 Block: 2'
        → ('douglas unit 2', '9', '2')
    """
    if not legal:
        return ("", "", "")
    s = legal.upper()

    # Find lot
    lot = ""
    m = re.search(r"\bLOT[S]?\s*[:\-]?\s*([\d,A-Z-]+)", s)
    if m:
        lot = m.group(1).strip(" ,-")

    # Find block
    block = ""
    m = re.search(r"\bBLOCK\s*[:\-]?\s*([\dA-Z-]+)", s)
    if m:
        block = m.group(1).strip(" ,-")
    else:
        m = re.search(r"\bBLK\.?\s*[:\-]?\s*([\dA-Z-]+)", s)
        if m:
            block = m.group(1).strip(" ,-")

    # Find subdivision name. Strip the structure tokens (LOT, BLOCK,
    # SUBDIVISION, NAME:, etc.), then take whatever's left.
    sub = re.sub(r"SUBDIVISION[\s\-]+NAME[\s:]*", " ", s)
    sub = re.sub(r"\bLOT[S]?\s*[:\-]?\s*[\d,A-Z-]+", " ", sub)
    sub = re.sub(r"\bBLOCK\s*[:\-]?\s*[\dA-Z-]+", " ", sub)
    sub = re.sub(r"\bBLK\.?\s*[:\-]?\s*[\dA-Z-]+", " ", sub)
    sub = re.sub(r"\bSUBDIVISION\b|\bADDITION\b|\bSECTION\b|\bPHASE\b",
                  " ", sub)
    sub = re.sub(r"[\(\)]", " ", sub)
    sub = re.sub(r"\s+", " ", sub).strip(" ,-:")

    # Normalize "UNIT TWO" → "UNIT 2" etc.
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
    # Lot must match exactly
    if la != lb:
        return False
    # Block must match if both have one
    if ba and bb and ba != bb:
        return False
    # Subdivision name: tolerate minor differences via token overlap.
    a_tokens = set(sa.split())
    b_tokens = set(sb.split())
    common = a_tokens & b_tokens
    # Require at least 1 meaningful subdivision-name token in common
    # (filter out generic stop-words first).
    STOP = {"the", "of", "a", "an"}
    common -= STOP
    return len(common) >= 1


# --------------------------------------------------------------------------- #
# Top-level driver: process a list of foreclosure records
# --------------------------------------------------------------------------- #

async def process_foreclosure_pdfs(
    records: List[Dict[str, Any]],
    root_dir: Path,
    max_downloads: int = PDF_DOWNLOADS_PER_RUN,
) -> int:
    """Drive the full pipeline for up to `max_downloads` records.

    Mutates each processed record in place with extracted fields.
    Returns the number of records newly enriched.

    Strategy:
      1. Identify records that need PDF processing (no owner yet, has
         clerk-portal access via doc_num)
      2. Sort by sale_date ascending (closest first — most actionable)
      3. Login once
      4. For each record: navigate, download, extract, parse, save
      5. Random sleep between docs (anti-fingerprint)
    """
    try:
        from playwright.async_api import async_playwright  # type: ignore
    except ImportError:
        log.error("playwright not installed — cannot process PDFs")
        return 0

    # Pick eligible records: pre-foreclosure (sale date in the future
    # OR within ~7 days past) AND no owner field populated yet.
    today = datetime.now(timezone.utc).date().isoformat()
    eligible = []
    for r in records:
        if r.get("owner"):
            continue
        if not r.get("doc_num"):
            continue
        # Skip post-foreclosure (sale already happened > 7 days ago)
        sd = r.get("sale_date") or ""
        # Keep if no sale_date OR if sale_date >= today
        if sd and sd < today:
            continue
        eligible.append(r)

    if not eligible:
        log.info("PDF reader: no eligible foreclosure records")
        return 0

    # Sort by sale_date ascending (soonest first), undated last.
    eligible.sort(key=lambda r: (r.get("sale_date") or "9999-99-99"))

    cache = load_pdf_cache(root_dir)
    log.info("PDF reader: %d eligible, %d cached, will download up to %d",
             len(eligible), len(cache), max_downloads)

    download_dir = root_dir / "debug" / "pdfs"
    download_dir.mkdir(parents=True, exist_ok=True)

    deadline = time.time() + PDF_PHASE_BUDGET_SEC
    enriched = 0
    downloaded_this_run = 0

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
                "--disable-web-security",
            ],
        )
        context = await browser.new_context(
            user_agent=USER_AGENT,
            accept_downloads=True,
            viewport={"width": 1280, "height": 800},
            locale="en-US",
            timezone_id="America/Chicago",
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": ("text/html,application/xhtml+xml,application/xml;"
                            "q=0.9,image/avif,image/webp,*/*;q=0.8"),
            },
        )

        # Hide the webdriver flag and other automation telltales — many
        # SPAs sniff for navigator.webdriver and deny data loads when set.
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined
            });
            // Pretend we have Chrome's runtime
            window.chrome = window.chrome || { runtime: {} };
            // Fake plugins length (real Chrome has plugins)
            Object.defineProperty(navigator, 'plugins', {
                get: () => [1, 2, 3, 4, 5]
            });
            Object.defineProperty(navigator, 'languages', {
                get: () => ['en-US', 'en']
            });
        """)

        page = await context.new_page()

        # Authenticate once.
        if not await _login(page):
            log.error("login failed — aborting PDF processing")
            await context.close()
            await browser.close()
            return 0

        for rec in eligible:
            if downloaded_this_run >= max_downloads:
                log.info("PDF reader: hit max_downloads=%d cap",
                         max_downloads)
                break
            if time.time() > deadline:
                log.warning("PDF reader: time budget exhausted")
                break

            dn = rec.get("doc_num")
            cached = cache.get(dn) if dn else None
            # Only honor SUCCESSFUL cache entries — retry errors so a
            # later run after a code fix can succeed.
            if cached and not cached.get("error"):
                _apply_extraction(rec, cached)
                if rec.get("owner"):
                    enriched += 1
                continue

            log.info("processing PDF for doc %s (sale %s)...",
                     dn, rec.get("sale_date"))

            # Navigate
            ok = await _navigate_to_doc(page, dn,
                                          debug_dir=root_dir / "debug")
            if not ok:
                log.warning("could not navigate to doc %s", dn)
                cache[dn] = {"error": "navigation_failed",
                             "fetched_at": datetime.now(timezone.utc).isoformat()}
                downloaded_this_run += 1
                await _humanlike_sleep()
                continue

            # Download
            pdf_path = await _download_pdf(page, dn, download_dir)
            downloaded_this_run += 1
            if not pdf_path:
                log.warning("could not download PDF for doc %s", dn)
                cache[dn] = {"error": "download_failed",
                             "fetched_at": datetime.now(timezone.utc).isoformat()}
                await _humanlike_sleep()
                continue

            # Extract + parse
            text = _extract_text(pdf_path)
            if not text:
                log.warning("no text extracted from %s.pdf (scanned?)", dn)
                cache[dn] = {"error": "no_text",
                             "fetched_at": datetime.now(timezone.utc).isoformat()}
                await _humanlike_sleep()
                continue

            fields = parse_foreclosure_pdf_text(text)
            fields["fetched_at"] = datetime.now(timezone.utc).isoformat()
            fields["text_length"] = len(text)
            cache[dn] = fields

            _apply_extraction(rec, fields)
            if rec.get("owner"):
                enriched += 1
                log.info("  → %s: %s @ %s", dn,
                         fields.get("borrower"),
                         fields.get("prop_address") or
                         f"legal:{fields.get('legal_subdivision', '?')}")
            else:
                log.info("  → %s: no borrower found in PDF", dn)

            await _humanlike_sleep()

        await context.close()
        await browser.close()

    save_pdf_cache(root_dir, cache)
    log.info("PDF reader: downloaded %d, enriched %d records this run",
             downloaded_this_run, enriched)
    return enriched


def _apply_extraction(rec: Dict[str, Any], fields: Dict[str, Any]) -> None:
    """Copy extracted PDF fields onto a foreclosure record dict."""
    if not fields or fields.get("error"):
        return
    if fields.get("borrower") and not rec.get("owner"):
        rec["owner"] = fields["borrower"]
    if fields.get("loan_amount") and not rec.get("loan_amount"):
        rec["loan_amount"] = fields["loan_amount"]
    if fields.get("prop_address") and not rec.get("prop_address"):
        rec["prop_address"] = fields["prop_address"]
        rec["prop_city"] = fields.get("prop_city", "")
        rec["prop_state"] = fields.get("prop_state", "TX")
        rec["prop_zip"] = fields.get("prop_zip", "")
    if fields.get("lender") and not rec.get("lender"):
        rec["lender"] = fields["lender"]


async def _humanlike_sleep() -> None:
    """Random delay between PDF downloads (anti-fingerprint).

    Default 30-90 seconds. Override via env vars PDF_MIN_DELAY_SEC and
    PDF_MAX_DELAY_SEC — useful during smoke-testing.
    """
    min_s = float(os.environ.get("PDF_MIN_DELAY_SEC", PDF_MIN_DELAY_SEC))
    max_s = float(os.environ.get("PDF_MAX_DELAY_SEC", PDF_MAX_DELAY_SEC))
    secs = random.uniform(min_s, max_s)
    log.info("  sleeping %.0fs before next document...", secs)
    await asyncio.sleep(secs)


# --------------------------------------------------------------------------- #
# Cross-reference: NCAD lookup-by-name + legal-description match
# --------------------------------------------------------------------------- #

async def cross_reference_legal_descriptions(
    records: List[Dict[str, Any]],
    root_dir: Path,
) -> int:
    """For records that have a borrower + legal description from the PDF
    but no street address, search NCAD by borrower name and find the
    matching property by legal description. Mutates records in place.

    Returns the number of records newly enriched with addresses.
    """
    eligible = []
    for r in records:
        if r.get("prop_address"):
            continue                  # already have address
        if not r.get("owner"):
            continue                  # no name to search by
        if not (r.get("legal_subdivision") or r.get("legal_lot")):
            continue                  # no legal to match against
        eligible.append(r)

    if not eligible:
        log.info("cross-ref: no records eligible for legal-match enrichment")
        return 0

    log.info("cross-ref: %d records eligible for NCAD legal-match",
             len(eligible))

    # Lazy import — these depend on fetch.py being importable
    try:
        import sys
        sys.path.insert(0, str(root_dir / "scraper"))
        from fetch import (   # type: ignore
            _esearch_query_variants,
            _parse_esearch_result_list,
            NCAD_ESEARCH_BASE,
        )
    except Exception as exc:
        log.error("could not import NCAD helpers: %s", exc)
        return 0

    try:
        from playwright.async_api import async_playwright  # type: ignore
    except ImportError:
        log.error("playwright not installed — cannot do NCAD cross-ref")
        return 0

    enriched = 0
    from urllib.parse import urlencode

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
        context = await browser.new_context(user_agent=USER_AGENT)
        page = await context.new_page()

        # Mint a session token for NCAD esearch.
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
        log.info("NCAD session token: %s", "present" if token else "missing")

        current_year = str(datetime.now(timezone.utc).year)

        for rec in eligible:
            name = rec.get("owner", "")
            if not name:
                continue

            # Build the legal we're matching against (from PDF).
            pdf_legal = (
                f"Lot: {rec.get('legal_lot', '')} "
                f"Block: {rec.get('legal_block', '')} "
                f"Subdivision- Name: {rec.get('legal_subdivision', '')}"
            )

            # Try a few name variants. _esearch_query_variants normalizes
            # capitalization and tries "LAST FIRST" / "FIRST LAST" forms.
            best_match: Optional[Dict[str, str]] = None
            for variant in _esearch_query_variants(name)[:3]:
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

                # Walk every result, compare legal to PDF's
                for res in results:
                    res_legal = res.get("legal", "")
                    if not res_legal:
                        continue
                    if legal_descriptions_match(pdf_legal, res_legal):
                        best_match = res
                        break
                if best_match:
                    break

                await asyncio.sleep(1.0)   # polite

            if best_match and best_match.get("situs"):
                # Parse the situs address.
                from fetch import _split_us_address    # type: ignore
                site_addr, site_city, site_state, site_zip = (
                    _split_us_address(best_match["situs"]))
                if site_addr:
                    rec["prop_address"] = site_addr
                    rec["prop_city"] = site_city or "CORPUS CHRISTI"
                    rec["prop_state"] = site_state or "TX"
                    rec["prop_zip"] = site_zip
                    enriched += 1
                    log.info("  cross-ref %r → %s (matched legal)",
                             name, site_addr)
            else:
                log.info("  cross-ref %r → no match for legal %r",
                         name, pdf_legal[:80])

            await asyncio.sleep(1.5)

        await context.close()
        await browser.close()

    log.info("cross-ref: %d records gained address via NCAD legal-match",
             enriched)
    return enriched

