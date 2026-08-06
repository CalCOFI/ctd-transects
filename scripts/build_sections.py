"""build_sections.py — reshape the flat SQL output into per-section JSON shards.

    python3 scripts/build_sections.py        (after scripts/build_sections.sql)

Reads the `public/data/_*.parquet` intermediates and writes:

    public/data/index.json                    lines, cruises, variables
    public/data/stations.json                 the CalCOFI grid, for the map
    public/data/sections/{line}__{cruise}.json  one shard per transect

WHY SHARDS, AND WHY A MATRIX
    The app draws one (line, cruise, variable) section at a time, so it should
    fetch one. All sections together are ~1.5M values; a single bundle would be
    ~40 MB of JSON parsed before first paint.

    Each shard stores every variable as a station x depth MATRIX rather than a
    list of points: with stations as columns and 5 m depth bins as rows, a
    typical section is ~11 x 101 = ~1,100 numbers per variable. That is both
    smaller than the equivalent point list AND exactly the `z` argument a Plotly
    heatmap wants, so the browser does no reshaping and — importantly — no
    interpolation.

    apps/ctd-viz gets its smooth ODV-style field from MBA::mba.surf(), a
    multilevel B-spline with no JavaScript port. Rather than reimplement it, the
    app hands the matrix to Plotly with zsmooth:"best" and lets the renderer
    interpolate. Same look, no numerical code in the client.

DISTANCE
    Cumulative great-circle distance between consecutive stations, nearshore
    first — the section's x-axis. Baked here because it is a property of the
    transect, not of the view.
"""

import json
import math
import re
from collections import defaultdict
from pathlib import Path

import pandas as pd

DATA = Path("public/data")
SECTIONS = DATA / "sections"

# depth grid: 0-500 m in 5 m bins, matching the binning in build_sections.sql
DEPTHS = list(range(0, 505, 5))

# Display order and grouping for the variable picker. The corrected forms are the
# headline series; the uncorrected sensor series exist so that a preliminary_without_bottle
# cruise — one that has not been through the bottle merge, and therefore has NO
# corrected salinity or oxygen at all — still plots something rather than an empty
# panel. `prefer` names the corrected series a raw one stands in for.
VAR_ORDER = [
    ("temperature_ave",          None),
    ("salinity_ave_corr",        None),
    ("oxygen_ml_l_ave_sta_corr", None),
    ("sigma_theta_1",            None),
    ("fluorescence_v",           None),
    ("salinity_1",               "salinity_ave_corr"),
    ("oxygen_ml_l_1",            "oxygen_ml_l_ave_sta_corr"),
]

# lines with near-complete cruise coverage; everything else is offered but not
# defaulted to (lines 60-73.3 appear on ~20-27 cruises, vs ~90 for these)
CORE_LINES = ["93.3", "90", "86.7", "83.3", "80", "76.7"]

# The three-tier vocabulary the app knows how to badge. The pre-2026.08 bare
# `preliminary` is deliberately NOT here: it predates knowing there are two
# preliminary tiers, so it cannot say whether the bottle merge has run — and on a
# without-bottle cruise the bottle-corrected salinity and oxygen do not exist at
# all, which is exactly what a reader needs told.
#
# Failing HERE rather than in the browser is the point: a release still carrying
# the old value is a build-time fact, and a shard that reached the app with an
# unbadgeable stage would render as a bare cruise with no caveat.
KNOWN_STAGES = {"final", "preliminary_with_bottle", "preliminary_without_bottle"}


def haversine_km(lon1, lat1, lon2, lat2):
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def fmt_line(v):
    """90.0 -> '90', 93.3 -> '93.3' — the label used in filenames and the UI."""
    return f"{v:g}"


