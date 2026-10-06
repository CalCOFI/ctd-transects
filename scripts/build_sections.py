"""build_sections.py — reshape the flat SQL output into per-section JSON shards.

    python3 scripts/build_sections.py        (after scripts/build_sections.sql)

Reads the `public/data/_*.parquet` intermediates and writes:

    public/data/index.json                    lines, cruises, variables, baseline
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

DISTANCE — TWO RULERS, BOTH BAKED
    `dist_km` is cumulative great-circle distance between the stations THIS
    cruise occupied, starting at 0. The section fills the plot, which is what you
    want when reading one cruise.

    `line_dist_km` is each station's distance along the FULL line from its most
    inshore grid station, so every cruise on a line shares one ruler and their
    widths are directly comparable.

    Both ship, and the app toggles; neither can be derived in the browser from the
    other. It is not cosmetic. Line 93.3 has not been sampled past station 90
    since 2025-01, though 113 of the 130 cruises before it reached station 120 —
    so under the default ruler a recent section is drawn the same width as a
    historical one that covered 40% more ocean, and comparing them by eye
    overstates recent gradients.

ANOMALY
    Each shard carries an `anom` matrix beside every `vars` matrix, differenced
    against the release's own `climatology` table (calcofi4db::build_climatology():
    station x cruise month x 10 m bin, 1993-2013, >= 5 cruises) — the same
    baseline the CalCOFI Explorer subtracts. Cells with no baseline are null, never 0.

WHAT IS NOT DRAWN (scripts/display_rules.py)
    A section with fewer than 3 stations is not written. Within a section, a
    variable with data at fewer than 3 stations or fewer than half the section's
    stations, or constant with depth at every station that has it over >= 50 m,
    is dropped and recorded in the shard's
    `withheld` ({var: reason}) so the app can say so. Rasmus Swalethorp's
    2026-09-29 screenshots of line 93.3 / 2025-04-3322 are the fixtures.
"""

import json
import math
import re
from collections import defaultdict
from pathlib import Path

import pandas as pd

from display_rules import (MIN_STATIONS, apply_variable_rules, cruise_fallbacks,
                           section_is_drawable, stage_filter)

DATA = Path("public/data")
SECTIONS = DATA / "sections"

# depth grid: 0-500 m in 10 m FLOOR bins (labelled by the shallow edge), matching
# build_sections.sql and the release's climatology / obs_env depth_bin
DEPTHS = list(range(0, 510, 10))

# Display order for the variable picker: the corrected, sensor-pair-combined series
# only. The uncorrected per-sensor salinity_1 / oxygen_ml_l_1 fallbacks are gone
# (Rasmus, 2026-09-16: "individual sensors … we can get rid of"). sigma_theta_1 is
# replaced by calcofi_ctd-derived's sensor-pair average sigma_theta_ave, with no
# fallback to sensor 1 (ctd-transects#5); spiciness0 is new (#6). fluorescence_v
# stays until Rasmus decides on it (#10).
VAR_ORDER = [
    ("temperature_ave",               None),
    ("salinity_ave_corr",             None),
    ("sigma_theta_ave",               None),
    ("spiciness0",                    None),
    ("oxygen_ml_l_ave_sta_corr",      None),
    ("oxygen_ml_l_ave_cruise_corr",   None),
    ("est_chlorophyll_a_sta_corr",    None),
    ("est_chlorophyll_a_cruise_corr", None),
    ("est_nitrate_sta_corr",          None),
    ("est_nitrate_cruise_corr",       None),
    ("fluorescence_v",                None),
]

# Picker labels where the registry's description is a definition rather than a
# name (it stays the registry's; this is only what fits in a <select>). The units
# still come from the registry.
SHORT_LABEL = {
    "sigma_theta_ave": "Potential density σθ (sensor-pair average)",
    "spiciness0":      "Spice (+ spicy, − minty)",
}

# What a cruise at each processing tier may show is display_rules.STAGE_VARS,
# enforced HERE so the published JSON carries the rule, not only the browser
# (app.js availableVars() applies the same set). A CTD-only preliminary cruise has
# had no bottle correction, and a drifting or uncalibrated sensor reads as a real
# signal in the anomaly view (Rasmus, 2026-09-09), so it ships temperature only.

