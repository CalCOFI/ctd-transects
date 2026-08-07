-- build_sections.sql — CalCOFI CTD cross-shelf transects -> public/data/_sections.parquet
--
--   duckdb -c ".read scripts/build_sections.sql"     (needs duckdb CLI + network)
--
-- Emits ONE flat intermediate; scripts/build_sections.py reshapes it into the
-- per-(line, cruise) JSON shards the app fetches. The split is deliberate: SQL is
-- good at the filtering/binning/dedup below, and bad at pivoting a ragged
-- station x depth matrix, which is three lines of Python.
--
-- ORDERING IS BY LINE AND STATION, NOT BY THE SHIP'S TRACK.
--   apps/ctd-viz orders a transect by `ord_occ` (the order stations were occupied)
--   and asks the user to click a start and an end station. That gives whatever
--   direction the ship happened to steam, which is why it cannot be automated.
--   Here a transect is a CalCOFI line, ordered by station number ascending =
--   nearshore -> offshore (calcofi4r/R/data.R sets is_offshore = station > 60;
--   station 100 on line 76.7 sits at -124.3 degrees lon). That is well defined for
--   every cruise with no user input, which is the whole point.
--
--   `order_occ` could not be used even if we wanted it: it is NULL on roughly half
--   of sample's cast rows in the release.

INSTALL httpfs; LOAD httpfs;
INSTALL spatial; LOAD spatial;

-- ── the climatological baseline ──────────────────────────────────────────────
-- ONE knob, stated here and carried into index.json so the app can print it. An
-- anomaly whose baseline is not on screen is not interpretable, and a baseline
-- buried in a build script is not on screen.
--
-- 1993-2013 is the window Rasmus Swalethorp asked for (CCIEA), and it is
-- available for the first time in the 2026-08 release: the Wilkinson archive
-- backfills 1993-08 through 2002, where the published record used to jump
-- 1998 -> 2003. Before that ingest this range would have quietly meant
-- "1998 plus 2003-2013".
--
-- 21 years is long enough to average over ENSO (1997-98 El Nino and 1998-99
-- La Nina both fall inside it) and it ends before the 2014-16 marine heatwave,
-- so the heatwave and what follows read as departures rather than being folded
-- into the normal. It is NOT a WMO 30-year normal; CalCOFI's CTD record cannot
-- support one without reaching into ship-hydrocast data of a different vintage.
SET VARIABLE clim_yr_min = 1993;
SET VARIABLE clim_yr_max = 2013;
-- a cell needs this many observations to be a baseline at all; below it the
-- anomaly is NULL rather than a difference against one lucky cast
SET VARIABLE clim_min_n  = 3;

