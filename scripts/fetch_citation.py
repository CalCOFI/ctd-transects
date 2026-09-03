#!/usr/bin/env python3
"""fetch_citation.py — the citation block for public/data/version.json.

This page already resolves every release through resolve_release.py's catalog
fetch; this script reads that same catalog.json (WS-A0's `citation` /
`doi` / `concept_doi`, calcofi4db >= 3.30.0) plus the release's metadata.json
(the `calcofi_ctd-cast` dataset's `citation_main` / `license`) and writes what
it finds — nothing is authored here, and a field the source doesn't carry is
simply absent from the output rather than guessed.

Both fields are new as of the 2026-09-03 attribution contract. A release cut
before that has no `citation` on its catalog and no `citation_main` worth
showing (13 of 16 datasets shipped an empty one before WS-A1), so this prints
`{}` rather than a placeholder, and refresh.yml folds that straight into
version.json — the About page and footer stay hidden until a release actually
carries the data (see public/app.js's renderCitation()).

    python3 scripts/fetch_citation.py --version v2026.09.03
    python3 scripts/fetch_citation.py --catalog build/catalog.json --version v2026.09.03
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import resolve_release as rr  # noqa: E402

DATASET_KEY = "calcofi_ctd-cast"


def fetch_metadata(version: str) -> dict:
    return json.loads(rr.fetch_text(f"{rr.RELEASES_HTTPS}/{version}/metadata.json"))


def citation_block(catalog: dict, metadata: dict) -> dict:
    out: dict[str, str] = {}
    release = catalog.get("citation")
    if release:
        out["release"] = release
    doi = catalog.get("doi") or catalog.get("concept_doi")
    if doi:
        out["release_doi"] = doi
    ds = (metadata.get("datasets") or {}).get(DATASET_KEY) or {}
    if ds.get("citation_main"):
        out["dataset"] = ds["citation_main"]
    if ds.get("license"):
        out["license"] = ds["license"]
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", help="release version (default: latest.txt); ignored with --catalog")
    p.add_argument("--catalog", help="read this catalog.json instead of fetching one (e.g. build/catalog.json, "
                                     "already fetched by resolve_release.py for the same build)")
    p.add_argument("--metadata", help="read this metadata.json instead of fetching one (fixtures, offline)")
    a = p.parse_args(argv)

    if a.catalog:
        with open(a.catalog) as f:
            catalog = json.load(f)
    else:
        catalog = rr.fetch_catalog(rr.resolve_version(a.version))
    version = catalog["version"]

    if a.metadata:
        with open(a.metadata) as f:
            metadata = json.load(f)
    else:
        metadata = fetch_metadata(version)

    print(json.dumps(citation_block(catalog, metadata)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
