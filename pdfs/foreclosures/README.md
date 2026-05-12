[README (1).md](https://github.com/user-attachments/files/27653016/README.1.md)
# Foreclosure PDFs — manual upload folder

Drop newly-downloaded foreclosure PDFs into this folder, then run the
**"Parse uploaded foreclosure PDFs"** workflow from the Actions tab.

## Daily workflow

1. Open the clerk portal and identify new foreclosure filings
2. For each, go through the cart flow (Add to Cart → Place Your Order
   → Download PDF). They'll save to your local `Downloads` (or wherever
   you've configured).
3. Open this repo on GitHub.com
4. Navigate to `pdfs/foreclosures/`
5. Click **"Add file"** → **"Upload files"**
6. Drag the PDFs from your local folder into the upload area
7. Click **"Commit changes"**
8. Go to the **Actions** tab → **"Parse uploaded foreclosure PDFs"** →
   click **"Run workflow"** → green button
9. Wait ~3-5 minutes. The dashboard updates automatically.

## What the parser does

For each PDF:

1. Extracts text via `pdfplumber`
2. Parses out the document number (which has to be IN the PDF text —
   filename is ignored since portal download filenames vary)
3. Matches the doc number to an existing record in `foreclosures.json`
4. Extracts and saves these fields onto the record:
   - **Borrower name** (the homeowner)
   - **Lender** (the bank / beneficiary)
   - **Loan amount** (original principal)
   - **Deed of trust date** (when the original loan was executed)
   - **Property street address** (when present in PDF)
   - **Legal description** (subdivision/lot/block — always extracted)
5. If the PDF didn't have a street address but did have a legal
   description, runs NCAD reverse-lookup-by-borrower-name + legal-match
   to recover the address from the appraisal district
6. **Deletes the processed PDF** so this folder doesn't accumulate

## What if a PDF doesn't get processed?

It stays in this folder. Check the workflow log to see why:

- **"no text extracted"** — PDF is a scanned image with no embedded
  text. (Rare for new filings — most are text-based.)
- **"could not parse doc number from XXX.pdf"** — the parser's regex
  patterns didn't find a 10-digit document number in the text. Could
  be a non-foreclosure file uploaded by mistake, or unusual formatting.
- **"doc XXXX parsed but no matching record"** — the PDF's doc number
  doesn't match any foreclosure in the dashboard. Either the daily
  scraper hasn't caught up yet, or the doc is older than our 90-day
  lookahead window.

You can manually delete unprocessed PDFs from this folder when you're
done investigating.
