#!/usr/bin/env python3
"""Run the REAL scripts/build_sections.sql against a tiny local release fixture.

test_resolve_release.py proves the build's tokens resolve; this proves what the
build then DOES with the bytes, one small fixture per rule (ctd-transects#5, #6):

  * calcofi_ctd-derived rows (sigma_theta_ave, spiciness0) join their cast through
    ctd-cast's own sample_key and are drawn; sigma_theta_1 is not.
  * a provider flag outranks everything: a flagged ctd-cast value is not drawn, and
    a derived value whose inputs are flagged at that cast and depth is not drawn
    either (spiciness0 <- temperature_ave / salinity_ave_corr; sigma_theta_ave
    <- both sigma_theta sensors flagged). The summary counts what the guard dropped.
  * the anomaly subtracts the climatology of the dataset the value came from.

The fixture is written as parquet with the duckdb CLI (the same binary refresh.yml
installs), described by a catalog in the release's content-addressed shape, and
the rendered SQL is pointed at the local files by swapping the bucket prefix.
Skipped where no duckdb CLI is on PATH.

    python3 scripts/test_build_sections_sql.py
"""

import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPTS_DIR)
import resolve_release as rr  # noqa: E402

DUCKDB = shutil.which("duckdb")

CAST = "calcofi_ctd-cast"
DERIVED = "calcofi_ctd-derived"
CRUISE = "2025-04-3322"

# three stations on line 93.3; sample keys in ctd-cast's namespace, which is
# what the release's ctd-derived rows carry too (measured on v2026.10.01)
STATIONS = [(30.0, "st30-ln93.3"), (40.0, "st40-ln93.3"), (50.0, "st50-ln93.3")]


def skey(sta):
    return f"{CAST}:cast:2504_{int(sta):03d}d"


def site(sta):
    return f"093.3 {sta:05.1f}"


