#!/usr/bin/env python3
"""Pin build_sections.py's reshaping of the anomaly and per-cast intermediates.

  * a NULL anomaly (what a NULL monthly clim_mean becomes) leaves its cell AND its
    baseline count null, never 0 and never a count without an anomaly beside it;
    a variable with no cell at all is left out, so its key means "has an anomaly";
  * the seasonal matrices are built the same way from their own columns;
  * the per-cast lines carry one entry per station, normals null where there is none,
    and a baseline count only beside a normal.

    python3 scripts/test_build_sections_reshape.py
"""

import os
import sys
import unittest

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import display_rules as dr  # noqa: E402
from build_sections import (CAST_LINES, DEPTHS, anomaly_matrices,  # noqa: E402
                            cast_lines)

NA = float("nan")
STA_IX = {30.0: 0, 40.0: 1, 50.0: 2}
DEPTH_IX = {d: i for i, d in enumerate(DEPTHS)}


def anomaly_frame():
    # station 40 at 0 m: the quarter passes, the month does not (clim_mean NULL)
    return pd.DataFrame({
        "var":              ["temperature_ave"] * 3 + ["spiciness0"],
        "sta":              [30.0, 40.0, 50.0, 30.0],
        "depth_m":          [0, 0, 10, 0],
        "anomaly":          [1.0, NA, 0.5, NA],
        "n_cruises":        [12, NA, 7, NA],
        "anomaly_season":   [1.5, 1.0, NA, NA],
        "season_n_cruises": [30, 9, NA, NA],
    })


class AnomalyMatrices(unittest.TestCase):
    def test_null_anomaly_is_a_null_cell_and_a_null_count(self):
        z, n = anomaly_matrices(anomaly_frame(), STA_IX, DEPTH_IX, 3, "anomaly", "n_cruises")
        t = z["temperature_ave"]
        self.assertEqual(t[0], [1.0, None, None])
        self.assertEqual(t[1], [None, None, 0.5])
        self.assertEqual(n["temperature_ave"][0], [12, None, None])
        self.assertEqual(n["temperature_ave"][1], [None, None, 7])

    def test_variable_with_no_cell_is_left_out(self):
        z, n = anomaly_matrices(anomaly_frame(), STA_IX, DEPTH_IX, 3, "anomaly", "n_cruises")
        self.assertEqual(set(z), {"temperature_ave"})
        self.assertEqual(set(n), {"temperature_ave"})

    def test_seasonal_matrices_read_their_own_columns(self):
        z, n = anomaly_matrices(anomaly_frame(), STA_IX, DEPTH_IX, 3,
                                "anomaly_season", "season_n_cruises")
        self.assertEqual(z["temperature_ave"][0], [1.5, 1.0, None])
        self.assertEqual(n["temperature_ave"][0], [30, 9, None])
        self.assertEqual(z["temperature_ave"][1], [None, None, None])

    def test_no_rows_no_matrices(self):
        self.assertEqual(anomaly_matrices(None, STA_IX, DEPTH_IX, 3, "anomaly", "n_cruises"),
                         ({}, {}))

    def test_matrix_shape_is_the_shards(self):
        z, _ = anomaly_matrices(anomaly_frame(), STA_IX, DEPTH_IX, 3, "anomaly", "n_cruises")
        self.assertEqual(len(z["temperature_ave"]), len(DEPTHS))
        self.assertTrue(all(len(r) == 3 for r in z["temperature_ave"]))


class CastLines(unittest.TestCase):
    def frame(self):
        return pd.DataFrame({
            "var":              ["mld_sigma_theta_002"] * 3 + ["chl_max_depth"],
            "sta":              [30.0, 40.0, 50.0, 30.0],
            "value":            [25.0, 30.0, 35.0, 40.0],
            "clim_mean":        [20.0, NA, NA, NA],
            "n_cruises":        [10, NA, NA, NA],
            "season_mean":      [22.0, NA, 33.0, NA],
            "season_n_cruises": [15, NA, 6, NA],
        })

    def test_one_entry_per_station_with_normals(self):
        got = cast_lines(self.frame(), STA_IX, 3)
        self.assertEqual(got["mld_sigma_theta_002"], {
            "value":    [25.0, 30.0, 35.0],
            "month":    [20.0, None, None],
            "season":   [22.0, None, 33.0],
            "n_month":  [10, None, None],
            "n_season": [15, None, 6],
        })
        self.assertEqual(got["chl_max_depth"]["value"], [40.0, None, None])
        self.assertEqual(got["chl_max_depth"]["month"], [None, None, None])

    def test_count_only_beside_a_normal(self):
        f = self.frame()
        f.loc[1, "n_cruises"] = 4          # a count with no mean is not a baseline
        got = cast_lines(f, STA_IX, 3)
        self.assertIsNone(got["mld_sigma_theta_002"]["n_month"][1])

    def test_station_off_the_section_is_ignored(self):
        f = pd.concat([self.frame(), pd.DataFrame({
            "var": ["mld_sigma_theta_002"], "sta": [99.0], "value": [1.0],
            "clim_mean": [NA], "n_cruises": [NA], "season_mean": [NA],
            "season_n_cruises": [NA]})], ignore_index=True)
        self.assertEqual(cast_lines(f, STA_IX, 3)["mld_sigma_theta_002"]["value"],
                         [25.0, 30.0, 35.0])

    def test_no_rows_no_lines(self):
        self.assertEqual(cast_lines(None, STA_IX, 3), {})

    def test_drawn_types_are_the_stage_rules(self):
        # the app's legend order and the stage rule name the same two types
        self.assertEqual({v for v, _ in CAST_LINES}, dr.CAST_LINE_VARS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
