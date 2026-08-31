-- build_sections.sql — CalCOFI CTD cross-shelf transects -> public/data/_sections.parquet
--
--   python3 scripts/resolve_release.py               # renders this template -> build/build_sections.sql
--   duckdb -c ".read build/build_sections.sql"       (needs duckdb CLI + network)
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
-- The baseline is the release's own `climatology` table (calcofi4db::
-- build_climatology(), run once when the release is cut): a plain mean per
-- dataset x station x calendar month x 10 m floor depth bin x measurement type
-- over 1993-2013, kept where at least 3 distinct cruises contribute, the window
-- stamped on every row (clim_yr_min / clim_yr_max). This app, the CalCOFI
-- Explorer's Sections lens and calcofi4r::cc_climatology() all subtract that one
-- table — until 2026-08-31 each computed its own and they had drifted (one pooled
-- all months, one binned a thinned 10 m series at 5 m, none shared a floor), so
-- the same July 2026 section read +1.4 degC here and ~0 in the explorer.
--
-- Why 1993-2013: the window Rasmus Swalethorp asked for (CCIEA), available since
-- the Wilkinson archive backfilled 1993-2002; 21 years with both phases of the
-- 1997-99 ENSO inside it, ending before the 2014-16 marine heatwave so the
-- heatwave and what follows read as departures. NOT a WMO 30-year normal.
--
-- For a release that predates the table, resolve_release.py substitutes
-- scripts/climatology_fallback.sql (the same definition, inline) and warns.

-- ── where the release's parquet lives ────────────────────────────────────────
-- This file is a TEMPLATE. scripts/resolve_release.py resolves the release (the
-- same latest.txt every other consumer reads, or the version refresh.yml hands
-- it — never a hardcoded tag), fetches its catalog.json, and renders every
-- `__TBL:<table>__` token below into the read_parquet() over that table's parquet
-- objects: content-addressed under ducklake/tables/ since the v2026.09 releases
-- (one immutable object per table or partition, listed in the catalog), the
-- legacy releases/<version>/parquet/ path before that. `__RELEASE__` becomes the
-- version string. Never write either path by hand here: the per-release path is
-- only guaranteed to answer for the promoted and consolidated versions.

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
FROM __TBL:grid__
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
FROM __TBL:sample__ s
JOIN station g USING (grid_key)
WHERE s.dataset_key = 'calcofi_ctd-cast'
  AND s.sample_type = 'cast'
QUALIFY row_number() OVER (
  PARTITION BY s.cruise_key, s.grid_key
  ORDER BY right(s.sample_key, 1) = 'd' DESC, s.datetime) = 1;

-- ── the section values ───────────────────────────────────────────────────────
-- Depth binned to 10 m FLOOR bins (floor(depth_min_m / 10) * 10, labelled by the
-- shallow edge — the release's obs_env.depth_bin convention, and the grain of its
-- `climatology` table) and capped at 500 m. The cap is what makes the sections
-- comparable: a handful of casts go to 5000 m, and letting them set the y-axis
-- would squash every standard 500 m cast into the top tenth of the plot.
--
-- Not 5 m, as this was until 2026-08-31: `obs` carries the THINNED CTD series (a
-- 10 m grid + RDP inflection points + bottle depths), so at 5 m the off-grid bins
-- held about a third of the casts, sampled exactly where the profile bends, and
-- their means sat visibly off their neighbours' (station 60, July: bin 25 at
-- 14.27 degC between 15.39 and 15.04). Every 10 m bin holds every cast.
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
       (floor(o.depth_min_m / 10) * 10)::INTEGER AS depth_m,
       ROUND(AVG(o.measurement_value), 4) AS value
FROM __TBL:obs__ o
JOIN ctd_cast c USING (sample_key)
WHERE o.dataset_key = 'calcofi_ctd-cast'
  AND o.measurement_value IS NOT NULL
  -- quality flags: CTD 8 = questionable, 9 = bad/missing (1/2 are sensor-selection
  -- hints, not grades). NULL-safe. Same predicate as calcofi4r::cc_qual_ok_sql().
  AND COALESCE(regexp_replace(o.measurement_qual, '\.0+$', '') NOT IN ('8', '9'), TRUE)
  AND o.depth_min_m IS NOT NULL
  AND o.depth_min_m < 510
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
-- Read from the release, never recomputed here (see the header). `depth_bin` is
-- the same 10 m floor bin as `section.depth_m`, so the join below is exact.
CREATE TEMP TABLE climatology AS
SELECT grid_key,
       month            AS mon,
       depth_bin        AS depth_m,
       measurement_type AS var,
       clim_mean, clim_sd, clim_n, n_cruises, clim_yr_min, clim_yr_max
FROM __TBL:climatology__
WHERE dataset_key = 'calcofi_ctd-cast'
  AND measurement_type IN (SELECT DISTINCT var FROM section);

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
       cl.clim_n,
       cl.n_cruises
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
FROM __TBL:cruise__ cr
LEFT JOIN __TBL:ship__ sh USING (ship_key);

-- ── measurement labels ───────────────────────────────────────────────────────
CREATE TEMP TABLE variable AS
SELECT measurement_type AS var, description, units, valid_min, valid_max
FROM __TBL:measurement_type__
WHERE measurement_type IN (SELECT DISTINCT var FROM section);

-- ── export flat intermediates for the reshaper ───────────────────────────────
COPY section         TO 'public/data/_sections.parquet'  (FORMAT PARQUET);
COPY section_station TO 'public/data/_stations.parquet'  (FORMAT PARQUET);
COPY section_anomaly TO 'public/data/_anomaly.parquet'   (FORMAT PARQUET);
COPY cruise          TO 'public/data/_cruises.parquet'   (FORMAT PARQUET);
COPY variable        TO 'public/data/_variables.parquet' (FORMAT PARQUET);
COPY (SELECT * FROM station) TO 'public/data/_grid.parquet' (FORMAT PARQUET);

-- the baseline the reshaper stamps into index.json for the app to display — read
-- off the table's own rows, so the app cannot print a window the data was not
-- averaged over
COPY (SELECT any_value(clim_yr_min)           AS yr_min,
             any_value(clim_yr_max)           AS yr_max,
             min(n_cruises)                   AS min_cruises,
             count(*)                         AS n_cells,
             (SELECT count(DISTINCT cruise_key) FROM ctd_cast
              WHERE year(datetime) BETWEEN (SELECT any_value(clim_yr_min) FROM climatology)
                                       AND (SELECT any_value(clim_yr_max) FROM climatology)) AS n_cruises
      FROM climatology)
     TO 'public/data/_baseline.parquet' (FORMAT PARQUET);

SELECT '__RELEASE__'                                         AS release,
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