def floor_profile(line_floor, knots_along, knots_x):
    """Place the dense along-line seafloor on this section's x-axis.

    Two different rulers are in play. `line_dist_km` runs along the full line
    from its most-inshore grid station; the section's x runs station-to-station
    over only the stations this cruise occupied. They agree exactly at every
    occupied station and differ slightly between them (straight-line hop vs
    distance along the line), so map one to the other piecewise-linearly with the
    stations as knots. Nothing is invented: the profile is anchored at every
    station and merely stretched between them.
    """
    if line_floor is None or len(knots_along) < 2:
        return None
    lo, hi = knots_along[0], knots_along[-1]
    seg = line_floor[(line_floor["line_dist_km"] >= lo)
                     & (line_floor["line_dist_km"] <= hi)]
    if seg.empty:
        return None
    xs, ys = [], []
    for a, b in zip(seg["line_dist_km"], seg["bathy_m"]):
        if pd.isna(b):
            continue
        # locate `a` between the bracketing station knots
        i = 0
        while i < len(knots_along) - 2 and a > knots_along[i + 1]:
            i += 1
        span = knots_along[i + 1] - knots_along[i]
        f = 0.0 if span <= 0 else (a - knots_along[i]) / span
        xs.append(round(knots_x[i] + f * (knots_x[i + 1] - knots_x[i]), 2))
        ys.append(float(b))
    if len(xs) < 2:
        return None
    return {"dist_km": xs, "bathy_m": ys}


def slug(s):
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(s))


