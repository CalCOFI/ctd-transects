-- climatology_fallback.sql — the release's `climatology` table, computed inline for a
-- release that predates it (before v2026.09).
--
-- resolve_release.py substitutes this SELECT, parenthesised, wherever build_sections.sql
-- says __TBL:climatology__ and the catalog has no such table — and says so on stderr.
-- It is the SAME definition as calcofi4db::build_climatology(), which is what the release
-- runs: a plain mean per dataset x station x cruise month x 10 m floor depth bin x
-- measurement type over 1993-2013, kept where at least 5 distinct cruises contribute
-- (raised from 3 — Rasmus Swalethorp, 2026-09-09) —
-- the station being sample.site_key (the real line/station), not the grid cell, since
-- calcofi4db 4.8.0; grid_key rides along as the station's modal cell.
-- The month is the cruise's designated month (MM of cruise_key YYYY-MM-NODC), not the
-- cast's calendar month: a cast occupied on the last day of June for a July cruise
-- belongs to the July baseline. The year window stays on year(datetime).
-- Restricted to the two CTD datasets this app reads (ctd-cast, and ctd-derived where the
-- release has it); the release table carries every env dataset. Delete this file once every release this app
-- can be pointed at ships the table.
SELECT o.dataset_key,
       s.site_key,
       mode(o.grid_key)                                     AS grid_key,
       TRY_CAST(substr(o.cruise_key, 6, 2) AS TINYINT)      AS month,
       (floor(o.depth_min_m / 10) * 10)::INTEGER            AS depth_bin,
       o.measurement_type,
       avg(o.measurement_value)                             AS clim_mean,
       stddev_samp(o.measurement_value)                     AS clim_sd,
       count(*)::INTEGER                                    AS clim_n,
       count(DISTINCT o.cruise_key)::INTEGER                AS n_cruises,
       1993::SMALLINT                                       AS clim_yr_min,
       2013::SMALLINT                                       AS clim_yr_max
FROM __TBL:obs__ o
JOIN __TBL:sample__ s USING (sample_key)
WHERE o.realm = 'env'
  AND o.dataset_key IN ('calcofi_ctd-cast', 'calcofi_ctd-derived')
  AND s.site_key IS NOT NULL AND o.datetime IS NOT NULL
  AND o.depth_min_m IS NOT NULL AND o.depth_min_m >= 0 AND o.depth_min_m < 510
  AND o.measurement_value IS NOT NULL AND isfinite(o.measurement_value)
  AND year(o.datetime) BETWEEN 1993 AND 2013
  AND COALESCE(regexp_replace(o.measurement_qual, '\.0+$', '') NOT IN ('8', '9'), TRUE)
GROUP BY o.dataset_key, s.site_key, month, depth_bin, o.measurement_type
HAVING count(DISTINCT o.cruise_key) >= 5
