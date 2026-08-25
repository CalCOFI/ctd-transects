#!/usr/bin/env python3
"""resolve_release.py — render scripts/build_*.sql against a release catalog.

Where a CalCOFI release table's parquet lives is decided by the release's
catalog.json, not by a path a consumer builds by hand. Since the v2026.09
releases the database is content-addressed: each table (or each partition of a
partitioned table) is one immutable object under
gs://calcofi-db/ducklake/tables/{table}/[{key}={value}/]{hash}/…, listed per
table in the catalog's `objects[]`. A release that changed three tables shares
every other object with the release before it, and the archive can drop a
retired version's own copies — so a releases/{version}/parquet/… path is only
guaranteed to answer for the promoted and consolidated versions.

This script is the one place this repo turns a catalog entry into URLs. The
build SQL carries tokens instead of paths, and this renders each token into a
literal DuckDB expression, writing the result to a build directory:

    __RELEASE__                             -> the version string (v2026.08.25)
    __TBL:grid__                            -> read_parquet('https://…/grid.parquet')
    __TBL:obs__                             -> read_parquet(['https://…', …], hive_partitioning = true)
    __TBL:obs:dataset_key=cce-lter_zoodb__  -> the same, for just that partition

Rules, in order (mirrors calcofi4py.release.release_sources() and
calcofi4r::cc_release_sources() — keep the three in step):
  1. the entry has `objects[]` -> one https URL per object, in catalog order
     (a partition file carries its key=value segment, so DuckDB's
     hive_partitioning = true recovers the partition column from the URL; an
     explicit list also reads over plain https, where a ** glob cannot);
  2. otherwise (releases before v2026.09) -> the legacy per-release path:
     https://…/releases/{version}/parquet/{table}.parquet, or a gs:// glob for
     a partitioned table (DuckDB cannot glob over https; the public bucket
     reads anonymously over gs://).

A partitioned table may ALSO publish one whole-table file (obs does, for
browser DuckDB-WASM and other https-only readers that cannot take a list): the
object without a partition. It is reported as `single_file`, never mixed into
the partition list — reading both would double every row.

It also writes a small tables.json ({version, layout, tables: {name: {urls,
hive, canonical, partition_by, single_file}}}) for the tables a browser app
reads directly, so the app resolves the same way without fetching the catalog.

    python3 scripts/resolve_release.py                       # latest.txt, every scripts/build_*.sql -> build/
    python3 scripts/resolve_release.py --version v2026.08.25 scripts/build_stations.sql
    python3 scripts/resolve_release.py --catalog build/catalog.json --expr region
    python3 scripts/test_resolve_release.py                  # the self-test (stdlib unittest)

Standard library only — it runs inside GitHub Actions before anything is
installed.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
import urllib.request

BUCKET_HTTPS = "https://storage.googleapis.com/calcofi-db"
BUCKET_GS = "gs://calcofi-db"
RELEASES_HTTPS = f"{BUCKET_HTTPS}/ducklake/releases"

# __TBL:<table>__ or __TBL:<table>:<key>=<value>__ ; non-greedy up to the
# closing __ so a table or value containing single underscores still matches
TOKEN_RE = re.compile(r"__TBL:([a-z0-9_]+?)(?::([a-z0-9_]+?)=([^\s'\"]+?))?__")
LEFTOVER_RE = re.compile(r"__(?:TBL:\S*?|RELEASE)__")


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


def fetch_text(url: str) -> str:
    with urllib.request.urlopen(url, timeout=60) as r:  # noqa: S310 (fixed public host)
        return r.read().decode()


def resolve_version(version: str | None) -> str:
    """`None`/`latest` -> the promoted version from latest.txt; else as given."""
    if version in (None, "", "latest"):
        return fetch_text(f"{RELEASES_HTTPS}/latest.txt").strip().splitlines()[0]
    if not version.startswith("v"):
        sys.exit(f"resolve_release: version must be 'latest' or like 'v2026.08.25', got {version!r}")
    return version


def fetch_catalog(version: str) -> dict:
    return json.loads(fetch_text(f"{RELEASES_HTTPS}/{version}/catalog.json"))


def release_sources(catalog: dict, table: str, partition: tuple[str, str] | None = None) -> dict:
    """Where a release table's (or one partition's) parquet bytes live.

    Returns {"urls": [...], "hive": bool, "canonical": bool, "partition_by": str|None,
    "single_file": str|None} — `single_file` is the whole-table file a partitioned
    table may also publish (obs does) for https-only readers that cannot take a
    list. Read it OR `urls`, never both: that would double every row.
    """
    entry = next((t for t in catalog["tables"] if t["name"] == table), None)
    if entry is None:
        sys.exit(f"resolve_release: table {table!r} is not in the catalog for {catalog.get('version')}")
    partitioned = bool(entry.get("partitioned"))
    if partition and not partitioned:
        sys.exit(f"resolve_release: table {table!r} is not partitioned, cannot take {partition[0]}={partition[1]}")
    objs = entry.get("objects") or []
    if objs:
        single_file = None
        if partitioned:
            # the whole-table twin is the object without a partition; the
            # partitions are the rest
            twins = [o for o in objs if "partition_by" not in o]
            objs = [o for o in objs if "partition_by" in o]
            if twins:
                single_file = f"{BUCKET_HTTPS}/{twins[0]['path']}"
        partition_by = objs[0].get("partition_by") if partitioned and objs else None
        if partition:
            key, value = partition
            objs = [o for o in objs
                    if o.get("partition_by") == key and str(o.get("partition_value")) == value]
            if not objs:
                sys.exit(f"resolve_release: no object of {table!r} has {key}={value} "
                         f"in the catalog for {catalog['version']}")
        return {"urls": [f"{BUCKET_HTTPS}/{o['path']}" for o in objs],
                "hive": partitioned, "canonical": True, "partition_by": partition_by,
                "single_file": single_file}
    # legacy layout (releases before v2026.09): one parquet per table, or a
    # hive-partitioned directory that only a gs:// glob can enumerate
    base = f"ducklake/releases/{catalog['version']}/parquet"
    if partitioned:
        sub = f"{partition[0]}={partition[1]}/*.parquet" if partition else "**/*.parquet"
        return {"urls": [f"{BUCKET_GS}/{base}/{table}/{sub}"],
                "hive": True, "canonical": False, "partition_by": partition[0] if partition else None,
                # obs is the one legacy partitioned table with a single-file twin
                "single_file": f"{BUCKET_HTTPS}/{base}/obs.parquet" if table == "obs" else None}
    return {"urls": [f"{BUCKET_HTTPS}/{base}/{table}.parquet"],
            "hive": False, "canonical": False, "partition_by": None, "single_file": None}


def read_parquet_sql(src: dict) -> str:
    """The read_parquet(...) SQL for a resolved source (mirrors cc_read_parquet_sql())."""
    urls = src["urls"]
    lst = f"'{urls[0]}'" if len(urls) == 1 else "[" + ", ".join(f"'{u}'" for u in urls) + "]"
    return f"read_parquet({lst}, hive_partitioning = true)" if src["hive"] else f"read_parquet({lst})"


def parse_spec(spec: str) -> tuple[str, tuple[str, str] | None]:
    """'obs' -> ('obs', None); 'obs:dataset_key=x' -> ('obs', ('dataset_key', 'x'))."""
    table, _, part = spec.partition(":")
    if not part:
        return table, None
    key, eq, value = part.partition("=")
    if not eq or not key or not value:
        sys.exit(f"resolve_release: bad table spec {spec!r} (want table or table:key=value)")
    return table, (key, value)


def render(sql: str, catalog: dict) -> str:
    """Substitute every token in the SQL; refuse to hand back code with one left over.

    `--` line comments are left verbatim, so a header can spell `__TBL:<table>__`
    to explain itself without being rendered or tripping the leftover check
    (no build script carries `--` inside a string literal).
    """
    def sub(m: re.Match) -> str:
        table, key, value = m.groups()
        return read_parquet_sql(release_sources(catalog, table, (key, value) if key else None))
    out = []
    for line in sql.split("\n"):
        code, dashes, comment = line.partition("--")
        code = TOKEN_RE.sub(sub, code).replace("__RELEASE__", catalog["version"])
        left = LEFTOVER_RE.search(code)
        if left:
            sys.exit(f"resolve_release: unrendered token {left.group(0)!r} — check its spelling")
        out.append(code + dashes + comment)
    return "\n".join(out)


def tables_json(catalog: dict, tables: list[str]) -> dict:
    return {"version": catalog["version"],
            "layout": "canonical" if any(t.get("objects") for t in catalog["tables"]) else "legacy",
            "tables": {t: release_sources(catalog, t) for t in tables}}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("sql", nargs="*",
                   help="SQL files to render (default: every scripts/build_*.sql carrying a token)")
    p.add_argument("--version", help="release version (default: latest.txt); ignored with --catalog")
    p.add_argument("--catalog", help="read this catalog.json instead of fetching one (fixtures, offline)")
    p.add_argument("--out-dir", default="build", help="where rendered SQL goes (default: build/)")
    p.add_argument("--tables-json", help="also write {version, tables} here for the browser app")
    p.add_argument("--tables", default="", help="comma-separated tables to put in --tables-json")
    p.add_argument("--expr", metavar="TABLE[:KEY=VALUE]",
                   help="print the read_parquet() expression for one table (or partition) and exit")
    a = p.parse_args(argv)

    if a.catalog:
        with open(a.catalog) as f:
            catalog = json.load(f)
    else:
        catalog = fetch_catalog(resolve_version(a.version))
    version = catalog["version"]
    if a.version and a.catalog and a.version != version:
        log(f"resolve_release: note: --version {a.version} ignored, catalog is {version}")

    if a.expr:
        table, partition = parse_spec(a.expr)
        print(read_parquet_sql(release_sources(catalog, table, partition)))
        return 0

    layout = "canonical" if any(t.get("objects") for t in catalog["tables"]) else "legacy"
    log(f"resolve_release: {version} ({layout} layout)")

    sql_files = a.sql or [f for f in sorted(glob.glob("scripts/build_*.sql"))
                          if LEFTOVER_RE.search(open(f).read())]
    if sql_files:
        out_dir = os.path.abspath(a.out_dir)
        if any(os.path.abspath(os.path.dirname(f)) == out_dir for f in sql_files):
            sys.exit("resolve_release: --out-dir would overwrite the source SQL")
        os.makedirs(out_dir, exist_ok=True)
        # keep the catalog beside the rendered SQL so a later --expr / re-render
        # in the same build resolves against the same bytes without a refetch
        with open(os.path.join(out_dir, "catalog.json"), "w") as f:
            json.dump(catalog, f)
        for src in sql_files:
            with open(src) as f:
                text = f.read()
            dst = os.path.join(out_dir, os.path.basename(src))
            with open(dst, "w") as f:
                f.write(render(text, catalog))
            log(f"  {src} -> {os.path.relpath(dst)}")

    if a.tables_json:
        tables = [t for t in a.tables.split(",") if t]
        if not tables:
            sys.exit("resolve_release: --tables-json needs --tables a,b,…")
        os.makedirs(os.path.dirname(os.path.abspath(a.tables_json)), exist_ok=True)
        with open(a.tables_json, "w") as f:
            json.dump(tables_json(catalog, tables), f, indent=1)
            f.write("\n")
        log(f"  {a.tables_json} ({', '.join(tables)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
