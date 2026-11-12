# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Static GitHub Pages site of CalCOFI CTD cross-shelf sections (one per line × cruise). Prebuilt JSON in
`public/data/`, vanilla JS in `public/app.js` + Plotly (`public/vendor/`). No server, no database at
request time. `README.md` holds the reasoning behind most rules below — read the relevant section before
changing behavior.

## Commands

Run everything from the repo root (scripts use relative paths like `public/data`).

```bash
# build (what CI does in .github/workflows/refresh.yml)
python3 scripts/resolve_release.py             # latest.txt + catalog.json -> build/build_sections.sql (--version vYYYY.MM.DD to pin)
# staged candidate: CALCOFI_RELEASE_PREFIX=ducklake-staging/releases CALCOFI_RELEASE_VERSION=vYYYY.MM.DD python3 scripts/resolve_release.py
mkdir -p public/data/sections                  # duckdb COPY does not mkdir
duckdb -c ".read build/build_sections.sql"     # query release parquet -> public/data/_*.parquet (~30 s)
pip install -r requirements.txt                # pandas, pyarrow
python3 scripts/build_sections.py              # reshape into index.json, stations.json, sections/*.json (~6 s)
cd public && python3 -m http.server 8777       # serve locally

# tests: plain unittest scripts (no pytest config), each self-contained
python3 scripts/test_display_rules.py
python3 scripts/test_display_rules.py TestSparse                  # one class
python3 scripts/test_display_rules.py TestSparse.test_<name>      # one test
# others: test_resolve_release, test_build_sections_sql (needs duckdb CLI, else skipped),
#         test_station_ruler, test_build_sections_reshape, test_fetch_citation
```

CI (`refresh.yml`) runs the self-tests before every build; a new rule in the build should get a test there
too and a step in `refresh.yml`.

## Architecture

Pipeline: GCS release parquet → `scripts/build_sections.sql` (DuckDB) → flat `public/data/_*.parquet`
(git-ignored scaffolding) → `scripts/build_sections.py` → shards in `public/data/sections/{line}__{cruise}.json`
plus `index.json` and `stations.json`. The app fetches `index.json`, then one shard at a time.

- **Release resolution**: `scripts/build_sections.sql` is a *template*; it never names a parquet path.
  `__TBL:<table>__` tokens are rendered by `scripts/resolve_release.py` (stdlib only) into `read_parquet()`
  over the objects listed in the release's `catalog.json` (content-addressed `ducklake/tables/` since
  v2026.09; legacy `releases/<v>/parquet/` before). Release version always comes from `latest.txt` — never
  hardcode it. Same rule as `calcofi4py.release.release_sources()` / `calcofi4r::cc_release_sources()`.
- **Fallbacks for older releases**: `climatology_fallback.sql` and `sample_measurement_fallback.sql` are
  inlined when a release lacks those tables; optional columns (e.g. seasonal baseline) are feature-detected
  with an empty typed `SELECT` unioned `BY NAME`, and `index.json` flags tell the app what is available.
- **Shard format**: every variable as a station × depth matrix (10 m bins to 500 m) — exactly Plotly
  heatmap `z`. Smoothing is `zsmooth: "best"`; there is deliberately no interpolation code in the browser.
  Each shard carries `anom` and `anom_season` (anomaly vs the release's `climatology` table, keyed on the
  cruise's designated month from `cruise_key`, never the cast date), per-cast MLD / chl-max lines, and
  `withheld: {var: reason}`.
- **Transect definition**: a CalCOFI line ordered by station number, offshore left / coast right. Stations
  are the real `sample.site_key`, not the grid cell. A station belongs to the section line within 0.5 units
  of its `site_key` line; its x position on the comparable ruler is `station_ruler()` in `build_sections.py`
  (7.386 km per station unit, `+proj=calcofi`).
- **Display rules** (`scripts/display_rules.py`, applied at build so published JSON carries them): drop
  sections with < 3 stations, variables at < 3 or < half the stations, and variables constant with depth.
  The app shows what was withheld. Separately, `PRELIMINARY_WITHOUT_BOTTLE_VARS` in `app.js` limits
  `preliminary_without_bottle` cruises to temperature only; `build_sections.py` refuses the retired
  `preliminary` data_stage.
- **Bathymetry is a committed input**: `metadata/station_bathymetry.csv` and `metadata/line_bathymetry.csv`
  are made by hand with `scripts/build_station_bathymetry.R` (via `calcofi4r::cc_transect_bathy()`, 500 m
  sampling). CI never regenerates them; re-run whenever a release changes `grid`.
- **App state** lives in the URL (`readURL()` / `writeURL()` in `app.js`, e.g. `?base=season`,
  `?lines=off`). Data fetches append `?v=<release>` from `version.json` for cache-busting (Pages serves
  `max-age=600`).

## CI and data ownership

- `public/data/` is CI-owned: `refresh.yml` (weekly, manual, and `repository_dispatch: db-release` from
  `CalCOFI/workflows`) rebuilds and commits it as "data: refresh transects from latest DB release". Don't
  hand-commit data; most of `git log` is these bot commits.
- Any new generated artifact must be added to the `git add` list in `refresh.yml`, or it is silently dropped.
- `refresh.yml` must dispatch `pages.yml` with an explicit `-f sha=` (a `GITHUB_TOKEN` push does not
  trigger `on: push`, and `--ref main` alone can deploy the pre-push tree). Pages source must be
  "GitHub Actions".
