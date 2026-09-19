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

-- ── sensor-pair combination (ctd-transects#1) ────────────────────────────────
-- Mirrors calcofi4db::combine_sensor_pair_sql() exactly (R package — not
-- callable from this plain-DuckDB file, so its generated CASE expression is
-- transcribed here by hand). The rule, in precedence order:
--   1. a sensor flagged 8 (questionable) or 9 (bad) is dropped;
--   2. a 1 (use primary) on either sensor's flag selects sensor 1 alone, a 2
--      (use secondary) selects sensor 2 alone, when both flags agree; if they
--      disagree, neither is trusted over the other and the mean is used;
--   3. otherwise the mean of the two; one alone when the other is NULL; NULL
--      when neither survives.
-- Flags are compared as the source writes them ("8", "8.0", 8 all mean the
-- same thing), matching calcofi4r::cc_qual_ok_sql()'s normalisation.
-- Before trusting this in a release build: run the six calcofi4db
-- combine_sensor_pair() doc examples through both this macro and the R
-- function and confirm they agree.
CREATE MACRO combine_sensor_pair(v1, v2, q1, q2) AS (
  CASE
    WHEN COALESCE((regexp_replace(CAST(q1 AS VARCHAR), '\.0+$', '') = '1'
                OR regexp_replace(CAST(q2 AS VARCHAR), '\.0+$', '') = '1'), FALSE)
     AND NOT COALESCE((regexp_replace(CAST(q1 AS VARCHAR), '\.0+$', '') = '2'
                    OR regexp_replace(CAST(q2 AS VARCHAR), '\.0+$', '') = '2'), FALSE)
     AND (CASE WHEN regexp_replace(CAST(q1 AS VARCHAR), '\.0+$', '') IN ('8', '9') THEN NULL ELSE v1 END) IS NOT NULL
    THEN (CASE WHEN regexp_replace(CAST(q1 AS VARCHAR), '\.0+$', '') IN ('8', '9') THEN NULL ELSE v1 END)

    WHEN COALESCE((regexp_replace(CAST(q1 AS VARCHAR), '\.0+$', '') = '2'
                OR regexp_replace(CAST(q2 AS VARCHAR), '\.0+$', '') = '2'), FALSE)
     AND NOT COALESCE((regexp_replace(CAST(q1 AS VARCHAR), '\.0+$', '') = '1'
                    OR regexp_replace(CAST(q2 AS VARCHAR), '\.0+$', '') = '1'), FALSE)
     AND (CASE WHEN regexp_replace(CAST(q2 AS VARCHAR), '\.0+$', '') IN ('8', '9') THEN NULL ELSE v2 END) IS NOT NULL
    THEN (CASE WHEN regexp_replace(CAST(q2 AS VARCHAR), '\.0+$', '') IN ('8', '9') THEN NULL ELSE v2 END)

    WHEN (CASE WHEN regexp_replace(CAST(q1 AS VARCHAR), '\.0+$', '') IN ('8', '9') THEN NULL ELSE v1 END) IS NOT NULL
     AND (CASE WHEN regexp_replace(CAST(q2 AS VARCHAR), '\.0+$', '') IN ('8', '9') THEN NULL ELSE v2 END) IS NOT NULL
    THEN ((CASE WHEN regexp_replace(CAST(q1 AS VARCHAR), '\.0+$', '') IN ('8', '9') THEN NULL ELSE v1 END)
        + (CASE WHEN regexp_replace(CAST(q2 AS VARCHAR), '\.0+$', '') IN ('8', '9') THEN NULL ELSE v2 END)) / 2

    ELSE COALESCE(
      (CASE WHEN regexp_replace(CAST(q1 AS VARCHAR), '\.0+$', '') IN ('8', '9') THEN NULL ELSE v1 END),
      (CASE WHEN regexp_replace(CAST(q2 AS VARCHAR), '\.0+$', '') IN ('8', '9') THEN NULL ELSE v2 END))
  END
);

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

-- ── one cast per (cruise, station) — the STATION, not the grid cell ──────────
-- `sample.site_key` is the real line/station ("090.0 028.0"); `grid_key` is the
-- ~2,350 km² cell it falls in. The inshore cells of the core lines each hold 2–4
-- stations occupied every cruise since 2004 (st30-ln90 = 90.30, 90.28, 90.27.7
-- and 88.5/30.1: 3.7 occupations per cruise), and until 2026-09-09 this partition
-- was (cruise_key, grid_key): 1,595 of 9,637 occupations (16.6 %; 26 % on line 90
-- since 2004) never drew, and which one drew was whichever the ship reached
-- first. The station is parsed from site_key; the cell still supplies the line
-- (a station logged as 93.4 sits 2 km off line 93.3 and belongs to it), the
-- shore class and the nominal position. A station whose own line is more than
-- 0.5 units from the cell's line (88.5/30.1 inside st30-ln90) is not on this
-- line and is left out rather than drawn 17 km from where it was.
--
-- `obs` is already effectively single-direction (the ingest's ctd_thin picks one
-- physical direction per cast, downcast preferred); the QUALIFY keeps that
-- explicit — one row per (cruise, line, station), the downcast first.
CREATE TEMP TABLE ctd_cast AS
SELECT s.sample_key, s.cruise_key, s.grid_key, s.site_key,
       g.line,
       TRY_CAST(split_part(s.site_key, ' ', 2) AS DOUBLE) AS sta,
       g.lon AS grid_lon, g.lat AS grid_lat,
       s.latitude, s.longitude, s.datetime, s.data_stage
FROM __TBL:sample__ s
JOIN station g USING (grid_key)
WHERE s.dataset_key = 'calcofi_ctd-cast'
  AND s.sample_type = 'cast'
  AND s.site_key IS NOT NULL
  AND abs(TRY_CAST(split_part(s.site_key, ' ', 1) AS DOUBLE) - g.line) <= 0.5
QUALIFY row_number() OVER (
  PARTITION BY s.cruise_key, g.line, TRY_CAST(split_part(s.site_key, ' ', 2) AS DOUBLE)
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
--
-- SENSOR-PAIR VARIABLES (ctd-transects#1). Salinity and oxygen are true sensor
-- pairs — two physical sensors to combine, so they get the pivot +
-- combine_sensor_pair() treatment below. Chlorophyll and nitrate are NOT: per
-- workflows/libs/build_ctd_measurement_registry.R, est_chlorophyll_a_*/
-- est_nitrate_* are each a single fluorometer/ISUS-derived estimate with one
-- quality flag (fluor_q / isusq) — no second sensor to reconcile — so they need
-- no macro, just their own two measurement_type strings added to the
-- straight-from-obs list below, same as temperature/sigma_theta/fluorescence.
--
-- No longer read from the release's precomputed *_ave_* fields (how they treat
-- a flagged sensor is undocumented). Computed here instead, via
-- combine_sensor_pair() above. `obs` gives one row per sensor (long format);
-- the macro needs both sensors in one row (wide), so each pair is pivoted
-- first. Deliberately NOT run through the blanket 8/9 filter below — the macro
-- needs to see each sensor's own flag to decide, so filtering a flagged row
-- out first would hide it from the rule that needs it.
CREATE TEMP TABLE salinity_pair AS
SELECT sample_key,
       depth_min_m,
       MAX(CASE WHEN measurement_type = 'salinity_1_corr' THEN measurement_value END) AS s1,
       MAX(CASE WHEN measurement_type = 'salinity_1_corr' THEN measurement_qual  END) AS q1,
       MAX(CASE WHEN measurement_type = 'salinity_2_corr' THEN measurement_value END) AS s2,
       MAX(CASE WHEN measurement_type = 'salinity_2_corr' THEN measurement_qual  END) AS q2
FROM __TBL:obs__
WHERE dataset_key = 'calcofi_ctd-cast'
  AND measurement_type IN ('salinity_1_corr', 'salinity_2_corr')
  AND depth_min_m IS NOT NULL
  AND depth_min_m < 510
GROUP BY sample_key, depth_min_m;

CREATE TEMP TABLE salinity_combined AS
SELECT sample_key, depth_min_m,
       'salinity_ave_corr' AS var,
       combine_sensor_pair(s1, s2, q1, q2) AS value
FROM salinity_pair;

-- oxygen: TWO pairs (station-corrected and cruise-corrected are different fits,
-- both wanted — Rasmus), so two pivots and two combines, kept as two variables.
CREATE TEMP TABLE oxygen_sta_pair AS
SELECT sample_key,
       depth_min_m,
       MAX(CASE WHEN measurement_type = 'oxygen_ml_l_1_sta_corr' THEN measurement_value END) AS s1,
       MAX(CASE WHEN measurement_type = 'oxygen_ml_l_1_sta_corr' THEN measurement_qual  END) AS q1,
       MAX(CASE WHEN measurement_type = 'oxygen_ml_l_2_sta_corr' THEN measurement_value END) AS s2,
       MAX(CASE WHEN measurement_type = 'oxygen_ml_l_2_sta_corr' THEN measurement_qual  END) AS q2
FROM __TBL:obs__
WHERE dataset_key = 'calcofi_ctd-cast'
  AND measurement_type IN ('oxygen_ml_l_1_sta_corr', 'oxygen_ml_l_2_sta_corr')
  AND depth_min_m IS NOT NULL
  AND depth_min_m < 510
GROUP BY sample_key, depth_min_m;

CREATE TEMP TABLE oxygen_cruise_pair AS
SELECT sample_key,
       depth_min_m,
       MAX(CASE WHEN measurement_type = 'oxygen_ml_l_1_cruise_corr' THEN measurement_value END) AS s1,
       MAX(CASE WHEN measurement_type = 'oxygen_ml_l_1_cruise_corr' THEN measurement_qual  END) AS q1,
       MAX(CASE WHEN measurement_type = 'oxygen_ml_l_2_cruise_corr' THEN measurement_value END) AS s2,
       MAX(CASE WHEN measurement_type = 'oxygen_ml_l_2_cruise_corr' THEN measurement_qual  END) AS q2
FROM __TBL:obs__
WHERE dataset_key = 'calcofi_ctd-cast'
  AND measurement_type IN ('oxygen_ml_l_1_cruise_corr', 'oxygen_ml_l_2_cruise_corr')
  AND depth_min_m IS NOT NULL
  AND depth_min_m < 510
GROUP BY sample_key, depth_min_m;

CREATE TEMP TABLE oxygen_combined AS
SELECT sample_key, depth_min_m,
       'oxygen_ml_l_ave_sta_corr' AS var,          -- same name the release used to publish
       combine_sensor_pair(s1, s2, q1, q2) AS value
FROM oxygen_sta_pair
UNION ALL
SELECT sample_key, depth_min_m,
       'oxygen_ml_l_ave_cruise_corr' AS var,       -- new: no equivalent existed before
       combine_sensor_pair(s1, s2, q1, q2) AS value
FROM oxygen_cruise_pair;

CREATE TEMP TABLE section AS
SELECT c.line,
       c.cruise_key,
       c.sta,
       x.var,
       (floor(x.depth_min_m / 10) * 10)::INTEGER AS depth_m,
       ROUND(AVG(x.value), 4) AS value
FROM (
  -- unchanged variables, straight from obs — now including chlorophyll and
  -- nitrate (single-flag estimates, no sensor pair to combine — see above)
  SELECT sample_key, depth_min_m, measurement_type AS var, measurement_value AS value
  FROM __TBL:obs__
  WHERE dataset_key = 'calcofi_ctd-cast'
    AND measurement_value IS NOT NULL
    -- quality flags: CTD 8 = questionable, 9 = bad/missing (1/2 are sensor-selection
    -- hints, not grades). NULL-safe. Same predicate as calcofi4r::cc_qual_ok_sql().
    AND COALESCE(regexp_replace(measurement_qual, '\.0+$', '') NOT IN ('8', '9'), TRUE)
    AND depth_min_m IS NOT NULL
    AND depth_min_m < 510
    AND measurement_type IN (
      'temperature_ave',
      'sigma_theta_1',
      'fluorescence_v',
      'salinity_1',
      'oxygen_ml_l_1',
      'est_chlorophyll_a_sta_corr',
      'est_chlorophyll_a_cruise_corr',
      'est_nitrate_sta_corr',
      'est_nitrate_cruise_corr')

  UNION ALL

  -- salinity and oxygen: sensor-pair combined above, flag handling already applied
  SELECT sample_key, depth_min_m, var, value FROM salinity_combined WHERE value IS NOT NULL
  UNION ALL
  SELECT sample_key, depth_min_m, var, value FROM oxygen_combined   WHERE value IS NOT NULL
) x
JOIN ctd_cast c USING (sample_key)
GROUP BY ALL;

-- ── cast-level metadata, one row per (line, cruise, station) ─────────────────
CREATE TEMP TABLE section_station AS
SELECT c.line, c.cruise_key, c.sta, c.grid_key, c.site_key, c.shore,
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
SELECT site_key,
       grid_key,
       month            AS mon,
       depth_bin        AS depth_m,
       measurement_type AS var,
       clim_mean, clim_sd, clim_n, n_cruises, clim_yr_min, clim_yr_max
FROM __TBL:climatology__
WHERE dataset_key = 'calcofi_ctd-cast'
  AND measurement_type IN (SELECT DISTINCT var FROM section);

-- ── the anomaly ──────────────────────────────────────────────────────────────
-- value - clim_mean, matched on the STATION (site_key, since the release's
-- climatology moved to that grain, calcofi4db 4.8.0), calendar month and depth bin.
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
  ON cl.site_key = ss.site_key
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
