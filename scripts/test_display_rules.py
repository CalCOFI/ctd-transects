#!/usr/bin/env python3
"""Self-test for scripts/display_rules.py — one small fixture per rule.

The fixtures are the shapes in Rasmus Swalethorp's 2026-09-29 screenshots of line
93.3, cruise 2025-04-3322 (15 stations, 10 m bins): oxygen with data at 3 stations,
nitrate with one value per cast, and a healthy profile that must survive.

    python3 scripts/test_display_rules.py
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import display_rules as dr  # noqa: E402

N_DEPTH = 51   # 0-500 m in 10 m bins, as the shards


def grid(n_sta, fill):
    """z[depth][station] from fill(station_index, depth_index) -> value | None."""
    return [[fill(j, i) for j in range(n_sta)] for i in range(N_DEPTH)]


def profile(j, i):                      # varies with depth and station
    return 10.0 - 0.01 * i + 0.1 * j


class TestConstants(unittest.TestCase):
    def test_named_thresholds(self):
        self.assertEqual(dr.MIN_STATIONS, 3)
        self.assertEqual(dr.MIN_INVARIANT_BINS, 6)


class TestSparse(unittest.TestCase):
    def test_named_fraction(self):
        self.assertEqual(dr.MIN_STATION_FRACTION, 0.5)

    def test_min_stations_boundary_in_a_three_station_section(self):
        # rule 1a: "fewer than 3" -> 3 of 3 stays; 2 of 3 goes (also under 50%? no,
        # 2 of 3 is > half, so this is the absolute floor doing the work)
        three = grid(3, profile)
        two = grid(3, lambda j, i: profile(j, i) if j < 2 else None)
        self.assertIsNone(dr.withhold_reason(three))
        self.assertEqual(dr.withhold_reason(two), "data at 2 of 3 stations")

    def test_fraction_boundary_is_fewer_than_half(self):
        # rule 1b: 7 of 15 is under half and goes; 8 of 15 is over half and stays;
        # exactly half (3 of 6) stays
        seven = grid(15, lambda j, i: profile(j, i) if j < 7 else None)
        eight = grid(15, lambda j, i: profile(j, i) if j < 8 else None)
        half = grid(6, lambda j, i: profile(j, i) if j < 3 else None)
        self.assertEqual(dr.withhold_reason(seven), "data at 7 of 15 stations")
        self.assertIsNone(dr.withhold_reason(eight))
        self.assertIsNone(dr.withhold_reason(half))

    def test_oxygen_3_of_15_regression_2025_04_3322(self):
        # Rasmus 2026-09-29: oxygen_ml_l_ave_sta_corr on line 93.3, 2025-04-3322 has
        # data at 3 of 15 stations (6, 7, 8) and was filled across as one block
        z = grid(15, lambda j, i: profile(j, i) if j in (6, 7, 8) else None)
        self.assertEqual(dr.n_stations_with_data(z), 3)
        self.assertEqual(dr.withhold_reason(z), "data at 3 of 15 stations")

    def test_full_short_sections_are_not_over_withheld(self):
        # 5 of 5 and 4 of 4 are full coverage of a short section: kept
        self.assertIsNone(dr.withhold_reason(grid(5, profile)))
        self.assertIsNone(dr.withhold_reason(grid(4, profile)))
        self.assertIsNone(dr.withhold_reason(grid(3, profile)))

    def test_one_station_of_many_is_withheld(self):
        z = grid(15, lambda j, i: profile(j, i) if j == 7 else None)
        self.assertEqual(dr.withhold_reason(z), "data at 1 of 15 stations")

    def test_all_null_is_sparse(self):
        self.assertEqual(dr.withhold_reason(grid(15, lambda j, i: None)),
                         "data at 0 of 15 stations")

    def test_nan_counts_as_no_data(self):
        z = grid(5, lambda j, i: float("nan") if j > 1 else profile(j, i))
        self.assertEqual(dr.n_stations_with_data(z), 2)

    def test_a_single_value_counts_as_a_station(self):
        z = grid(5, lambda j, i: 1.0 if (i == 0 and j < 3) else None)
        self.assertEqual(dr.n_stations_with_data(z), 3)


class TestConstantWithDepth(unittest.TestCase):
    def test_one_value_per_cast_is_withheld(self):
        # EstNO3_CruiseCorr: a different scalar per cast, identical down the column
        z = grid(15, lambda j, i: 1.0 + 0.2 * j)
        self.assertEqual(dr.withhold_reason(z),
                         "constant with depth at every station (source column suspect)")

    def test_varies_with_depth_is_kept(self):
        self.assertIsNone(dr.withhold_reason(grid(15, profile)))

    def test_one_station_that_varies_saves_the_variable(self):
        z = grid(15, lambda j, i: (profile(j, i) if j == 4 else 2.0))
        self.assertFalse(dr.is_depth_invariant(z))
        self.assertIsNone(dr.withhold_reason(z))

    def test_tolerance_is_1e_9(self):
        flat = grid(5, lambda j, i: 3.0 + (1e-12 if i % 2 else 0.0))
        moving = grid(5, lambda j, i: 3.0 + (1e-6 if i % 2 else 0.0))
        self.assertTrue(dr.is_depth_invariant(flat))
        self.assertFalse(dr.is_depth_invariant(moving))

    def test_five_bins_is_too_short_to_judge_six_is_not(self):
        five = grid(5, lambda j, i: 2.0 if i < 5 else None)
        six = grid(5, lambda j, i: 2.0 if i < 6 else None)
        self.assertFalse(dr.is_depth_invariant(five))    # 40 m: no verdict
        self.assertTrue(dr.is_depth_invariant(six))      # 50 m: constant
        self.assertIsNone(dr.withhold_reason(five))
        self.assertEqual(dr.withhold_reason(six), dr.WITHHELD_CONSTANT_REASON)

    def test_short_stations_do_not_save_or_condemn(self):
        # 2 shallow casts (3 bins, varying) + 5 full constant casts: judged on the
        # full ones only -> withheld; the shallow ones do not rescue it
        z = grid(7, lambda j, i: (float(i) if (j < 2 and i < 3) else
                                  None if j < 2 else 4.0 + j))
        self.assertEqual(dr.withhold_reason(z), dr.WITHHELD_CONSTANT_REASON)

    def test_gaps_inside_a_constant_profile_still_constant(self):
        z = grid(4, lambda j, i: None if i % 7 == 3 else 1.5)
        self.assertTrue(dr.is_depth_invariant(z))

    def test_sparse_reason_wins_over_constant(self):
        z = grid(15, lambda j, i: 2.0 if j == 0 else None)
        self.assertEqual(dr.withhold_reason(z), "data at 1 of 15 stations")


class TestApplyVariableRules(unittest.TestCase):
    def setUp(self):
        self.grids = {
            "temperature_ave": grid(15, profile),
            "oxygen_ml_l_ave_sta_corr": grid(15, lambda j, i: profile(j, i) if j in (6, 7) else None),
            "est_nitrate_cruise_corr": grid(15, lambda j, i: 1.0 + 0.2 * j),
        }
        self.anoms = {v: z for v, z in self.grids.items()}
        self.anoms_n = {v: z for v, z in self.grids.items()}

    def test_drops_from_every_matrix_and_records_why(self):
        g, a, n, w = dr.apply_variable_rules(self.grids, self.anoms, self.anoms_n)
        self.assertEqual(list(g), ["temperature_ave"])
        self.assertEqual(list(a), ["temperature_ave"])
        self.assertEqual(list(n), ["temperature_ave"])
        self.assertEqual(w, {
            "oxygen_ml_l_ave_sta_corr": "data at 2 of 15 stations",
            "est_nitrate_cruise_corr": "constant with depth at every station (source column suspect)",
        })

    def test_anomaly_dict_with_extra_or_missing_keys(self):
        g, a, w = dr.apply_variable_rules(self.grids, {"est_nitrate_cruise_corr": [[1]]})
        self.assertEqual(a, {})
        self.assertIn("est_nitrate_cruise_corr", w)

    def test_nothing_withheld_is_an_empty_dict_not_none(self):
        g, w = dr.apply_variable_rules({"temperature_ave": grid(15, profile)})
        self.assertEqual(w, {})
        self.assertEqual(list(g), ["temperature_ave"])

    def test_inputs_are_not_mutated(self):
        before = set(self.grids)
        dr.apply_variable_rules(self.grids, self.anoms)
        self.assertEqual(set(self.grids), before)


class TestSections(unittest.TestCase):
    def test_fewer_than_three_stations_is_not_drawable(self):
        g = {"temperature_ave": grid(2, profile)}
        self.assertFalse(dr.section_is_drawable(2, g))
        self.assertTrue(dr.section_is_drawable(3, {"temperature_ave": grid(3, profile)}))

    def test_no_variable_left_is_not_drawable(self):
        self.assertFalse(dr.section_is_drawable(15, {}))


class TestFallback(unittest.TestCase):
    def test_sibling_var(self):
        self.assertEqual(dr.sibling_var("oxygen_ml_l_ave_sta_corr"),
                         "oxygen_ml_l_ave_cruise_corr")
        self.assertEqual(dr.sibling_var("est_nitrate_sta_corr"), "est_nitrate_cruise_corr")
        self.assertIsNone(dr.sibling_var("salinity_ave_corr"))
        self.assertIsNone(dr.sibling_var("oxygen_ml_l_ave_cruise_corr"))

    def test_withheld_sta_corr_falls_back_to_cruise_corr(self):
        fb = dr.cruise_fallbacks({"oxygen_ml_l_ave_sta_corr"},
                                 ["temperature_ave", "oxygen_ml_l_ave_cruise_corr"])
        self.assertEqual(fb, {"oxygen_ml_l_ave_sta_corr": "oxygen_ml_l_ave_cruise_corr"})

    def test_no_sibling_on_offer_no_fallback(self):
        # the release without oxygen_ml_l_ave_cruise_corr (before the release cut
        # 2026-10-01): nothing to fall back to, so the app uses its old default
        self.assertEqual(dr.cruise_fallbacks({"oxygen_ml_l_ave_sta_corr"},
                                             ["temperature_ave"]), {})

    def test_withheld_sibling_is_not_offered(self):
        # the cruise-corrected series is itself withheld -> never a fallback target
        g = {"temperature_ave": grid(15, profile),
             "est_nitrate_sta_corr": grid(15, lambda j, i: None),
             "est_nitrate_cruise_corr": grid(15, lambda j, i: 1.0 + 0.2 * j)}
        kept, w = dr.apply_variable_rules(g)
        self.assertEqual(dr.cruise_fallbacks(set(w), kept), {})

    def test_available_var_has_no_fallback(self):
        self.assertEqual(dr.cruise_fallbacks(
            {"est_nitrate_sta_corr"}, ["est_nitrate_sta_corr", "est_nitrate_cruise_corr"]), {})

    def test_generic_in_the_variable_name(self):
        self.assertEqual(dr.cruise_fallbacks({"foo_sta_corr"}, ["foo_cruise_corr"]),
                         {"foo_sta_corr": "foo_cruise_corr"})


class TestStage(unittest.TestCase):
    """Rule 5: a CTD-only preliminary cruise shows temperature alone, so the
    ctd-derived series (ctd-transects#5, #6) never reach one."""

    def setUp(self):
        self.vars = ["temperature_ave", "salinity_ave_corr", "sigma_theta_ave",
                     "spiciness0", "fluorescence_v"]
        self.grids = {v: grid(5, profile) for v in self.vars}

    def test_derived_vars_are_named(self):
        self.assertEqual(dr.DERIVED_VARS, {"sigma_theta_ave", "spiciness0"})

    def test_without_bottle_is_temperature_only(self):
        g, a = dr.stage_filter("preliminary_without_bottle", self.grids, dict(self.grids))
        self.assertEqual(list(g), ["temperature_ave"])
        self.assertEqual(list(a), ["temperature_ave"])
        self.assertFalse(dr.DERIVED_VARS & set(g))

    def test_final_and_with_bottle_keep_the_derived_vars(self):
        for stage in ("final", "preliminary_with_bottle"):
            (g,) = dr.stage_filter(stage, self.grids)
            self.assertEqual(list(g), self.vars, stage)
            self.assertTrue(dr.DERIVED_VARS <= set(g), stage)

    def test_unknown_stage_is_unrestricted(self):
        (g,) = dr.stage_filter(None, self.grids)
        self.assertEqual(list(g), self.vars)

    def test_inputs_are_not_mutated(self):
        dr.stage_filter("preliminary_without_bottle", self.grids)
        self.assertEqual(list(self.grids), self.vars)


class TestRasmus20250422(unittest.TestCase):
    """The line 93.3 / 2025-04-3322 picture in one section."""

    def test_the_screenshots(self):
        g = {
            "temperature_ave": grid(15, profile),
            "oxygen_ml_l_ave_sta_corr": grid(15, lambda j, i: profile(j, i) if j in (6, 7, 8) else None),
            "oxygen_ml_l_ave_cruise_corr": grid(15, profile),
            "est_nitrate_cruise_corr": grid(15, lambda j, i: 0.5 + 0.25 * j),
            "est_nitrate_sta_corr": grid(15, profile),
        }
        kept, w = dr.apply_variable_rules(g)
        # (a) DO at 3 of 15 stations is withheld and the reader lands on the
        #     cruise-corrected sibling
        # (b) one nitrate value per cast is withheld; its station-corrected sibling stays
        self.assertEqual(w, {
            "oxygen_ml_l_ave_sta_corr": "data at 3 of 15 stations",
            "est_nitrate_cruise_corr": dr.WITHHELD_CONSTANT_REASON,
        })
        self.assertEqual(sorted(kept), ["est_nitrate_sta_corr", "oxygen_ml_l_ave_cruise_corr",
                                        "temperature_ave"])
        self.assertEqual(dr.cruise_fallbacks(set(w), kept),
                         {"oxygen_ml_l_ave_sta_corr": "oxygen_ml_l_ave_cruise_corr"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