FIXTURE_SQL = r"""
INSTALL spatial; LOAD spatial;
CREATE TABLE grid AS
SELECT * FROM (VALUES
  ('st30-ln93.3', 30.0, 93.3, 'inshore',  ST_Point(-117.40, 32.85)),
  ('st40-ln93.3', 40.0, 93.3, 'inshore',  ST_Point(-117.55, 32.80)),
  ('st50-ln93.3', 50.0, 93.3, 'offshore', ST_Point(-117.70, 32.75)),
  -- v2026.10.04: SCCOOS inshore stations are one-cell "lines" of their own
  ('st26.4-ln93.4', 26.4, 93.4, 'inshore', ST_Point(-117.33, 32.90)),
  ('st30.1-ln88.5', 30.1, 88.5, 'inshore', ST_Point(-117.40, 33.30)))
  t(grid_key, station, line, shore, geom_ctr);

CREATE TABLE sample AS
SELECT 'calcofi_ctd-cast:cast:2504_' || lpad(CAST(CAST(station AS INT) AS VARCHAR), 3, '0') || 'd' AS sample_key,
       'calcofi_ctd-cast' AS dataset_key, 'cast' AS sample_type, '2025-04-3322' AS cruise_key,
       grid_key, '093.3 ' || lpad(printf('%.1f', station), 5, '0') AS site_key,
       32.8 AS latitude, -117.5 AS longitude,
       TIMESTAMP '2025-04-10 12:00:00' AS datetime, 'final' AS data_stage
FROM grid WHERE line = 93.3
UNION ALL SELECT * FROM (VALUES
  ('calcofi_ctd-cast:cast:2504_901d', 'calcofi_ctd-cast', 'cast', '2025-04-3322',
   'st26.4-ln93.4', '093.4 026.4', 32.9, -117.33, TIMESTAMP '2025-04-10 06:00:00', 'final'),
  ('calcofi_ctd-cast:cast:2504_902d', 'calcofi_ctd-cast', 'cast', '2025-04-3322',
   'st30.1-ln88.5', '088.5 030.1', 33.3, -117.40, TIMESTAMP '2025-04-10 03:00:00', 'final'));

-- (station, depth, type, value, qual)
CREATE TABLE obs_rows AS SELECT * FROM (VALUES
  -- ctd-cast inputs
  ('calcofi_ctd-cast', 30, 1.0,  'temperature_ave',   15.0, NULL),
  ('calcofi_ctd-cast', 30, 10.0, 'temperature_ave',   14.0, NULL),
  ('calcofi_ctd-cast', 40, 1.0,  'temperature_ave',   15.5, NULL),
  ('calcofi_ctd-cast', 40, 10.0, 'temperature_ave',   14.5, NULL),
  ('calcofi_ctd-cast', 50, 1.0,  'temperature_ave',   16.0, NULL),
  ('calcofi_ctd-cast', 50, 10.0, 'temperature_ave',   15.0, NULL),
  ('calcofi_ctd-cast', 30, 1.0,  'salinity_ave_corr', 33.5, NULL),
  ('calcofi_ctd-cast', 30, 10.0, 'salinity_ave_corr', 33.6, NULL),
  ('calcofi_ctd-cast', 40, 1.0,  'salinity_ave_corr', 33.4, NULL),
  ('calcofi_ctd-cast', 40, 10.0, 'salinity_ave_corr', 33.5, NULL),
  ('calcofi_ctd-cast', 50, 1.0,  'salinity_ave_corr', 33.3, NULL),
  ('calcofi_ctd-cast', 50, 10.0, 'salinity_ave_corr', 99.0, '9.0'),   -- flagged bad
  ('calcofi_ctd-cast', 30, 1.0,  'sigma_theta_1',     24.6, NULL),
  ('calcofi_ctd-cast', 30, 10.0, 'sigma_theta_1',     24.9, '8'),     -- sensor 1 only
  ('calcofi_ctd-cast', 40, 1.0,  'sigma_theta_1',     24.4, NULL),
  ('calcofi_ctd-cast', 40, 10.0, 'sigma_theta_1',     24.8, '8'),     -- both sensors
  ('calcofi_ctd-cast', 40, 10.0, 'sigma_theta_2',     24.8, '9'),
  ('calcofi_ctd-cast', 50, 1.0,  'sigma_theta_1',     24.2, NULL),
  ('calcofi_ctd-cast', 50, 10.0, 'sigma_theta_1',     24.5, NULL),
  -- ctd-derived, keyed by the ctd-cast sample_key, no measurement_qual
  ('calcofi_ctd-derived', 30, 1.0,  'sigma_theta_ave', 24.60, NULL),
  ('calcofi_ctd-derived', 30, 10.0, 'sigma_theta_ave', 24.70, NULL),
  ('calcofi_ctd-derived', 40, 1.0,  'sigma_theta_ave', 24.40, NULL),
  ('calcofi_ctd-derived', 40, 10.0, 'sigma_theta_ave', 24.80, NULL),  -- both inputs flagged
  ('calcofi_ctd-derived', 50, 1.0,  'sigma_theta_ave', 24.20, NULL),
  ('calcofi_ctd-derived', 50, 10.0, 'sigma_theta_ave', 24.50, NULL),
  ('calcofi_ctd-derived', 30, 1.0,  'spiciness0',      0.25, NULL),
  ('calcofi_ctd-derived', 30, 10.0, 'spiciness0',      0.20, NULL),
  ('calcofi_ctd-derived', 40, 1.0,  'spiciness0',     -0.10, NULL),
  ('calcofi_ctd-derived', 40, 10.0, 'spiciness0',     -0.15, NULL),
  ('calcofi_ctd-derived', 50, 1.0,  'spiciness0',     -0.30, NULL),
  ('calcofi_ctd-derived', 50, 10.0, 'spiciness0',      5.00, NULL),   -- salinity flagged
  -- 901 = SCCOOS 93.4 26.4 (drawn on line 93.3), 902 = 88.5 30.1 (on no line)
  ('calcofi_ctd-cast', 901, 1.0,  'temperature_ave',   15.8, NULL),
  ('calcofi_ctd-cast', 902, 1.0,  'temperature_ave',   16.2, NULL))
  t(dataset_key, sta, depth_min_m, measurement_type, measurement_value, measurement_qual);

CREATE TABLE obs AS
SELECT 'calcofi_ctd-cast:cast:2504_' || lpad(CAST(sta AS VARCHAR), 3, '0') || 'd' AS sample_key,
       '2025-04-3322' AS cruise_key, depth_min_m, depth_min_m AS depth_max_m,
       measurement_type, measurement_value, CAST(measurement_qual AS VARCHAR) AS measurement_qual,
       dataset_key
FROM obs_rows;

CREATE TABLE climatology AS SELECT * FROM (VALUES
  ('calcofi_ctd-derived', '093.3 030.0', 'st30-ln93.3', 4, 0, 'spiciness0',      0.05, 0.1, 40, 12),
  ('calcofi_ctd-cast',    '093.3 030.0', 'st30-ln93.3', 4, 0, 'spiciness0',    100.0,  0.1, 40, 12),  -- decoy
  ('calcofi_ctd-derived', '093.3 030.0', 'st30-ln93.3', 4, 0, 'sigma_theta_ave', 24.5, 0.2, 40, 12),
  ('calcofi_ctd-cast',    '093.3 030.0', 'st30-ln93.3', 4, 0, 'temperature_ave', 14.0, 0.5, 40, 12))
  t(dataset_key, site_key, grid_key, month, depth_bin, measurement_type,
    clim_mean, clim_sd, clim_n, n_cruises);
ALTER TABLE climatology ADD COLUMN clim_yr_min SMALLINT DEFAULT 1993;
ALTER TABLE climatology ADD COLUMN clim_yr_max SMALLINT DEFAULT 2013;

CREATE TABLE cruise AS SELECT '2025-04-3322' AS cruise_key, '33RL' AS ship_key;
CREATE TABLE ship AS SELECT '33RL' AS ship_key, 'Reuben Lasker' AS ship_name;
CREATE TABLE measurement_type AS SELECT * FROM (VALUES
  ('temperature_ave', 'Average temperature', 'degC', -2.0, 40.0),
  ('salinity_ave_corr', 'Average salinity corrected', 'PSU', 0.0, 45.0),
  ('sigma_theta_1', 'Potential density sensor 1', 'kg/m3', 15.0, 35.0),
  ('sigma_theta_ave', 'Potential density anomaly (sigma-theta), sensor-pair average', 'kg/m3', 15.0, 35.0),
  ('spiciness0', 'Spice (spiciness at 0 dbar, TEOS-10)', 'kg/m3', -25.0, 18.0))
  t(measurement_type, description, units, valid_min, valid_max);

COPY grid             TO 'fx/ducklake/tables/grid/h/grid.parquet' (FORMAT PARQUET);
COPY sample           TO 'fx/ducklake/tables/sample/h/sample.parquet' (FORMAT PARQUET);
COPY cruise           TO 'fx/ducklake/tables/cruise/h/cruise.parquet' (FORMAT PARQUET);
COPY ship             TO 'fx/ducklake/tables/ship/h/ship.parquet' (FORMAT PARQUET);
COPY measurement_type TO 'fx/ducklake/tables/measurement_type/h/measurement_type.parquet' (FORMAT PARQUET);
COPY obs              TO 'fx/ducklake/tables/obs' (FORMAT PARQUET, PARTITION_BY (dataset_key));
COPY climatology      TO 'fx/ducklake/tables/climatology' (FORMAT PARQUET, PARTITION_BY (measurement_type));
"""


