#!/usr/bin/env python3
"""Self-test for scripts/resolve_release.py — exact URLs against both catalog shapes.

The two fixtures are copies of calcofi4r/tests/testthat/fixtures/catalog_canonical.json
and catalog_legacy.json (the R and Python packages assert the same URLs against
the same files), embedded here so this runs in CI with nothing checked out but
this repo.

    python3 scripts/test_resolve_release.py
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import resolve_release as rr  # noqa: E402

CANONICAL = {'version': 'v2026.09.01',
 'release_date': '2026-09-01',
 'layout': 'canonical',
 'writer': {'compression': 'zstd', 'row_group_size': 122880, 'parquet_version': 'V1'},
 'tables': [{'name': 'cruise',
             'rows': 700,
             'partitioned': False,
             'supplemental': False,
             'content_hash': 'a1b2c3d4e5f60718293a4b5c6d7e8f90',
             'compat_path': 'ducklake/releases/v2026.09.01/parquet/cruise.parquet',
             'objects': [{'path': 'ducklake/tables/cruise/a1b2c3d4e5f60718293a4b5c/cruise.parquet',
                          'bytes': 40960,
                          'sha256': '00ff',
                          'content_hash': 'a1b2c3d4e5f60718293a4b5c6d7e8f90',
                          'since': 'v2026.08.25',
                          'compat_path': 'ducklake/releases/v2026.09.01/parquet/cruise.parquet'}]},
            {'name': 'obs',
             'rows': 3000000,
             'partitioned': True,
             'supplemental': False,
             'content_hash': '0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f',
             'compat_path': 'ducklake/releases/v2026.09.01/parquet/obs/',
             'objects': [{'path': 'ducklake/tables/obs/year=2019/1111111111111111111111aa/data_0.parquet',
                          'bytes': 1000,
                          'sha256': '01',
                          'content_hash': '1111111111111111111111aa11111111',
                          'since': 'v2026.07.17',
                          'partition_by': 'year',
                          'partition_value': '2019',
                          'compat_path': 'ducklake/releases/v2026.09.01/parquet/obs/year=2019/data_0.parquet'},
                         {'path': 'ducklake/tables/obs/year=2020/2222222222222222222222bb/data_0.parquet',
                          'bytes': 1000,
                          'sha256': '02',
                          'content_hash': '2222222222222222222222bb22222222',
                          'since': 'v2026.09.01',
                          'partition_by': 'year',
                          'partition_value': '2020',
                          'compat_path': 'ducklake/releases/v2026.09.01/parquet/obs/year=2020/data_0.parquet'},
                         {'path': 'ducklake/tables/obs/9999999999999999999999ff/obs.parquet',
                          'bytes': 1900,
                          'sha256': '09',
                          'content_hash': '9999999999999999999999ff99999999',
                          'since': 'v2026.09.01',
                          'compat_path': 'ducklake/releases/v2026.09.01/parquet/obs.parquet'}]},
            {'name': 'obs_ctd_full',
             'rows': 216000000,
             'partitioned': True,
             'supplemental': True,
             'content_hash': 'abababababababababababababababab',
             'compat_path': 'ducklake/releases/v2026.09.01/parquet/obs_ctd_full/',
             'objects': [{'path': 'ducklake/tables/obs_ctd_full/year=2019/3333333333333333333333cc/data_0.parquet',
                          'bytes': 1000,
                          'sha256': '03',
                          'content_hash': '3333333333333333333333cc33333333',
                          'since': 'v2026.07.17',
                          'partition_by': 'year',
                          'partition_value': '2019',
                          'compat_path': 'ducklake/releases/v2026.09.01/parquet/obs_ctd_full/year=2019/data_0.parquet'}]}]}

LEGACY = {'version': 'v2026.08.14',
 'release_date': '2026-08-14',
 'tables': [{'name': 'cruise', 'rows': 700, 'partitioned': False, 'supplemental': False},
            {'name': 'obs', 'rows': 3000000, 'partitioned': True, 'supplemental': False},
            {'name': 'obs_ctd_full', 'rows': 216000000, 'partitioned': True, 'supplemental': True}]}

B = "https://storage.googleapis.com/calcofi-db"
CRUISE_CANON = f"{B}/ducklake/tables/cruise/a1b2c3d4e5f60718293a4b5c/cruise.parquet"
OBS_2019 = f"{B}/ducklake/tables/obs/year=2019/1111111111111111111111aa/data_0.parquet"
OBS_2020 = f"{B}/ducklake/tables/obs/year=2020/2222222222222222222222bb/data_0.parquet"
OBS_TWIN = f"{B}/ducklake/tables/obs/9999999999999999999999ff/obs.parquet"
CRUISE_LEGACY = f"{B}/ducklake/releases/v2026.08.14/parquet/cruise.parquet"
OBS_LEGACY = "gs://calcofi-db/ducklake/releases/v2026.08.14/parquet/obs/**/*.parquet"
OBS_LEGACY_TWIN = f"{B}/ducklake/releases/v2026.08.14/parquet/obs.parquet"


class Canonical(unittest.TestCase):
    def test_single_object(self):
        src = rr.release_sources(CANONICAL, "cruise")
        self.assertEqual(src, {"urls": [CRUISE_CANON], "hive": False, "canonical": True,
                               "partition_by": None, "single_file": None})
        self.assertEqual(rr.read_parquet_sql(src), f"read_parquet('{CRUISE_CANON}')")

    def test_partitioned_is_an_explicit_hive_list(self):
        src = rr.release_sources(CANONICAL, "obs")
        # the whole-table twin is exposed separately, never mixed into the partitions
        self.assertEqual(src["urls"], [OBS_2019, OBS_2020])
        self.assertEqual(src["single_file"], OBS_TWIN)
        self.assertTrue(src["hive"] and src["canonical"])
        self.assertEqual(src["partition_by"], "year")
        self.assertEqual(rr.read_parquet_sql(src),
                         f"read_parquet(['{OBS_2019}', '{OBS_2020}'], hive_partitioning = true)")

    def test_partitioned_without_twin(self):
        src = rr.release_sources(CANONICAL, "obs_ctd_full")
        self.assertEqual(len(src["urls"]), 1)
        self.assertIsNone(src["single_file"])

    def test_single_partition_is_the_matching_subset(self):
        src = rr.release_sources(CANONICAL, "obs", ("year", "2020"))
        self.assertEqual(src["urls"], [OBS_2020])
        self.assertEqual(rr.read_parquet_sql(src),
                         f"read_parquet('{OBS_2020}', hive_partitioning = true)")

    def test_missing_partition_fails_loudly(self):
        with self.assertRaises(SystemExit):
            rr.release_sources(CANONICAL, "obs", ("year", "1999"))


class Legacy(unittest.TestCase):
    def test_single_table_is_the_per_release_path(self):
        src = rr.release_sources(LEGACY, "cruise")
        self.assertEqual(src, {"urls": [CRUISE_LEGACY], "hive": False, "canonical": False,
                               "partition_by": None, "single_file": None})
        self.assertEqual(rr.read_parquet_sql(src), f"read_parquet('{CRUISE_LEGACY}')")

    def test_partitioned_is_a_gs_glob(self):
        src = rr.release_sources(LEGACY, "obs")
        self.assertEqual(src["urls"], [OBS_LEGACY])
        self.assertTrue(src["hive"])
        self.assertFalse(src["canonical"])
        # obs is the one legacy partitioned table with a whole-table twin
        self.assertEqual(src["single_file"], OBS_LEGACY_TWIN)
        self.assertIsNone(rr.release_sources(LEGACY, "obs_ctd_full")["single_file"])
        self.assertEqual(rr.read_parquet_sql(src),
                         f"read_parquet('{OBS_LEGACY}', hive_partitioning = true)")

    def test_single_partition_glob(self):
        src = rr.release_sources(LEGACY, "obs", ("dataset_key", "cce-lter_zoodb"))
        self.assertEqual(src["urls"], ["gs://calcofi-db/ducklake/releases/v2026.08.14/parquet/"
                                       "obs/dataset_key=cce-lter_zoodb/*.parquet"])
        self.assertTrue(src["hive"])

    def test_unknown_table_fails(self):
        with self.assertRaises(SystemExit):
            rr.release_sources(LEGACY, "nope")

    def test_partition_of_unpartitioned_fails(self):
        with self.assertRaises(SystemExit):
            rr.release_sources(LEGACY, "cruise", ("year", "2020"))


class Render(unittest.TestCase):
    SQL = ("SELECT '__RELEASE__' AS release;\n"
           "SELECT * FROM __TBL:cruise__ c JOIN __TBL:obs__ o USING (cruise_key)\n"
           "UNION ALL SELECT * FROM __TBL:obs:year=2019__;\n")

    def test_tokens_become_expressions(self):
        out = rr.render(self.SQL, CANONICAL)
        self.assertIn("SELECT 'v2026.09.01' AS release;", out)
        self.assertIn(f"FROM read_parquet('{CRUISE_CANON}') c", out)
        self.assertIn(f"JOIN read_parquet(['{OBS_2019}', '{OBS_2020}'], hive_partitioning = true) o", out)
        self.assertIn(f"FROM read_parquet('{OBS_2019}', hive_partitioning = true);", out)
        self.assertNotIn("__", out)

    def test_legacy_render(self):
        out = rr.render("FROM __TBL:obs__ o WHERE 1", LEGACY)
        self.assertEqual(out, f"FROM read_parquet('{OBS_LEGACY}', hive_partitioning = true) o WHERE 1")

    def test_comments_are_left_verbatim(self):
        sql = ("-- every __TBL:<table>__ token, e.g. __TBL:obs:dataset_key=<key>__, and __RELEASE__\n"
               "SELECT 1 FROM __TBL:cruise__; -- __TBL:not_a_table__ __RELEASE__\n")
        out = rr.render(sql, CANONICAL)
        self.assertEqual(out.split("\n")[0], sql.split("\n")[0])
        self.assertEqual(out.split("\n")[1],
                         f"SELECT 1 FROM read_parquet('{CRUISE_CANON}'); -- __TBL:not_a_table__ __RELEASE__")

    def test_leftover_token_is_an_error(self):
        with self.assertRaises(SystemExit):
            rr.render("SELECT * FROM __TBL:obs:year_2019__", CANONICAL)  # missing '='
        with self.assertRaises(SystemExit):
            rr.render("SELECT * FROM __TBL:no_such_table__", CANONICAL)


class Cli(unittest.TestCase):
    def run_cli(self, catalog, extra):
        with tempfile.TemporaryDirectory() as d:
            cat = os.path.join(d, "catalog.json")
            with open(cat, "w") as f:
                json.dump(catalog, f)
            sql = os.path.join(d, "build_x.sql")
            with open(sql, "w") as f:
                f.write("SELECT '__RELEASE__' AS v FROM __TBL:cruise__;\n")
            out = os.path.join(d, "build")
            tj = os.path.join(d, "public", "data", "tables.json")
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = rr.main(["--catalog", cat, "--out-dir", out, "--tables-json", tj,
                              "--tables", "obs,cruise", sql] + extra)
            self.assertEqual(rc, 0, err.getvalue())
            rendered = open(os.path.join(out, "build_x.sql")).read()
            tables = json.load(open(tj))
            self.assertTrue(os.path.exists(os.path.join(out, "catalog.json")))
            return rendered, tables

    def test_canonical_end_to_end(self):
        rendered, tables = self.run_cli(CANONICAL, [])
        self.assertEqual(rendered, f"SELECT 'v2026.09.01' AS v FROM read_parquet('{CRUISE_CANON}');\n")
        self.assertEqual(tables["version"], "v2026.09.01")
        self.assertEqual(tables["layout"], "canonical")
        self.assertEqual(tables["tables"]["obs"]["urls"], [OBS_2019, OBS_2020])
        self.assertEqual(tables["tables"]["obs"]["partition_by"], "year")
        self.assertEqual(tables["tables"]["obs"]["single_file"], OBS_TWIN)
        self.assertEqual(tables["tables"]["cruise"]["urls"], [CRUISE_CANON])

    def test_legacy_end_to_end(self):
        rendered, tables = self.run_cli(LEGACY, [])
        self.assertEqual(rendered, f"SELECT 'v2026.08.14' AS v FROM read_parquet('{CRUISE_LEGACY}');\n")
        self.assertEqual(tables["layout"], "legacy")
        self.assertFalse(tables["tables"]["obs"]["canonical"])
        self.assertEqual(tables["tables"]["obs"]["urls"], [OBS_LEGACY])
        self.assertEqual(tables["tables"]["obs"]["single_file"], OBS_LEGACY_TWIN)

    def test_expr(self):
        with tempfile.TemporaryDirectory() as d:
            cat = os.path.join(d, "catalog.json")
            with open(cat, "w") as f:
                json.dump(CANONICAL, f)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = rr.main(["--catalog", cat, "--expr", "obs:year=2020"])
            self.assertEqual(rc, 0)
            self.assertEqual(out.getvalue().strip(),
                             f"read_parquet('{OBS_2020}', hive_partitioning = true)")


if __name__ == "__main__":
    unittest.main(verbosity=1)
