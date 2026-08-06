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
COPY cruise          TO 'public/data/_cruises.parquet'   (FORMAT PARQUET);
COPY variable        TO 'public/data/_variables.parquet' (FORMAT PARQUET);
COPY (SELECT * FROM station) TO 'public/data/_grid.parquet' (FORMAT PARQUET);

SELECT getvariable('release_version')                        AS release,
       (SELECT count(*) FROM section)                        AS n_values,
       (SELECT count(DISTINCT (line, cruise_key)) FROM section) AS n_sections,
       (SELECT count(DISTINCT var) FROM section)             AS n_variables,
       (SELECT count(DISTINCT cruise_key) FROM section)      AS n_cruises;
