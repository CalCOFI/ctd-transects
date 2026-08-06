# CalCOFI CTD Transects

Cross-shelf temperature, salinity, oxygen, density and fluorescence sections along
every CalCOFI line, for every cruise in the integrated database — drawn
automatically, nearshore to offshore, with no station picking.

A static site: prebuilt JSON in `public/`, vanilla JavaScript, GitHub Pages. There
is no server and no database at request time.

## Why this exists

[`apps/ctd-viz`](https://github.com/CalCOFI/apps/tree/main/ctd-viz) already draws
CalCOFI CTD sections, but it is a Shiny app and it asks the user to define the
transect by clicking a start and an end station on a map. That makes it a tool for
someone who already knows what they are looking for, and it orders the section by
`ord_occ` — the order the ship occupied stations — so the direction is whichever
way the ship happened to steam.

Here a transect is **a CalCOFI line, ordered by station number ascending =
nearshore → offshore**. That is well defined for every cruise without any user
input, which is what makes the whole set pre-renderable and linkable.

The prompt was Dan Rudnick's
[CUGN glider climatology](https://spraydata.ucsd.edu/products/cugn-climatology/) —
good plots, but nothing on the page says *where* the section is. Hence the map.

## How it works

```
GCS release parquet ──build_sections.sql──> public/data/_*.parquet   (flat)
  (obs, sample, grid,                                │
   cruise, ship,                     build_sections.py
   measurement_type)                                 │
                                                     ▼
                         public/data/index.json      lines, cruises, variables
                         public/data/stations.json   the CalCOFI grid, for the map
                         public/data/sections/       one shard per (line, cruise)
```

Each shard holds every variable as a **station × depth matrix** (5 m bins to
500 m) — about 11 × 101 numbers per variable, ~32 KB per shard, 671 shards. The
app fetches one at a time.

That matrix goes straight to a Plotly `heatmap` with `zsmooth: "best"`. There is
**no interpolation code in the browser**: `ctd-viz` gets its smooth ODV-style field
from `MBA::mba.surf()`, a multilevel B-spline with no JavaScript port, and letting
the renderer resample the matrix gets the same look without reimplementing it.

The release version is resolved at build time from
[`latest.txt`](https://storage.googleapis.com/calcofi-db/ducklake/releases/latest.txt),
never hardcoded.

## Build

```bash
duckdb -c ".read scripts/build_sections.sql"   # query the release  (~30 s)
pip install -r requirements.txt
python scripts/build_sections.py               # reshape into shards (~6 s)

cd public && python3 -m http.server 8777       # then open localhost:8777
```

CI does exactly this — see `.github/workflows/refresh.yml`, which runs weekly, on
manual dispatch, and on a `db-release` `repository_dispatch` fired by
`CalCOFI/workflows` when a release is promoted.

### Bathymetry is a committed input, not a build output

`metadata/station_bathymetry.csv` and `metadata/line_bathymetry.csv` are generated
**by hand** with `scripts/build_station_bathymetry.R`, which needs the GEBCO raster
from a sibling `apps/ctd-viz` checkout. They only go stale if the CalCOFI grid
changes, which it does not, so CI does not carry a raster or a GDAL stack to
recompute numbers that never move.

`line_bathymetry.csv` samples the seafloor every 2 km **along** each line rather
than once per station. Sampling only at stations and joining the points invents
terrain: on line 86.7, station 50 sits on a Channel Islands bank at 80 m with
neighbours 37 km away in 1,200–1,650 m of water, and the straight-line version drew
that as a single triangle 74 km wide and 1.5 km tall — right where a reader is
looking for the thermocline.

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