def main():
    sec = pd.read_parquet(DATA / "_sections.parquet")
    sta = pd.read_parquet(DATA / "_stations.parquet")
    cru = pd.read_parquet(DATA / "_cruises.parquet")
    var = pd.read_parquet(DATA / "_variables.parquet")
    bathy = pd.read_csv("metadata/station_bathymetry.csv")

    sec["line_s"] = sec["line"].map(fmt_line)
    sta["line_s"] = sta["line"].map(fmt_line)

    sta = sta.merge(bathy[["grid_key", "bathy_m", "line_dist_km"]],
                    on="grid_key", how="left")

    # the seafloor sampled every 2 km along each line, keyed by distance from the
    # line's most-inshore grid station
    floor = pd.read_csv("metadata/line_bathymetry.csv")
    floor["line_s"] = floor["line"].map(fmt_line)
    floor_by_line = {k: v.sort_values("line_dist_km")
                     for k, v in floor.groupby("line_s")}

    ship = dict(zip(cru["cruise_key"], cru["ship_name"].fillna("")))

    stages = set(sta["data_stage"].dropna().unique())
    unknown = stages - KNOWN_STAGES
    if unknown:
        raise SystemExit(
            f"release carries unbadgeable data_stage value(s): {sorted(unknown)}\n"
            f"  expected one of: {sorted(KNOWN_STAGES)}\n"
            "  'preliminary' means the release predates the 2026-08-06 split and "
            "cannot say whether the bottle merge has run; rebuild from a release "
            "that post-dates it.")

    var_meta = {r["var"]: r for _, r in var.iterrows()}
    present = set(sec["var"].unique())
    variables = []
    for v, prefer in VAR_ORDER:
        if v not in present:
            continue
        m = var_meta.get(v, {})
        variables.append({
            "var": v,
            "label": (m.get("description") or v),
            "units": (m.get("units") or ""),
            "prefer": prefer,
            "uncorrected": prefer is not None,
        })

    SECTIONS.mkdir(parents=True, exist_ok=True)
    for old in SECTIONS.glob("*.json"):
        old.unlink()

    depth_ix = {d: i for i, d in enumerate(DEPTHS)}
    index_lines = defaultdict(dict)
    n_written = 0

    for (line_s, cruise_key), g in sec.groupby(["line_s", "cruise_key"], sort=False):
        st = (sta[(sta["line_s"] == line_s) & (sta["cruise_key"] == cruise_key)]
              .sort_values("sta"))          # ascending station = nearshore -> offshore
        if st.empty:
            continue

        lons = st["lon"].tolist()
        lats = st["lat"].tolist()
        dist = [0.0]
        for i in range(1, len(lons)):
            dist.append(dist[-1] + haversine_km(lons[i - 1], lats[i - 1],
                                                lons[i], lats[i]))

        stations = []
        for i, (_, r) in enumerate(st.iterrows()):
            dt = r["datetime"]
            stations.append({
                "sta": float(r["sta"]),
                "grid_key": r["grid_key"],
                "lon": round(float(r["lon"]), 5),
                "lat": round(float(r["lat"]), 5),
                "dist_km": round(dist[i], 2),
                "bathy_m": (None if pd.isna(r["bathy_m"]) else float(r["bathy_m"])),
                "datetime": (None if pd.isna(dt) else pd.Timestamp(dt).isoformat()),
            })

        sta_ix = {float(r["sta"]): i for i, (_, r) in enumerate(st.iterrows())}
        n_sta = len(stations)

        # z[depth][station] — the orientation Plotly's heatmap takes directly
        grids = {}
        for v, gv in g.groupby("var", sort=False):
            z = [[None] * n_sta for _ in DEPTHS]
            for s, d, val in zip(gv["sta"], gv["depth_m"], gv["value"]):
                si = sta_ix.get(float(s))
                di = depth_ix.get(int(d))
                if si is not None and di is not None:
                    z[di][si] = None if pd.isna(val) else float(val)
            grids[v] = z

        knots_along = [x for x in st["line_dist_km"].tolist()]
        prof = None
        if not any(pd.isna(k) for k in knots_along):
            prof = floor_profile(floor_by_line.get(line_s), knots_along, dist)

        stage = st["data_stage"].dropna()
        shard = {
            "line": line_s,
            "cruise_key": cruise_key,
            "data_stage": (stage.iloc[0] if len(stage) else None),
            "stations": stations,
            "depths": DEPTHS,
            "vars": grids,
            "floor": prof,
        }

        fn = f"{slug(line_s)}__{slug(cruise_key)}.json"
        with open(SECTIONS / fn, "w") as f:
            json.dump(shard, f, separators=(",", ":"), allow_nan=False)
        n_written += 1

        index_lines[line_s][cruise_key] = {
            "cruise_key": cruise_key,
            "ship": ship.get(cruise_key, ""),
            "data_stage": shard["data_stage"],
            "n_stations": n_sta,
            "vars": sorted(grids.keys()),
            "file": f"sections/{fn}",
        }

    # cruises newest first. cruise_key is YYYY-MM-NODC, so a plain reverse string
    # sort IS year-month descending — no date parsing, and it stays correct for
    # the 1993 cruises the Wilkinson archive adds.
    lines = []
    for line_s, cruises in index_lines.items():
        lines.append({
            "line": line_s,
            "core": line_s in CORE_LINES,
            "n_cruises": len(cruises),
            "cruises": sorted(cruises.values(),
                              key=lambda c: c["cruise_key"], reverse=True),
        })
    # core lines first (north to south within each group), then the rest
    lines.sort(key=lambda l: (not l["core"], -float(l["line"])))

    release = json.loads((DATA / "version.json").read_text())["release"] \
        if (DATA / "version.json").exists() else None

    index = {
        "release": release,
        "lines": lines,
        "variables": variables,
        "default": {
            "line": lines[0]["line"] if lines else None,
            "cruise_key": lines[0]["cruises"][0]["cruise_key"] if lines else None,
            "var": "temperature_ave",
        },
    }
    with open(DATA / "index.json", "w") as f:
        json.dump(index, f, separators=(",", ":"), allow_nan=False)

    # the full grid, for the map: every station, whether or not it was occupied
    grid = pd.read_parquet(DATA / "_grid.parquet")
    grid["line_s"] = grid["line"].map(fmt_line)
    grid = grid.merge(bathy[["grid_key", "bathy_m"]], on="grid_key", how="left")
    stations_json = [
        {
            "grid_key": r["grid_key"],
            "line": r["line_s"],
            "sta": float(r["sta"]),
            "lon": round(float(r["lon"]), 5),
            "lat": round(float(r["lat"]), 5),
        }
        for _, r in grid.iterrows()
        if r["line_s"] in index_lines
    ]
    with open(DATA / "stations.json", "w") as f:
        json.dump(stations_json, f, separators=(",", ":"), allow_nan=False)

    total = sum(p.stat().st_size for p in SECTIONS.glob("*.json"))
    print(f"sections : {n_written} shards, {total / 1e6:.1f} MB "
          f"(mean {total / max(n_written, 1) / 1e3:.0f} KB)")
    print(f"index    : {len(lines)} lines, {len(variables)} variables")
    print(f"stations : {len(stations_json)} grid stations")


if __name__ == "__main__":
    main()
