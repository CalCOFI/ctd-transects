-- climatology_fallback.sql — the release's `climatology` table, computed inline for a
-- release that predates it (before v2026.09).
--
-- resolve_release.py substitutes this SELECT, parenthesised, wherever build_sections.sql
-- says __TBL:climatology__ and the catalog has no such table — and says so on stderr.
-- It is the SAME definition as calcofi4db::build_climatology(), which is what the release
-- runs: a plain mean per dataset x station x calendar month x 10 m floor depth bin x
-- measurement type over 1993-2013, kept where at least 3 distinct cruises contribute.
-- Restricted to the CTD dataset here because that is the only one this app reads; the
-- release table carries every env dataset. Delete this file once every release this app
-- can be pointed at ships the table.
SELECT o.dataset_key,
       o.grid_key,
       month(o.datetime)::TINYINT                           AS month,
       (floor(o.depth_min_m / 10) * 10)::INTEGER            AS depth_bin,
       o.measurement_type,
       avg(o.measurement_value)                             AS clim_mean,
       stddev_samp(o.measurement_value)                     AS clim_sd,
       count(*)::INTEGER                                    AS clim_n,
       count(DISTINCT o.cruise_key)::INTEGER                AS n_cruises,
       1993::SMALLINT                                       AS clim_yr_min,
       2013::SMALLINT                                       AS clim_yr_max
FROM __TBL:obs__ o
WHERE o.realm = 'env'
  AND o.dataset_key = 'calcofi_ctd-cast'
  AND o.grid_key IS NOT NULL AND o.datetime IS NOT NULL
  AND o.depth_min_m IS NOT NULL AND o.depth_min_m >= 0 AND o.depth_min_m < 510
  AND o.measurement_value IS NOT NULL AND isfinite(o.measurement_value)
  AND year(o.datetime) BETWEEN 1993 AND 2013
  AND COALESCE(regexp_replace(o.measurement_qual, '\.0+$', '') NOT IN ('8', '9'), TRUE)
GROUP BY ALL
HAVING count(DISTINCT o.cruise_key) >= 3
