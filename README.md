# CalCOFI CTD Transects

Cross-shelf temperature, salinity, oxygen, density and fluorescence sections along
every CalCOFI line, for every cruise in the integrated database — drawn
automatically, offshore to nearshore like the map, with no station picking.

A static site: prebuilt JSON in `public/`, vanilla JavaScript, GitHub Pages. There
is no server and no database at request time.

## Why this exists

[`apps/ctd-viz`](https://github.com/CalCOFI/apps/tree/main/ctd-viz) already draws
CalCOFI CTD sections, but it is a Shiny app and it asks the user to define the
transect by clicking a start and an end station on a map. That makes it a tool for
someone who already knows what they are looking for, and it orders the section by
`ord_occ` — the order the ship occupied stations — so the direction is whichever
way the ship happened to steam.

Here a transect is **a CalCOFI line, ordered by station number**. That is well
defined for every cruise without any user input, which is what makes the whole
set pre-renderable and linkable.

It is drawn **offshore on the left, the coast on the right**, matching the map
beside it (a CalCOFI line runs west-south-west from the coast) and the CalCOFI
Explorer's Sections lens. The x-axis carries **both rulers — distance below,
station number above** — because they are the same ruler: `+proj=calcofi` is
equidistant along a line at **7.386 km = 3.99 nmi per station unit**, constant to
0.04 % over the 665 km of line 90, so station number is distance rescaled. That
is why the station labels can sit on a distance axis without distorting either
the field or the GEBCO seafloor drawn under it.

The prompt was Dan Rudnick's
[CUGN glider climatology](https://spraydata.ucsd.edu/products/cugn-climatology/) —
good plots, but nothing on the page says *where* the section is. Hence the map.

## How it works

```
GCS release parquet ──build_sections.sql──> public/data/_*.parquet   (flat)
  (obs, sample, grid,                                │
   cruise, ship, climatology,        build_sections.py
   measurement_type)                                 │
                                                     ▼
                         public/data/index.json      lines, cruises, variables
                         public/data/stations.json   the CalCOFI grid, for the map
                         public/data/sections/       one shard per (line, cruise)
```

Each shard holds every variable as a **station × depth matrix** (10 m floor
bins to 500 m, the release's `depth_bin` convention) — about 11 × 51 numbers per
variable per view, ~20 KB per shard, 671 shards. The app fetches one at a time.

The anomaly view subtracts the release's own **`climatology`** table
(`calcofi4db::build_climatology()`: station × calendar month × 10 m bin,
1993–2013, at least 3 cruises per cell), which the CalCOFI Explorer's Sections
lens subtracts too — one baseline, so the two products cannot disagree. For a
release that predates the table, `scripts/resolve_release.py` inlines
`scripts/climatology_fallback.sql` (the same definition) and says so.

That matrix goes straight to a Plotly `heatmap` with `zsmooth: "best"`. There is
**no interpolation code in the browser**: `ctd-viz` gets its smooth ODV-style field
from `MBA::mba.surf()`, a multilevel B-spline with no JavaScript port, and letting
the renderer resample the matrix gets the same look without reimplementing it.

The release version is resolved at build time from
[`latest.txt`](https://storage.googleapis.com/calcofi-db/ducklake/releases/latest.txt),
never hardcoded, and its parquet is read **through the release's `catalog.json`**:
`scripts/build_sections.sql` is a template whose `__TBL:<table>__` tokens
`scripts/resolve_release.py` (stdlib only) renders into `read_parquet()` over each
table's objects — content-addressed under `ducklake/tables/` since the v2026.09
releases (one immutable file per table or partition, listed in the catalog), the
legacy `releases/<version>/parquet/` path before that, which is now only guaranteed
for the promoted and consolidated versions. Same rule as
`calcofi4py.release.release_sources()` / `calcofi4r::cc_release_sources()`;
`scripts/test_resolve_release.py` pins the exact URLs for both catalog shapes.

## Build

```bash
python3 scripts/resolve_release.py             # latest.txt + catalog -> build/build_sections.sql  (--version vYYYY.MM.DD to pin)
duckdb -c ".read build/build_sections.sql"     # query the release  (~30 s)
pip install -r requirements.txt
python scripts/build_sections.py               # reshape into shards (~6 s)

cd public && python3 -m http.server 8777       # then open localhost:8777
```

CI does exactly this — see `.github/workflows/refresh.yml`, which runs weekly, on
manual dispatch, and on a `db-release` `repository_dispatch` fired by
`CalCOFI/workflows` when a release is promoted.

### Bathymetry is a committed input, not a build output

`metadata/station_bathymetry.csv` and `metadata/line_bathymetry.csv` are generated
**by hand** with `scripts/build_station_bathymetry.R`. They only go stale if the
CalCOFI grid changes, which it does not, so CI does not carry a raster or a GDAL
stack to recompute numbers that never move.

`line_bathymetry.csv` samples the seafloor every **500 m along** each line rather
than once per station, via `calcofi4r::cc_transect_bathy()` — the same function
`apps/ctd-viz` draws its silhouette with, so the two apps cannot show different
seafloors. `calcofi4r::cc_bathy()` fetches the GEBCO 2025 crop from
`gs://calcofi-db/bathymetry/` and caches it; the script no longer needs a sibling
`apps/ctd-viz` checkout.

Sampling only at stations and joining the points invents terrain: on line 86.7,
station 50 sits on a Channel Islands bank at 80 m with neighbours 37 km away in
1,200–1,650 m of water, and the straight-line version drew that as a single
triangle 74 km wide and 1.5 km tall — right where a reader is looking for the
thermocline.

Too coarse an interval fails the same way, more quietly. The first fix here
sampled at 2 km against a grid whose own cell is ~390 m, and line 93.3 still drew
three spikes: Fortymile Bank is a ~14 km rise from 652 m to a 178 m crest, and at
2 km it is four soundings (385, 344, 238, 370 m). 500 m keeps every cell the line
crosses without implying detail GEBCO does not have.

## Data stages

`sample.data_stage` reaches consumers with three values, and the app surfaces it as
a badge because it changes what the numbers mean:

| stage | badge | what it means |
|---|---|---|
| `final` | Final 1 m-binned | fully processed |
| `preliminary_with_bottle` | Preliminary — CTD & bottle | bottle merge done; values may still shift after post-cruise calibration |
| `preliminary_without_bottle` | Preliminary — CTD only | **no bottle merge yet**, so bottle-corrected salinity, oxygen and chlorophyll do not exist for this cruise |

On a `preliminary_without_bottle` cruise the app offers the **uncorrected** sensor series
(`salinity_1`, `oxygen_ml_l_1`), clearly labelled, instead of an empty panel. The
uncorrected series are hidden whenever the corrected ones exist, so the picker
never shows two near-identical salinities.

## Links

- Ingest workflow: [`ingest_calcofi_ctd-cast`](https://calcofi.io/workflows/ingest_calcofi_ctd-cast.html)
- Schema: [calcofi.io/schema](https://calcofi.io/schema/)
- Source data: [CalCOFI CTD Cast Files](https://calcofi.org/data/oceanographic-data/ctd-cast-files/)
