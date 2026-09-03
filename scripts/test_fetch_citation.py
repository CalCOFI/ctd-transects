#!/usr/bin/env python3
"""Self-test for scripts/fetch_citation.py — one fixture per finding shape.

    python3 scripts/test_fetch_citation.py
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fetch_citation as fc  # noqa: E402

CATALOG_WITH_CITATION = {
    "version": "v2026.09.03",
    "citation": "CalCOFI (2026). CalCOFI Integrated Database, release v2026.09.03 [Data set]. …",
    "concept_doi": "10.5281/zenodo.22281994",
    "doi": "10.5281/zenodo.22281995",
}

CATALOG_LEGACY = {"version": "v2026.08.25"}  # no citation/doi keys at all — pre-2026-09-03

METADATA_WITH_DATASET = {
    "datasets": {
        "calcofi_ctd-cast": {
            "citation_main": "CalCOFI (2026). CTD Cast Files. Scripps Institution of Oceanography.",
            "license": "CC-BY-4.0",
        },
        "calcofi_bottle": {"citation_main": "should not appear", "license": "CC-BY-4.0"},
    }
}

METADATA_NO_DATASET = {"datasets": {"calcofi_bottle": {"citation_main": "x", "license": "CC-BY-4.0"}}}

METADATA_EMPTY_CITATION = {"datasets": {"calcofi_ctd-cast": {"citation_main": "", "license": ""}}}


class TestCitationBlock(unittest.TestCase):
    def test_full_release_and_dataset(self):
        out = fc.citation_block(CATALOG_WITH_CITATION, METADATA_WITH_DATASET)
        self.assertEqual(out["release"], CATALOG_WITH_CITATION["citation"])
        self.assertEqual(out["release_doi"], "10.5281/zenodo.22281995")  # version DOI wins over concept
        self.assertEqual(out["dataset"], METADATA_WITH_DATASET["datasets"]["calcofi_ctd-cast"]["citation_main"])
        self.assertEqual(out["license"], "CC-BY-4.0")

    def test_concept_doi_fallback_before_a_version_doi_is_minted(self):
        catalog = {**CATALOG_WITH_CITATION, "doi": None}
        out = fc.citation_block(catalog, METADATA_WITH_DATASET)
        self.assertEqual(out["release_doi"], "10.5281/zenodo.22281994")

    def test_legacy_catalog_has_no_release_keys(self):
        out = fc.citation_block(CATALOG_LEGACY, METADATA_WITH_DATASET)
        self.assertNotIn("release", out)
        self.assertNotIn("release_doi", out)
        # the dataset side is independent of the catalog and still comes through
        self.assertIn("dataset", out)

    def test_dataset_absent_from_metadata(self):
        out = fc.citation_block(CATALOG_WITH_CITATION, METADATA_NO_DATASET)
        self.assertNotIn("dataset", out)
        self.assertNotIn("license", out)

    def test_empty_strings_are_not_emitted(self):
        # pre-WS-A1 metadata: citation_main/license present as keys but blank
        out = fc.citation_block(CATALOG_WITH_CITATION, METADATA_EMPTY_CITATION)
        self.assertNotIn("dataset", out)
        self.assertNotIn("license", out)

    def test_nothing_at_all_yields_empty_object(self):
        out = fc.citation_block(CATALOG_LEGACY, {"datasets": {}})
        self.assertEqual(out, {})


if __name__ == "__main__":
    unittest.main()