-- Resolve the current release from the same latest.txt every other consumer reads
-- (build_workflows_index.R, db-viz-station's build_depth_profiles.sql). Never
-- hardcode a release tag. Resolved into a session variable rather than referenced
-- as a subquery inside the macro, because DuckDB table functions like
-- read_parquet() cannot accept a macro whose argument contains a subquery
-- ("Table function cannot contain subqueries").
CREATE TEMP TABLE _release AS
  SELECT regexp_replace(content, '\s+$', '') AS version
  FROM read_text('https://storage.googleapis.com/calcofi-db/ducklake/releases/latest.txt');
SET VARIABLE release_version = (SELECT version FROM _release);

CREATE TEMP MACRO r(p) AS
  'https://storage.googleapis.com/calcofi-db/ducklake/releases/'
  || getvariable('release_version') || '/parquet/' || p;

-- ── the CalCOFI grid: line/station geometry ──────────────────────────────────
-- 218 rows. `geom_ctr` is the station centre; the app draws these on the map and
-- uses the lon/lat to compute along-transect distance.
CREATE TEMP TABLE station AS
SELECT grid_key,
       line,
       station AS sta,
       shore,
       ST_X(geom_ctr) AS lon,
       ST_Y(geom_ctr) AS lat
FROM read_parquet(r('grid.parquet'))
WHERE line IS NOT NULL AND station IS NOT NULL;

-- ── one cast per (cruise, station) ───────────────────────────────────────────
-- `obs` is already effectively single-direction (the ingest's ctd_thin picks one
-- physical direction per cast, downcast preferred): 7,163 downcast vs 12 upcast
-- samples for temperature_ave, with only 5 stations carrying both. The QUALIFY
-- makes that explicit rather than leaving a doubled column on those 5.
CREATE TEMP TABLE ctd_cast AS
SELECT s.sample_key, s.cruise_key, s.grid_key,
       g.line, g.sta, g.lon AS grid_lon, g.lat AS grid_lat,
       s.latitude, s.longitude, s.datetime, s.data_stage
FROM read_parquet(r('sample.parquet')) s
JOIN station g USING (grid_key)
WHERE s.dataset_key = 'calcofi_ctd-cast'
  AND s.sample_type = 'cast'
QUALIFY row_number() OVER (
  PARTITION BY s.cruise_key, s.grid_key
  ORDER BY right(s.sample_key, 1) = 'd' DESC, s.datetime) = 1;

-- ── the section values ───────────────────────────────────────────────────────
-- Depth binned to 5 m and capped at ~500 m. Both matter for size, and the cap is
-- also what makes the sections comparable: a handful of casts go to 5000 m, and
-- letting them set the y-axis would squash every standard 500 m cast into the top
-- tenth of the plot.
--
-- Binning is not just compression. CTD sensors sample continuously (47.283 m,
-- 47.916 m, ...) so grouping by exact depth deduplicates almost nothing, and the
-- native-resolution jaggedness is sensor precision rather than signal.
--
-- VARIABLE LIST: the corrected forms first, then the uncorrected sensor series.
-- A preliminary_without_bottle cruise (sensor only, before the bottle merge) has NO
-- *_corr / *_sta_corr values at all — the correction is fitted against bottle
-- samples. Carrying the raw series is what keeps salinity and oxygen plottable on
-- the most recent cruises, which are exactly the ones a user opens first.
CREATE TEMP TABLE section AS
SELECT c.line,
       c.cruise_key,
       c.sta,
       o.measurement_type AS var,
       CAST(ROUND(o.depth_min_m / 5) * 5 AS INTEGER) AS depth_m,
       ROUND(AVG(o.measurement_value), 4) AS value
FROM read_parquet(r('obs.parquet')) o
JOIN ctd_cast c USING (sample_key)
WHERE o.dataset_key = 'calcofi_ctd-cast'
  AND o.measurement_value IS NOT NULL
  AND o.depth_min_m IS NOT NULL
  AND o.depth_min_m <= 515
  AND o.measurement_type IN (
    'temperature_ave',
    'salinity_ave_corr',
    'oxygen_ml_l_ave_sta_corr',
    'sigma_theta_1',
    'fluorescence_v',
    'salinity_1',
    'oxygen_ml_l_1')
GROUP BY ALL;

-- ── cast-level metadata, one row per (line, cruise, station) ─────────────────
CREATE TEMP TABLE section_station AS
SELECT c.line, c.cruise_key, c.sta, c.grid_key, c.shore,
       -- prefer the ACTUAL cast position; fall back to the nominal grid centre.
       -- A cast is occupied within a few km of the nominal station, and the real
       -- position is what the map should show.
       COALESCE(c.longitude, c.grid_lon) AS lon,
       COALESCE(c.latitude,  c.grid_lat) AS lat,
       c.datetime,
       c.data_stage
FROM (SELECT c.*, g.shore FROM ctd_cast c JOIN station g USING (grid_key)) c
WHERE c.sta IN (SELECT DISTINCT sta FROM section
                WHERE line = c.line AND cruise_key = c.cruise_key);

-- ── climatology: the baseline every anomaly is a departure from ──────────────
-- A plain monthly mean per (station, 5 m depth bin, calendar month) over the
-- window set at the top of this file. This mirrors `calcofi4r::cc_climatology()`
-- — the two are separate implementations of ONE definition, because the app's CI
-- has no R and the notebook path has no DuckDB CLI. If you change the grouping or
-- the floor here, change it there:
--   https://github.com/CalCOFI/calcofi4r/blob/main/R/transect.R
--
-- Why (station, depth, month) and not something finer: that is the finest
-- grouping CalCOFI's design supports. Quarterly-ish cruises over three decades
-- give many YEARS per calendar month at a station, but only a handful of days.
--
-- Why a plain mean and not harmonics: Rudnick et al. (2017) fit annual and
-- semiannual harmonics for the CUGN glider climatology, which suits near-
-- continuous glider sampling. CalCOFI's is episodic and unevenly spaced, and a
-- monthly mean is both defensible and legible — a reader can say exactly what the
-- anomaly is a departure from. `clim_n` ships so a thin cell is visible, not
-- silently trusted.
CREATE TEMP TABLE climatology AS
SELECT c.grid_key,
       month(c.datetime) AS mon,
       CAST(ROUND(o.depth_min_m / 5) * 5 AS INTEGER) AS depth_m,
       o.measurement_type AS var,
       ROUND(AVG(o.measurement_value), 4)   AS clim_mean,
       ROUND(STDDEV_SAMP(o.measurement_value), 4) AS clim_sd,
       COUNT(*)                             AS clim_n
FROM read_parquet(r('obs.parquet')) o
JOIN ctd_cast c USING (sample_key)
WHERE o.dataset_key = 'calcofi_ctd-cast'
  AND o.measurement_value IS NOT NULL
  AND o.depth_min_m IS NOT NULL
  AND o.depth_min_m <= 515
  AND c.datetime IS NOT NULL
  AND year(c.datetime) BETWEEN getvariable('clim_yr_min') AND getvariable('clim_yr_max')
  AND o.measurement_type IN (SELECT DISTINCT var FROM section)
GROUP BY ALL
HAVING COUNT(*) >= getvariable('clim_min_n');

-- ── the anomaly ──────────────────────────────────────────────────────────────
-- value - clim_mean, matched on station, calendar month and depth bin.
--
-- An INNER join, so a cell with no baseline is ABSENT rather than zero. An
-- unsampled baseline is not a zero anomaly, and collapsing the two is how a plot
-- ends up colouring "normal" somewhere never measured. The reshaper leaves those
-- cells null and the heatmap leaves them blank.
--
-- anomaly_sd expresses the departure in baseline standard deviations, which is
-- what makes 1 degC interpretable: large at 200 m, unremarkable at the surface.
CREATE TEMP TABLE section_anomaly AS
SELECT s.line, s.cruise_key, s.sta, s.var, s.depth_m,
       -- 2 dp, not 4: the anomaly is a DISPLAY quantity derived from values
       -- already shipped at full precision, and the extra digits cost ~16% of
       -- every shard to render a colour nobody can distinguish
       ROUND(s.value - cl.clim_mean, 2) AS anomaly,
       CASE WHEN cl.clim_sd > 0
            THEN ROUND((s.value - cl.clim_mean) / cl.clim_sd, 4) END AS anomaly_sd,
       cl.clim_mean,
       cl.clim_n
FROM section s
JOIN section_station ss
  ON ss.line = s.line AND ss.cruise_key = s.cruise_key AND ss.sta = s.sta
JOIN climatology cl
  ON cl.grid_key = ss.grid_key
 AND cl.mon      = month(ss.datetime)
 AND cl.depth_m  = s.depth_m
 AND cl.var      = s.var;

-- ── cruise labels ────────────────────────────────────────────────────────────
CREATE TEMP TABLE cruise AS
SELECT cr.cruise_key,
       cr.ship_key,
       sh.ship_name
FROM read_parquet(r('cruise.parquet')) cr
LEFT JOIN read_parquet(r('ship.parquet')) sh USING (ship_key);

-- ── measurement labels ───────────────────────────────────────────────────────
CREATE TEMP TABLE variable AS
SELECT measurement_type AS var, description, units, valid_min, valid_max
FROM read_parquet(r('measurement_type.parquet'))
WHERE measurement_type IN (SELECT DISTINCT var FROM section);

-- ── export flat intermediates for the reshaper ───────────────────────────────
COPY section         TO 'public/data/_sections.parquet'  (FORMAT PARQUET);
COPY section_station TO 'public/data/_stations.parquet'  (FORMAT PARQUET);
COPY section_anomaly TO 'public/data/_anomaly.parquet'   (FORMAT PARQUET);
COPY cruise          TO 'public/data/_cruises.parquet'   (FORMAT PARQUET);
COPY variable        TO 'public/data/_variables.parquet' (FORMAT PARQUET);
COPY (SELECT * FROM station) TO 'public/data/_grid.parquet' (FORMAT PARQUET);

-- the baseline the reshaper stamps into index.json for the app to display
COPY (SELECT getvariable('clim_yr_min') AS yr_min,
             getvariable('clim_yr_max') AS yr_max,
             getvariable('clim_min_n')  AS min_n,
             (SELECT count(*) FROM climatology) AS n_cells,
             (SELECT count(DISTINCT cruise_key) FROM ctd_cast
              WHERE year(datetime) BETWEEN getvariable('clim_yr_min')
                                       AND getvariable('clim_yr_max')) AS n_cruises)
     TO 'public/data/_baseline.parquet' (FORMAT PARQUET);

SELECT getvariable('release_version')                        AS release,
       (SELECT count(*) FROM section)                        AS n_values,
       (SELECT count(DISTINCT (line, cruise_key)) FROM section) AS n_sections,
       (SELECT count(DISTINCT var) FROM section)             AS n_variables,
       (SELECT count(DISTINCT cruise_key) FROM section)      AS n_cruises,
       (SELECT count(*) FROM climatology)                    AS n_clim_cells,
       (SELECT count(*) FROM section_anomaly)                AS n_anomalies,
       -- what fraction of section values HAVE a baseline; a sharp drop here means
       -- either the baseline window missed the release's coverage or a station is
       -- newly sampled, and either way the anomaly view will be mostly blank
       ROUND(100.0 * (SELECT count(*) FROM section_anomaly)
                   / (SELECT count(*) FROM section), 1)      AS pct_with_baseline;
