"""display_rules.py — which variables and sections are worth drawing at all.

Applied by scripts/build_sections.py so the published JSON carries the rule, not
only the browser. Stdlib only, so scripts/test_display_rules.py runs anywhere.

WHY (Rasmus Swalethorp, 2026-09-29 screenshots of line 93.3, cruise 2025-04-3322)
    (a) "DO average station-corrected" had values at 3 of 15 stations — the
        provider's bottle merge covered only those casts, and the station
        correction needs a ~500 m cast with ~10+ bottles — and the heatmap
        filled between them as one block.
    (b) "Est. nitrate cruise-corrected" was constant with depth at every station
        (the provider's EstNO3_CruiseCorr holds ONE value per cast over 0-520 m on
        cruises 2504SH, 2204SH, 2307SR and 9809NH; it varies properly on 0903JD),
        so it drew as vertical stripes — 0-4 umol/L to 500 m, which is not nitrate.
    And, 2026-09-23: "I suggest we do not offer up a transect plot if the transect
    has less than 3 stations on it."

THE RULES (all per section; a "section" is one line x one cruise)
    1. sparse    — a variable with data at fewer than MIN_STATIONS stations, OR at
                   fewer than MIN_STATION_FRACTION of the section's stations, is
                   withheld (reason "data at N of M stations"). The fraction is
                   what catches 2025-04-3322's DO: 3 of 15 stations clears "at
                   least 3" and still fills one block across the section.
    2. constant  — a variable that is depth-invariant at EVERY station that has it
                   over >= MIN_INVARIANT_BINS depth bins (50 m of 10 m bins) is
                   withheld: a profile that does not change with depth over 50 m+
                   at every station is a per-cast scalar smeared down the column,
                   not a measured profile. Stations with fewer bins are too short
                   to judge and neither condemn nor save the variable.
    3. sections  — a section with fewer than MIN_STATIONS stations, or with no
                   variable left after 1-2, is not emitted at all.
    4. fallback  — where a `*_sta_corr` series is withheld (or was never there)
                   and its `*_cruise_corr` sibling is drawn, the sibling is what the
                   selector falls back to (see cruise_fallbacks; the index carries
                   it per cruise as `fallback`).

Matrices are the shard's `z[depth][station]` lists, None = no value.
"""

import math

MIN_STATIONS = 3          # stations a section / variable needs ("less than 3" is out)
MIN_STATION_FRACTION = 0.5  # ... and of this share of the section's stations
MIN_INVARIANT_BINS = 6    # depth bins (10 m each = 50 m) before "constant" is judged
CONSTANT_TOL = 1e-9       # std (or range) below this = identical

STA_SUFFIX = "_sta_corr"
CRUISE_SUFFIX = "_cruise_corr"


def sparse_reason(n_with, n_total):
    """The shard's reason text for a sparse variable: 'data at 3 of 15 stations'."""
    return f"data at {n_with} of {n_total} stations"


def is_sparse(n_with, n_total, min_stations=MIN_STATIONS,
              min_fraction=MIN_STATION_FRACTION):
    """Fewer than min_stations stations, or fewer than min_fraction of n_total."""
    return n_with < min_stations or n_with < min_fraction * n_total


WITHHELD_CONSTANT_REASON = "constant with depth at every station (source column suspect)"


def _finite(v):
    return v is not None and not (isinstance(v, float) and (math.isnan(v) or math.isinf(v)))


def station_profiles(z):
    """Per-station list of the finite values down the column (depth order)."""
    n_sta = len(z[0]) if z else 0
    return [[row[j] for row in z if _finite(row[j])] for j in range(n_sta)]


def n_stations_with_data(z):
    return sum(1 for p in station_profiles(z) if p)


def _is_flat(vals, tol=CONSTANT_TOL):
    return max(vals) - min(vals) < tol


def is_depth_invariant(z, min_bins=MIN_INVARIANT_BINS, tol=CONSTANT_TOL):
    """True when every station with >= min_bins values has them all identical.

    Needs at least one such station: a variable with only short profiles has not
    been shown to be constant over 50 m, so it is not condemned here.
    """
    judged = [p for p in station_profiles(z) if len(p) >= min_bins]
    return bool(judged) and all(_is_flat(p, tol) for p in judged)


def withhold_reason(z, min_stations=MIN_STATIONS):
    """Why this variable should not be drawn for the section, or None to draw it."""
    n_with, n_total = n_stations_with_data(z), (len(z[0]) if z else 0)
    if is_sparse(n_with, n_total, min_stations):
        return sparse_reason(n_with, n_total)
    if is_depth_invariant(z):
        return WITHHELD_CONSTANT_REASON
    return None


def apply_variable_rules(grids, *others, min_stations=MIN_STATIONS):
    """Drop withheld variables from `grids` (and the same keys from `others`).

    grids  : {var: z} measured matrices — the rules are judged on these.
    others : any number of {var: z} dicts that ride with it (anomaly, anomaly n).
    Returns (grids, *others, withheld) with withheld = {var: reason}.
    """
    withheld = {}
    for v, z in grids.items():
        why = withhold_reason(z, min_stations)
        if why:
            withheld[v] = why
    keep = lambda d: {v: z for v, z in d.items() if v not in withheld}   # noqa: E731
    return (keep(grids), *[keep(o) for o in others], withheld)


def section_is_drawable(n_stations, grids, min_stations=MIN_STATIONS):
    """A section needs >= min_stations stations and at least one variable."""
    return n_stations >= min_stations and bool(grids)


def sibling_var(var):
    """`x_sta_corr` -> `x_cruise_corr`; None for anything else."""
    if var.endswith(STA_SUFFIX):
        return var[: -len(STA_SUFFIX)] + CRUISE_SUFFIX
    return None


def cruise_fallbacks(candidates, available):
    """{unavailable *_sta_corr var: its *_cruise_corr sibling} where the sibling is drawn.

    candidates : variables the reader might ask for that this section does not
                 offer — the ones withheld here plus any the cruise never had.
    available  : the variables the section does offer.

    The app consults this when the selected variable is not on offer, so changing
    cruise (or following a link) lands on the sibling rather than the first
    variable in the list. Generic in the variable name: a new `*_sta_corr` /
    `*_cruise_corr` pair needs no change here.
    """
    avail = set(available)
    out = {}
    for v in candidates:
        sib = sibling_var(v)
        if sib and sib in avail and v not in avail:
            out[v] = sib
    return out
