"""One-time backfill: normalize deed_date in existing foreclosures.json
records to ISO format (YYYY-MM-DD).

Runs in-place on dashboard/foreclosures.json and data/foreclosures.json.
Idempotent: records already in ISO are left untouched. Records with
unparseable dates are left as-is (the dashboard's formatDate() falls
back to displaying the raw string).

Usage (manually or as a workflow step):
    python scraper/backfill_deed_dates.py

Or via GitHub Actions, add as a one-shot job that runs once then is
removed. Doesn't need scheduled execution — after this runs once,
every future PDF parse writes ISO directly via pdf_text_extractor's
normalize_date_string().
"""
import json
import logging
import sys
from pathlib import Path

# Make the local scraper package importable so we can pull the
# normalizer in (single source of truth — no duplicated logic).
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from pdf_text_extractor import normalize_date_string  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger(__name__)


def backfill_file(path: Path) -> None:
    if not path.exists():
        log.info("skip %s (not found)", path)
        return

    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)

    records = data.get("records", [])
    converted = unchanged = unparseable = empty = 0

    for rec in records:
        original = rec.get("deed_date", "")
        if not original:
            empty += 1
            continue
        normalized = normalize_date_string(original)
        if normalized == original:
            # Either already ISO (unchanged) or unparseable (also
            # unchanged). Disambiguate so the log is informative.
            if (len(original) == 10 and original[4] == "-"
                    and original[7] == "-"):
                unchanged += 1   # was already ISO
            else:
                unparseable += 1
                log.warning("  could not normalize: %r", original)
        else:
            rec["deed_date"] = normalized
            converted += 1

    with path.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, default=str)

    log.info(
        "%s: %d converted, %d already-ISO, %d unparseable, %d empty",
        path.name, converted, unchanged, unparseable, empty)


def main() -> int:
    repo_root = HERE.parent
    targets = [
        repo_root / "dashboard" / "foreclosures.json",
        repo_root / "data"      / "foreclosures.json",
    ]
    log.info("Backfilling deed_date → ISO format")
    for p in targets:
        backfill_file(p)
    log.info("done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