# The release's measurement_type registry has no row yet for
# oxygen_ml_l_ave_cruise_corr (added in CalCOFI/workflows, reaching the next
# release); until then the picker would show the bare column name. Delete this entry
# once `index.json` shows the registry's own label for it.
FALLBACK_META = {
    "oxygen_ml_l_ave_cruise_corr": {
        "description": "DO average cruise-corrected",
        "units": "ml/L",
    },
}

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


def interp_linear(xs, ys, x, extrapolate=False):
    """Piecewise-linear y at x through the (sorted, ascending) knots xs/ys.

    Outside [xs[0], xs[-1]] it extends the end segment when `extrapolate`, else
    returns None. None also when there are fewer than 2 knots.
    """
    if len(xs) < 2:
        return None
    if x < xs[0] or x > xs[-1]:
        if not extrapolate:
            return None
        i = 0 if x < xs[0] else len(xs) - 2
    else:
        i = 0
        while i < len(xs) - 2 and x > xs[i + 1]:
            i += 1
    span = xs[i + 1] - xs[i]
    f = 0.0 if span <= 0 else (x - xs[i]) / span
    return ys[i] + f * (ys[i + 1] - ys[i])


def station_ruler(line_grid, sta):
    """A station's `line_dist_km` on its section line, from its station NUMBER.

    `line_grid` is the line's rows of metadata/station_bathymetry.csv (sta,
    line_dist_km), one per grid cell. Until 2026-10-04 a section station took the
    ruler of the grid cell it fell in, so 90.28 and 90.30 (one cell then) sat on
    the same x, and from v2026.10.04 a SCCOOS station on its own one-cell "line"
    (93.4 26.4, drawn on line 93.3) would have taken that line's ruler: 0 km.
    `+proj=calcofi` is equidistant along a line (7.386 km per station unit), so
    the station number interpolates the ruler exactly between grid stations and
    extends it past the ends.
    """
    g = line_grid.dropna(subset=["line_dist_km"]).sort_values("sta")
    v = interp_linear(g["sta"].tolist(), g["line_dist_km"].tolist(), float(sta),
                      extrapolate=True)
    return None if v is None else round(v, 3)


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
    anm = pd.read_parquet(DATA / "_anomaly.parquet")
    bas = pd.read_parquet(DATA / "_baseline.parquet").iloc[0]
    sta = pd.read_parquet(DATA / "_stations.parquet")
    cru = pd.read_parquet(DATA / "_cruises.parquet")
    var = pd.read_parquet(DATA / "_variables.parquet")
    bathy = pd.read_csv("metadata/station_bathymetry.csv")

    sec["line_s"] = sec["line"].map(fmt_line)
    anm["line_s"] = anm["line"].map(fmt_line)
    sta["line_s"] = sta["line"].map(fmt_line)

    # anomaly indexed the same way the section grids are built, so the two
    # matrices are filled in one pass per (line, cruise)
    anm_by_sec = {k: v for k, v in anm.groupby(["line_s", "cruise_key"], sort=False)}

    # the seafloor sampled every 500 m along each line, keyed by distance from the
    # line's most-inshore grid station
    floor = pd.read_csv("metadata/line_bathymetry.csv")
    floor["line_s"] = floor["line"].map(fmt_line)
    floor_by_line = {k: v.sort_values("line_dist_km")
                     for k, v in floor.groupby("line_s")}

    # each station's place on its SECTION line's ruler, and the seafloor there,
    # from its own station number (station_ruler) — never from the grid cell it
    # falls in, whose line may not be the section's (93.4 26.4 on line 93.3)
    bathy["line_s"] = bathy["line"].map(fmt_line)
    grid_by_line = {k: v for k, v in bathy.groupby("line_s")}
    sta["line_dist_km"] = pd.to_numeric(pd.Series([
        station_ruler(grid_by_line[ls], s) if ls in grid_by_line else None
        for ls, s in zip(sta["line_s"], sta["sta"])], index=sta.index, dtype="object"))

    # the depth under the station is read off the same profile the app draws (so
    # the hover and the silhouette agree); where the profile does not reach, the
    # GEBCO depth at the station's own grid cell centre
    cell_bathy = dict(zip(bathy["grid_key"], bathy["bathy_m"]))
    floor_knots = {k: (v.dropna(subset=["bathy_m"])["line_dist_km"].tolist(),
                       v.dropna(subset=["bathy_m"])["bathy_m"].tolist())
                   for k, v in floor_by_line.items()}

    def seafloor(ls, d, grid_key):
        v = None
        if ls in floor_knots and pd.notna(d):
            xs, ys = floor_knots[ls]
            if xs:
                # the profile and the ruler are rounded separately (2 vs 3 dp)
                d = min(max(float(d), xs[0]), xs[-1]) if xs[0] - 0.01 <= d <= xs[-1] + 0.01 else d
            v = interp_linear(xs, ys, float(d))
        if v is None:
            v = cell_bathy.get(grid_key)
        return None if v is None or pd.isna(v) else round(float(v), 1)

    sta["bathy_m"] = [seafloor(ls, d, gk) for ls, d, gk
                      in zip(sta["line_s"], sta["line_dist_km"], sta["grid_key"])]

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
        fb = FALLBACK_META.get(v, {})
        variables.append({
            "var": v,
            "label": (SHORT_LABEL.get(v) or m.get("description")
                      or fb.get("description") or v),
            "units": (m.get("units") or fb.get("units") or ""),
            "prefer": prefer,
            "uncorrected": prefer is not None,
        })

    SECTIONS.mkdir(parents=True, exist_ok=True)
    for old in SECTIONS.glob("*.json"):
        old.unlink()

    depth_ix = {d: i for i, d in enumerate(DEPTHS)}
    index_lines = defaultdict(dict)
    n_written = 0
    n_skipped = {"stations": 0, "variables": 0}

    for (line_s, cruise_key), g in sec.groupby(["line_s", "cruise_key"], sort=False):
        st = (sta[(sta["line_s"] == line_s) & (sta["cruise_key"] == cruise_key)]
              .sort_values("sta"))          # ascending station = nearshore -> offshore
        # a section is a transect: fewer than MIN_STATIONS stations is a pair of
        # profiles, not a section (Rasmus, 2026-09-23) — not emitted, not indexed
        if len(st) < MIN_STATIONS:
            n_skipped["stations"] += 1
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
                # the shared full-line ruler; None where the bathymetry build has
                # no entry for this station, in which case the app falls back to
                # the occupied ruler rather than drawing a broken axis
                "line_dist_km": (None if pd.isna(r["line_dist_km"])
                                 else round(float(r["line_dist_km"]), 2)),
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

        # z[depth][station] for the anomaly, same shape and same indices, so the
        # app swaps one matrix for the other without touching x, y or stations
        #
        # anoms_n carries the same shape again, but with the climatology's
        # n_cruises for that cell instead of the anomaly value — how many
        # cruises the baseline this departure is measured against was built
        # from. It rides along so the app can show it in the hover tooltip
        # without a second lookup; every anomaly cell has one (same join),
        # so the two matrices are never out of step.
        anoms = {}
        anoms_n = {}
        ga = anm_by_sec.get((line_s, cruise_key))
        if ga is not None:
            for v, gv in ga.groupby("var", sort=False):
                za = [[None] * n_sta for _ in DEPTHS]
                zn = [[None] * n_sta for _ in DEPTHS]
                for s_, d, val, n in zip(gv["sta"], gv["depth_m"], gv["anomaly"],
                                          gv["n_cruises"]):
                    si = sta_ix.get(float(s_))
                    di = depth_ix.get(int(d))
                    if si is not None and di is not None:
                        za[di][si] = None if pd.isna(val) else float(val)
                        zn[di][si] = None if pd.isna(n) else int(n)
                anoms[v] = za
                anoms_n[v] = zn

        knots_along = [x for x in st["line_dist_km"].tolist()]
        prof = None
        if not any(pd.isna(k) for k in knots_along):
            prof = floor_profile(floor_by_line.get(line_s), knots_along, dist)

        stage = st["data_stage"].dropna()
        grids, anoms, anoms_n = stage_filter(stage.iloc[0] if len(stage) else None,
                                             grids, anoms, anoms_n)
        # per-variable display rules, applied to the measured matrices and carried
        # through to the anomaly ones; `withheld` says what went and why, so the
        # app can tell the reader rather than silently omit it
        grids, anoms, anoms_n, withheld = apply_variable_rules(grids, anoms, anoms_n)
        if not section_is_drawable(n_sta, grids):
            n_skipped["variables"] += 1
            continue
        shard = {
            "line": line_s,
            "cruise_key": cruise_key,
            "data_stage": (stage.iloc[0] if len(stage) else None),
            "stations": stations,
            "depths": DEPTHS,
            "vars": grids,
            "anom": anoms,
            "anom_n": anoms_n,
            "floor": prof,
            "withheld": withheld,
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
            "anom_vars": sorted(anoms.keys()),
            "withheld": withheld,
            # unavailable *_sta_corr -> its *_cruise_corr sibling, where drawn;
            # the app's fallback when the selected variable is not on offer here
            "fallback": cruise_fallbacks(
                {x["var"] for x in variables} | set(withheld), grids.keys()),
            "file": f"sections/{fn}",
        }

    # cruises newest first. cruise_key is YYYY-MM-NODC, so a plain reverse string
    # sort IS year-month descending — no date parsing, and it stays correct for
    # the 1993 cruises the Wilkinson archive adds.
    # Full extent of each line's ruler, so the comparable x-axis is fixed for the
    # line rather than derived from whichever cruise is on screen — the axis has to
    # stay put when you change cruise or the comparison is not one.
    #
    # The extent is the farthest station any section on the line reaches, not the
    # end of the seafloor profile. The profile runs to the line's last GRID cell,
    # which on lines 60-90 is a historical cell out at station 200 that no CTD
    # cast has reached: once the bathymetry crop covered those cells (re-run for
    # the v2026.10.04 grid) line 90's axis would have been 1,274 km for sections
    # that end at 683 km (1,026 km before, already a third empty). The floor is
    # trimmed to the same range.
    sec_reach = sta.groupby("line_s")["line_dist_km"].agg(["min", "max"])
    line_extent = {}
    line_floor = {}
    for line_s, g in floor.groupby("line_s"):
        if line_s in sec_reach.index and pd.notna(sec_reach.loc[line_s, "max"]):
            lo = min(0.0, float(sec_reach.loc[line_s, "min"]))
            hi = float(sec_reach.loc[line_s, "max"])
            g = g[(g["line_dist_km"] >= lo - 0.5) & (g["line_dist_km"] <= hi + 0.5)]
        line_extent[line_s] = round(float(g["line_dist_km"].max()), 2)
        # The seafloor in the line's OWN coordinate, carried once per line rather
        # than per shard. Each shard's `floor` is this profile warped onto that
        # cruise's occupied x; under the comparable ruler the axis IS along-line
        # distance, so the warped copy would place the bathymetry in the wrong
        # place — visibly, as a shelf break tens of km from where it is.
        g2 = g.dropna(subset=["bathy_m"]).sort_values("line_dist_km")
        line_floor[line_s] = {
            "dist_km": [round(float(v), 2) for v in g2["line_dist_km"]],
            "bathy_m": [float(v) for v in g2["bathy_m"]],
        }

    lines = []
    for line_s, cruises in index_lines.items():
        lines.append({
            "line": line_s,
            "core": line_s in CORE_LINES,
            "extent_km": line_extent.get(line_s),
            "floor": line_floor.get(line_s),
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
        # stated in the app's methods panel; an anomaly whose baseline is not on
        # screen is not interpretable
        "baseline": {
            "yr_min":      int(bas["yr_min"]),
            "yr_max":      int(bas["yr_max"]),
            "min_cruises": int(bas["min_cruises"]),
            "n_cells":   int(bas["n_cells"]),
            "n_cruises": int(bas["n_cruises"]),
        },
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
    print(f"skipped  : {n_skipped['stations']} sections with < {MIN_STATIONS} stations, "
          f"{n_skipped['variables']} with no drawable variable")
    print(f"index    : {len(lines)} lines, {len(variables)} variables")
    print(f"stations : {len(stations_json)} grid stations")
    print(f"baseline : {int(bas['yr_min'])}-{int(bas['yr_max'])}, "
          f"{int(bas['n_cells']):,} cells from {int(bas['n_cruises'])} cruises "
          f"(min_cruises={int(bas['min_cruises'])})")


if __name__ == "__main__":
    main()
