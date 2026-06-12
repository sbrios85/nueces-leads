name: Enrich CCLN property type

# Adds `state_class` + `property_type` to City Lien (CCLN) records by
# joining their NCAD account number (geo_id) against the committed NCAD
# reference table (scraper/ncad_reference.csv.gz). CCLN records carry no
# property class on their own, so this is what lets the City Liens tab —
# and the Stack rows that are CCLN-only — show what kind of property it is.
#
# Idempotent: only fills records missing property_type unless force=true.
#
# Triggers:
#   1. Manual (workflow_dispatch) — apply / force toggles, dry-run default.
#   2. Automatic (workflow_run) — after "Scrape Nueces leads" refreshes
#      CCLN, so new liens get a property type without a manual run.
#
# Pages is served from a GitHub Actions deployment, so this workflow
# deploys Pages itself after writing.

on:
  workflow_dispatch:
    inputs:
      apply:
        description: "Write property_type into the JSON (false = dry-run report only)"
        type: boolean
        default: false
      force:
        description: "Re-fill records that already have a property type"
        type: boolean
        default: false
  workflow_run:
    workflows: ["Scrape Nueces leads"]
    types:
      - completed

permissions:
  contents: write
  pages: write
  id-token: write

# Share the repo-push lock with the scrapers / enrichers / geocoder so
# two workflows never push at once.
concurrency:
  group: "ncad-cache-write"
  cancel-in-progress: false

jobs:
  enrich:
    runs-on: ubuntu-latest
    timeout-minutes: 15
    if: ${{ github.event_name == 'workflow_dispatch' || github.event.workflow_run.conclusion == 'success' }}
    steps:
      - name: Checkout
        uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - name: Pull latest (catch checkout race)
        run: |
          git fetch origin main
          git reset --hard origin/main

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Run CCLN property-type enricher
        env:
          # Auto-runs always apply, force OFF. Manual runs honor inputs.
          CCLN_PROPTYPE_APPLY: ${{ (github.event_name == 'workflow_run' || inputs.apply) && '1' || '0' }}
          CCLN_PROPTYPE_FORCE: ${{ inputs.force && '1' || '0' }}
        run: python scraper/enrich_ccln_proptype.py

      - name: Commit property types
        if: ${{ github.event_name == 'workflow_run' || inputs.apply }}
        run: |
          git config user.name  "github-actions[bot]"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
          git add dashboard/city_liens.json data/city_liens.json 2>/dev/null || true
          if git diff --cached --quiet; then
            echo "No property-type changes to commit."
          else
            git commit -m "Enrich CCLN: add property_type (${{ github.event_name }})"
            git push
          fi

      - name: Upload dashboard artifact for Pages deploy
        if: ${{ github.event_name == 'workflow_run' || inputs.apply }}
        uses: actions/upload-pages-artifact@v3
        with:
          path: dashboard

  deploy:
    needs: enrich
    if: ${{ needs.enrich.result == 'success' && (github.event_name == 'workflow_run' || inputs.apply) }}
    runs-on: ubuntu-22.04
    environment:
      name: github-pages
      url: ${{ steps.deployment.outputs.page_url }}
    steps:
      - name: Deploy to GitHub Pages
        id: deployment
        uses: actions/deploy-pages@v4
