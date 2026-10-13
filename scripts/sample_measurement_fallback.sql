-- sample_measurement_fallback.sql — an EMPTY `sample_measurement` for a release that
-- predates the table.
--
-- resolve_release.py substitutes this SELECT, parenthesised, wherever build_sections.sql
-- says __TBL:sample_measurement__ and the catalog has no such table — and says so on
-- stderr. build_sections.sql reads it only for the per-cast mixed-layer-depth and
-- chlorophyll-maximum lines (calcofi_ctd-derived); a release without the table has no
-- such values, so the section draws with no lines rather than the build failing.
-- Same columns and types as the release table.
SELECT NULL::BIGINT  AS sample_measurement_id,
       NULL::VARCHAR AS sample_key,
       NULL::VARCHAR AS dataset_key,
       NULL::VARCHAR AS measurement_type,
       NULL::DOUBLE  AS measurement_value,
       NULL::VARCHAR AS measurement_qual
WHERE FALSE