def fixture_catalog(root):
    """The release catalog shape (objects[] per table) over the files in root/fx."""
    tables = []
    base = os.path.join(root, "fx")
    for name in ["grid", "sample", "cruise", "ship", "measurement_type", "obs", "climatology"]:
        files = sorted(glob.glob(os.path.join(base, "ducklake", "tables", name, "**", "*.parquet"),
                               recursive=True))
        objs = []
        for f in files:
            rel = os.path.relpath(f, base)
            o = {"path": rel}
            part = [seg for seg in rel.split(os.sep) if "=" in seg]
            if part:
                k, v = part[0].split("=", 1)
                o.update(partition_by=k, partition_value=v)
            objs.append(o)
        tables.append({"name": name, "partitioned": name in ("obs", "climatology"), "objects": objs})
    return {"version": "v0000.00.00", "tables": tables}


def duck(cwd, sql):
    r = subprocess.run([DUCKDB, "-json", "-c", sql], cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0:
        raise AssertionError(f"duckdb failed:\n{r.stderr}\n{r.stdout}")
    return r.stdout


def rows(cwd, sql):
    out = duck(cwd, sql).strip()
    return json.loads(out) if out else []


@unittest.skipUnless(DUCKDB, "duckdb CLI not on PATH")
class BuildSectionsSQL(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        d = cls.d = cls.tmp.name
        for t in ["grid", "sample", "cruise", "ship", "measurement_type"]:
            os.makedirs(os.path.join(d, "fx", "ducklake", "tables", t, "h"))
        os.makedirs(os.path.join(d, "public", "data"))
        duck(d, FIXTURE_SQL)
        with open(os.path.join(SCRIPTS_DIR, "build_sections.sql")) as f:
            sql = rr.render(f.read(), fixture_catalog(d))
        # the rendered SQL names https objects in the bucket; point it at the fixture
        sql = sql.replace(rr.BUCKET_HTTPS + "/", os.path.join(d, "fx") + "/")
        with open(os.path.join(d, "build_sections.sql"), "w") as f:
            f.write(sql)
        cls.summary = rows(d, ".read build_sections.sql")[-1]

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def section(self, where="TRUE"):
        return rows(self.d, "SELECT dataset_key, var, sta, depth_m, value "
                            f"FROM 'public/data/_sections.parquet' WHERE {where} "
                            "ORDER BY var, sta, depth_m")

    def test_variables_drawn(self):
        got = {r["var"] for r in self.section()}
        self.assertEqual(got, {"temperature_ave", "salinity_ave_corr",
                               "sigma_theta_ave", "spiciness0"})

    def test_sigma_theta_1_is_never_drawn(self):
        self.assertEqual(self.section("var IN ('sigma_theta_1', 'sigma_theta_2')"), [])

    def test_derived_rows_join_the_ctd_cast_cast(self):
        r = self.section("var = 'spiciness0' AND sta = 30 AND depth_m = 0")
        self.assertEqual(r, [{"dataset_key": DERIVED, "var": "spiciness0", "sta": 30.0,
                              "depth_m": 0, "value": 0.25}])

    def test_flagged_provider_value_is_not_drawn(self):
        self.assertEqual(self.section("var = 'salinity_ave_corr' AND sta = 50 AND depth_m = 10"), [])
        self.assertEqual(len(self.section("var = 'salinity_ave_corr'")), 5)

    def test_spice_on_a_flagged_input_is_not_drawn(self):
        self.assertEqual(self.section("var = 'spiciness0' AND sta = 50 AND depth_m = 10"), [])
        self.assertEqual(len(self.section("var = 'spiciness0'")), 5)

    def test_sigma_theta_ave_needs_both_sensors_flagged_to_drop(self):
        # station 40, 10 m: both sensors flagged -> no average
        self.assertEqual(self.section("var = 'sigma_theta_ave' AND sta = 40 AND depth_m = 10"), [])
        # station 30, 10 m: sensor 1 flagged, sensor 2 good -> the average stands
        self.assertEqual(len(self.section("var = 'sigma_theta_ave' AND sta = 30 AND depth_m = 10")), 1)
        self.assertEqual(len(self.section("var = 'sigma_theta_ave'")), 5)

    def test_summary_counts_the_guard(self):
        self.assertEqual(self.summary["n_derived_blocked"], 2)

    def test_anomaly_uses_the_values_own_dataset_climatology(self):
        a = rows(self.d, "SELECT var, sta, depth_m, anomaly FROM 'public/data/_anomaly.parquet' "
                         "ORDER BY var")
        self.assertEqual(a, [
            {"var": "sigma_theta_ave", "sta": 30.0, "depth_m": 0, "anomaly": 0.1},
            {"var": "spiciness0",      "sta": 30.0, "depth_m": 0, "anomaly": 0.2},
            {"var": "temperature_ave", "sta": 30.0, "depth_m": 0, "anomaly": 1.0},
        ])

    def test_section_line_is_the_stations_own_line_not_the_cell(self):
        # v2026.10.04: 93.4 26.4 is a one-cell line of its own; keyed on the cell it
        # would leave line 93.3, keyed on its site_key it is 0.1 off 93.3 and joins it
        r = rows(self.d, "SELECT line::DOUBLE AS line, sta, grid_key FROM 'public/data/_stations.parquet' "
                         "ORDER BY sta")
        self.assertEqual([(x["line"], x["sta"]) for x in r],
                         [(93.3, 26.4), (93.3, 30.0), (93.3, 40.0), (93.3, 50.0)])
        self.assertEqual(r[0]["grid_key"], "st26.4-ln93.4")
        self.assertEqual(self.section("var = 'temperature_ave' AND sta = 26.4"),
                         [{"dataset_key": CAST, "var": "temperature_ave", "sta": 26.4,
                           "depth_m": 0, "value": 15.8}])

    def test_station_off_every_line_is_not_drawn(self):
        # 88.5 30.1 is 1.5 line units from line 90: on no section, not a one-cell
        # line 88.5 section either
        self.assertEqual(self.section("sta = 30.1"), [])
        self.assertEqual({r["line"] for r in rows(
            self.d, "SELECT DISTINCT line::DOUBLE AS line FROM 'public/data/_sections.parquet'")}, {93.3})

    def test_variable_labels_cover_the_derived_types(self):
        v = {r["var"] for r in rows(self.d, "SELECT var FROM 'public/data/_variables.parquet'")}
        self.assertEqual(v, {"temperature_ave", "salinity_ave_corr", "sigma_theta_ave", "spiciness0"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
