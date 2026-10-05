#!/usr/bin/env python3
"""Pin build_sections.station_ruler(): a section station's place on its line's ruler.

A station's `line_dist_km` comes from its own station NUMBER, interpolated between
the line's grid stations (metadata/station_bathymetry.csv), never from the grid
cell it falls in. The cell's line can differ from the section's (v2026.10.04: the
SCCOOS station 93.4 26.4 is a one-cell line of its own but is drawn on line 93.3),
and before that one cell could hold several stations (90.28 and 90.30 shared x).

    python3 scripts/test_station_ruler.py
"""

import os
import sys
import unittest

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_sections import interp_linear, station_ruler  # noqa: E402

# line 93.3's inshore grid stations, 7.386 km per station unit (+proj=calcofi)
LINE = pd.DataFrame({"sta": [26.7, 28.0, 30.0, 35.0],
                     "line_dist_km": [0.0, 9.602, 24.374, 61.304]})


class StationRuler(unittest.TestCase):
    def test_grid_station_keeps_its_own_ruler(self):
        self.assertEqual(station_ruler(LINE, 30.0), 24.374)

    def test_between_grid_stations_interpolates_by_station_number(self):
        # 29.0 is half way from 28 to 30
        self.assertEqual(station_ruler(LINE, 29.0), round((9.602 + 24.374) / 2, 3))

    def test_inshore_of_the_first_grid_station_extends_the_end_segment(self):
        # SCCOOS 93.4 26.4 drawn on line 93.3: 0.3 station units inshore of 26.7
        got = station_ruler(LINE, 26.4)
        self.assertAlmostEqual(got, -0.3 * 9.602 / 1.3, places=3)
        self.assertLess(got, 0)

    def test_unsorted_and_missing_rows_are_tolerated(self):
        g = pd.concat([LINE.iloc[::-1], pd.DataFrame({"sta": [40.0], "line_dist_km": [None]})])
        self.assertEqual(station_ruler(g, 30.0), 24.374)

    def test_a_line_with_one_grid_station_has_no_ruler(self):
        self.assertIsNone(station_ruler(LINE.iloc[:1], 26.7))


class InterpLinear(unittest.TestCase):
    def test_outside_without_extrapolation_is_none(self):
        self.assertIsNone(interp_linear([0, 1], [10, 20], 1.5))
        self.assertIsNone(interp_linear([0, 1], [10, 20], -0.1))

    def test_inside(self):
        self.assertEqual(interp_linear([0, 1, 3], [10, 20, 0], 2), 10)

    def test_repeated_knot_does_not_divide_by_zero(self):
        self.assertEqual(interp_linear([0, 0, 1], [5, 7, 9], 0), 5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
