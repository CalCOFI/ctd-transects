/* CalCOFI CTD Transects
 *
 * Draws one (line, cruise, variable) cross-shelf section at a time from a
 * prebuilt station x depth matrix. There is no interpolation code here: the
 * matrix goes straight to a Plotly heatmap with zsmooth:"best", which is what
 * gives the smooth ODV-style field that apps/ctd-viz gets from MBA::mba.surf()
 * — a multilevel B-spline with no JavaScript port.
 *
 * State lives in the URL (?line=90&cruise=2026-04-3322&var=temperature_ave) so a
 * section can be linked to, which is the main thing people want from a plot like
 * this.
 */

const $ = (id) => document.getElementById(id);

const state = {
  index: null,
  stations: null,   // full CalCOFI grid, for the map
  shard: null,      // the section currently loaded
  cache: new Map(),
};

/* Cache-busting: GitHub Pages serves public/data/ with max-age=600 and no way to
 * set headers, so a returning visitor can pair a fresh app.js with stale data.
 * version.json carries the release the data was built from; every fetch appends
 * it. Written by refresh.yml, so the query string changes exactly when the data
 * does and nobody has to remember a counter. */
let CACHE_BUST = "";

async function getJSON(path) {
  if (state.cache.has(path)) return state.cache.get(path);
  const res = await fetch(path + CACHE_BUST);
  if (!res.ok) throw new Error(`${path}: ${res.status}`);
  const data = await res.json();
  state.cache.set(path, data);
  return data;
}

/* ── colour ──────────────────────────────────────────────────────────────── */

/* Two ramps, chosen by what the value IS rather than per variable.
 *
 * SEQUENTIAL (one hue, light -> dark) for magnitude: salinity, density, oxygen,
 * fluorescence. All four use the same blue ramp — only one field is on screen at
 * a time, so a second hue would carry no information and just invite the reader
 * to compare colours across variables that share no scale.
 *
 * DIVERGING (two hues + a neutral midpoint) for temperature alone. Strictly,
 * temperature in a section is magnitude too — but blue=cold / red=warm is so
 * deeply held that a one-hue ramp actively misleads: its darkest step would land
 * on the WARM surface water, reading as cold to anyone who glances. The neutral
 * sits at the midpoint of the plotted range, so the section splits into a cool
 * half and a warm half.
 *
 * No rainbow: jet and its relatives invent banding that is not in the data, which
 * is exactly the artefact a reader of a thermocline would misread as structure.
 */
const RAMP_SEQ = [           // blue 100 -> 700
  [0.00, "#cde2fb"], [0.17, "#9ec5f4"], [0.33, "#6da7ec"], [0.50, "#3987e5"],
  [0.67, "#256abf"], [0.83, "#184f95"], [1.00, "#0d366b"],
];

/* blue arm and red arm carry the same number of steps at mirrored lightness, so
 * neither pole dominates. `#7d1b28` is `#e34948` darkened to match `#0d366b`'s
 * position on the blue arm; the midpoints are the documented neutrals. */
const RAMP_DIV_LIGHT = [
  [0.00, "#0d366b"], [0.25, "#3987e5"], [0.50, "#f0efec"],
  [0.75, "#e34948"], [1.00, "#7d1b28"],
];
const RAMP_DIV_DARK = [
  [0.00, "#0d366b"], [0.25, "#3987e5"], [0.50, "#383835"],
  [0.75, "#e34948"], [1.00, "#7d1b28"],
];

const DIVERGING = new Set(["temperature_ave"]);

/* An ANOMALY is polarity, not magnitude — above or below normal — so it always
 * gets the diverging ramp with the neutral pinned to zero, whatever the variable.
 * Pinning matters: left to autoscale, a section that happens to be warm
 * throughout would put the neutral at its own mean and paint the coolest part of
 * a uniformly-warm cruise blue. `zmid` does the pinning; the ramp is the same
 * one temperature uses so the two views read consistently. */

/* the theme is whatever brand/v2/theme.js put on <html> — light unless it says
 * dark (brand v2) — never the OS setting, so the chrome and the plots cannot disagree */
const darkMode = () => document.documentElement.dataset.theme !== "light";

function scaleFor(varName, mode) {
  if (mode === "anomaly" || DIVERGING.has(varName))
    return darkMode() ? RAMP_DIV_DARK : RAMP_DIV_LIGHT;
  return RAMP_SEQ;
}

/* Dark mode is selected, not flipped: the ramps keep their poles and only the
 * surfaces and ink change, so a field looks the same in both. */
const theme = () => darkMode()
  ? { ink: "#e6e9ed", grid: "#3a3f44", panel: "#2c3035",
      contour: "rgba(255,255,255,0.5)", floor: "rgba(150,158,166,0.85)",
      floorLine: "#1b1d20" }
  : { ink: "#212529", grid: "#dee2e6", panel: "#f1f3f5",
      contour: "rgba(255,255,255,0.7)", floor: "rgba(120,128,136,0.9)",
      floorLine: "#333" };

/* ── labels ──────────────────────────────────────────────────────────────── */

/* Every label is distinct, and the pre-2026.08 bare `preliminary` is deliberately
 * absent. That value predates knowing there are two preliminary tiers, so it
 * cannot say whether the bottle merge has run — which is the difference the badge
 * exists to communicate. build_sections.py refuses to build shards carrying it,
 * so it cannot reach this file. */
const STAGE_LABELS = {
  final:                      "Final 1 m-binned",
  preliminary_with_bottle:    "Preliminary — CTD & bottle 1 m-binned",
  preliminary_without_bottle: "Preliminary — CTD only, no bottle merge",
};

/* preliminary_without_bottle offers temperature ONLY (see availableVars below
 * for why) — Rasmus Swalethorp, 2026-09-09 ("Next Two Weeks Tasks"): asked to
 * hold off on showing the raw sensor series at all here, uncorrected salinity
 * or oxygen can look like a real signal when the sensor has drifted or is
 * overdue for calibration, "especially visible with the anomalies". */
const STAGE_NOTE = {
  preliminary_without_bottle:
    "This cruise has not been through the bottle merge yet. Only temperature " +
    "is shown — salinity, oxygen, chlorophyll and nitrate all depend on sensor " +
    "calibration that the merge corrects for, and an uncorrected reading can " +
    "look like a real signal, especially in the anomaly view. Revisit once the " +
    "merge has run.",
  preliminary_with_bottle:
    "Preliminary: the bottle merge has run, but values may still change after " +
    "post-cruise calibration — especially oxygen, nitrate and chlorophyll.",
};

function cruiseLabel(c) {
  const ym = c.cruise_key.slice(0, 7);
  return c.ship ? `${ym} — ${c.ship}` : ym;
}

function varLabel(v) {
  return v.units ? `${v.label} (${v.units})` : v.label;
}

/* ── URL state ───────────────────────────────────────────────────────────── */

function readURL() {
  const p = new URLSearchParams(location.search);
  return {
    line: p.get("line"), cruise: p.get("cruise"), var: p.get("var"),
    mode: p.get("mode"),
    // absent means the default, which is the OCCUPIED ruler: show the data as
    // large as it can be drawn, and let comparison be the thing you opt into
    ruler: p.get("ruler") === "line" ? "line" : "occupied",
  };
}

function writeURL(sel) {
  const p = new URLSearchParams(
    { line: sel.line, cruise: sel.cruise, var: sel.var });
  // the anomaly is the default view; a measured-value link says so explicitly
  p.set("mode", sel.mode === "value" ? "value" : "anomaly");
  if (sel.ruler === "line") p.set("ruler", "line");
  history.replaceState(null, "", `${location.pathname}?${p}`);
}

/* ── selection ───────────────────────────────────────────────────────────── */

function lineByName(name) {
  return state.index.lines.find((l) => l.line === name);
}

// see the STAGE_NOTE comment above for why this is temperature alone, not a
// wider raw-sensor fallback
const PRELIMINARY_WITHOUT_BOTTLE_VARS = new Set(["temperature_ave"]);

/* Which variables can actually be drawn for this cruise, in display order. */
function availableVars(cruise) {
  const have = new Set(cruise.vars);

  if (cruise.data_stage === "preliminary_without_bottle") {
    return state.index.variables.filter(
      (v) => PRELIMINARY_WITHOUT_BOTTLE_VARS.has(v.var) && have.has(v.var));
  }

  /* Past the bottle merge, every corrected series that will ever exist for
   * this cruise already does, so an uncorrected `prefer` entry never actually
   * shows here in practice — it stays as a fallback rule rather than dead
   * code, in case a future stage needs the same "raw only if corrected is
   * missing" behaviour without the temperature-only restriction above. */
  return state.index.variables.filter((v) => {
    if (!have.has(v.var)) return false;
    if (v.prefer && have.has(v.prefer)) return false;
    return true;
  });
}

function fillSelect(el, items, value) {
  el.innerHTML = "";
  for (const it of items) {
    const o = document.createElement("option");
    o.value = it.value;
    o.textContent = it.label;
    if (it.title) o.title = it.title;
    el.appendChild(o);
  }
  if (value != null && items.some((i) => i.value === value)) el.value = value;
}

function syncControls(sel) {
  fillSelect($("sel-line"),
    state.index.lines.map((l) => ({
      value: l.line,
      label: `Line ${l.line}${l.core ? "" : " (partial)"}`,
      title: `${l.n_cruises} cruises`,
    })), sel.line);

  const line = lineByName(sel.line);
  fillSelect($("sel-cruise"),
    line.cruises.map((c) => ({
      value: c.cruise_key,
      label: cruiseLabel(c),
      title: STAGE_LABELS[c.data_stage] || "",
    })), sel.cruise);

  const cruise = line.cruises.find((c) => c.cruise_key === sel.cruise);
  const vars = availableVars(cruise);
  fillSelect($("sel-var"),
    vars.map((v) => ({
      value: v.var,
      label: varLabel(v) + (v.uncorrected ? " — uncorrected" : ""),
    })), sel.var);

  return { line, cruise, vars };
}

/* Keep the current choice across a change of line or cruise wherever it still
 * exists — changing the line should not silently reset you to temperature. */
function resolve(sel) {
  let line = lineByName(sel.line) || state.index.lines[0];
  let cruise = line.cruises.find((c) => c.cruise_key === sel.cruise)
            || line.cruises[0];
  const vars = availableVars(cruise);
  let v = vars.find((x) => x.var === sel.var);
  if (!v) {
    // fall back to the corrected series' uncorrected stand-in, then to the first
    const alt = state.index.variables.find((x) => x.prefer === sel.var);
    v = (alt && vars.find((x) => x.var === alt.var)) || vars[0];
  }
  // mode and ruler are carried through untouched: they are view state, valid for
  // any selection, and dropping them here silently reverted the URL to the
  // defaults on every render
  return { line: line.line, cruise: cruise.cruise_key, var: v ? v.var : null,
           mode: sel.mode === "anomaly" ? "anomaly" : "value",
           ruler: sel.ruler === "line" ? "line" : "occupied" };
}

/* ── the section plot ────────────────────────────────────────────────────── */

// shared with thinStationTicks below, so the pixel budget it measures against
// can never silently drift from the margin the plot actually renders with
const PLOT_MARGIN_L = 58, PLOT_MARGIN_R = 86;

/* Contour levels, computed here rather than left to Plotly's `autocontour`.
 *
 * `autocontour` is one render BEHIND under `Plotly.react`: it keeps the levels it
 * derived for the PREVIOUS z. Every switch that moves the range — variable,
 * value <-> anomaly, and often line or cruise — therefore drew the new field at
 * the old field's levels, and where the two ranges do not overlap that is not a
 * subtle error: temperature (6-16 degC) drawn at the anomaly levels (-2..3.5)
 * yields ZERO lines and ZERO labels, and the anomaly view drawn at 6-16 likewise.
 * The section came up bare and the next unrelated interaction "fixed" it.
 *
 * Deriving the levels from the z actually being drawn removes the state that
 * could go stale. The rule reproduces what `autocontour` picks when it is given
 * the right data — ~15 intervals snapped to a 2/5/10 x 10^n step — so the
 * levels are unchanged in every case that was already working. */
function contourLevels(z) {
  let lo = Infinity, hi = -Infinity;
  for (const row of z) for (const v of row)
    if (v != null && isFinite(v)) { if (v < lo) lo = v; if (v > hi) hi = v; }
  if (!(hi > lo)) return {};      // all-null or flat: nothing to contour

  const rough = (hi - lo) / 15;
  const mag = Math.pow(10, Math.floor(Math.log10(rough)));
  const size = [2, 5, 10].find((m) => rough <= m * mag) * mag;
  // the step lands on values like 0.30000000000000004, which reach the labels
  const snap = (v) => +v.toPrecision(12);
  return { start: snap(Math.ceil(lo / size) * size),
           end:   snap(Math.floor(hi / size) * size), size: snap(size) };
}

/* The header badge's default text — the release-wide cruise count. Shared by
 * init() (first paint) and resetBaselineBadge() below, so the two can't drift
 * into different wording. */
function baselineBadgeDefault(b) {
  // in the anomaly view, the range of baseline sizes behind the section on screen
  // (Rasmus, 2026-09-09: "maybe a min-max range since it will depend on station
  // and depth"); otherwise the release-wide total
  const r = state.sectionN;
  if (r) return r.lo === r.hi ? `${r.lo} cruises this section`
                              : `${r.lo}–${r.hi} cruises this section`;
  return `${b.n_cruises} cruises total`;
}

/* Min and max baseline size over the cells of the section in view, or null
 * when there is no anomaly grid to measure. */
function sectionNRange(nGrid) {
  if (!nGrid) return null;
  let lo = Infinity, hi = -Infinity;
  for (const row of nGrid) for (const n of row) {
    if (n == null) continue;
    if (n < lo) lo = n;
    if (n > hi) hi = n;
  }
  return Number.isFinite(lo) ? { lo, hi } : null;
}

function resetBaselineBadge() {
  const b = state.index.baseline;
  const badge = $("baseline-badge");
  if (!b || !badge) return;
  badge.querySelector("b").textContent = baselineBadgeDefault(b);
  $("baseline-hint").textContent = "";
  badge.classList.remove("thin");
}

/* Swaps the header badge to the hovered cell's own baseline size — same
 * number, same "at the minimum" threshold as the tooltip (see drawSection's
 * hovertemplate below), just also visible to someone glancing at the header
 * rather than reading the tooltip line by line. */
function syncBaselineBadgeTo(n) {
  const b = state.index.baseline;
  const badge = $("baseline-badge");
  if (!b || !badge) return;
  const thin = n <= b.min_cruises;
  badge.querySelector("b").textContent = `${n} cruise${n === 1 ? "" : "s"}`;
  $("baseline-hint").textContent = thin
    ? "— at the release's own minimum" : "— this cell";
  badge.classList.toggle("thin", thin);
}

/* Wired once per graph div — Plotly.react reuses the same element across
 * re-renders, so re-registering on every drawSection call would stack
 * duplicate listeners (mirrors the baseBadge click-handler guard in init()).
 * plotly_hover's customdata[2] is undefined for the station-tick markers
 * trace and for any cell with no baseline (value mode, or an anomaly cell
 * outside the climatology join) — both correctly fall back to the default. */
function wireBaselineBadgeSync(plotEl) {
  if (!plotEl || plotEl.dataset.baselineHoverWired) return;
  plotEl.dataset.baselineHoverWired = "1";
  plotEl.on("plotly_hover", (ev) => {
    const pt = ev.points && ev.points[0];
    const n = pt && pt.customdata ? pt.customdata[2] : null;
    if (n == null) resetBaselineBadge();
    else syncBaselineBadgeTo(n);
  });
  plotEl.on("plotly_unhover", resetBaselineBadge);
}

function drawSection(shard, varName, maxDepth, mode, ruler) {
  const meta = state.index.variables.find((v) => v.var === varName);
  const anom = mode === "anomaly";

  /* Which ruler. `line_dist_km` is each station's distance along the full line;
   * it is null where the bathymetry build has no entry for a station, and a
   * partly-null axis is worse than the wrong one, so fall back wholesale. */
  const haveLine = shard.stations.every((s) => s.line_dist_km != null);
  const useLine = ruler === "line" && haveLine;
  const x = shard.stations.map((s) => (useLine ? s.line_dist_km : s.dist_km));

  const keep = shard.depths.map((d, i) => [d, i]).filter(([d]) => d <= maxDepth);
  const y = keep.map(([d]) => d);
  const grid = anom ? (shard.anom || {})[varName] : shard.vars[varName];
  const z = grid ? keep.map(([, i]) => grid[i]) : keep.map(() => x.map(() => null));
  // per-cell baseline support, anomaly view only — same shape as `grid`, so the
  // same `keep`-filtered depth index lines it up with `z` row for row
  const nGrid = anom ? (shard.anom_n || {})[varName] : null;
  const baseline = state.index.baseline;
  state.sectionN = sectionNRange(nGrid ? keep.map(([, i]) => nGrid[i]) : null);
  resetBaselineBadge();

  const t = theme();
  const units = meta.units || "";
  const zlabel = anom ? `${meta.label} anomaly` : meta.label;

  const traces = [{
    type: "heatmap",
    x, y, z,
    // zsmooth is what replaces an interpolation step: the renderer resamples the
    // station x depth matrix into the smooth field an ODV-style section wants
    zsmooth: "best",
    colorscale: scaleFor(varName, mode),
    // zero is NORMAL, and it must sit on the neutral wherever the data lands —
    // otherwise a uniformly warm cruise paints its least-warm part blue
    ...(anom ? { zmid: 0 } : {}),
    /* connectgaps bridges a station that missed a depth bin, and it must stay ON
     * in both views.
     *
     * The tempting reading — "a gap in the anomaly means no baseline, so show
     * it" — is wrong twice. First, `obs` carries the THINNED CTD series (a 10 m
     * grid plus RDP inflection points plus bottle depths), so most holes in the
     * matrix are depths this cast simply has no scan at, in the value view
     * exactly as much as in the anomaly view; leaving them open reads as missing
     * baseline when it is missing sampling. Second, it is pathological: the
     * contour trace below tracing a matrix that sparse locked the page for tens
     * of seconds and had to be killed.
     *
     * How much of the section actually HAS a baseline is reported as a number in
     * the note under the plot instead, which says it precisely rather than
     * leaving the reader to estimate blank area by eye. */
    connectgaps: true,
    // customdata[0] is the station/distance label, always present.
    // customdata[1] is the baseline line — only in anomaly mode, where a
    // departure is only as trustworthy as the climatology it's measured
    // against. It's the same n_cruises Ben asked to see per cell, added
    // right where a reader is already looking rather than as a second
    // number to cross-reference. Below the release's own required minimum
    // per cell it says so plainly instead of just printing a small number.
    // customdata[2] is that same n_cruises as a bare number (or omitted) —
    // wireBaselineBadge()'s hover handler reads it to keep the header badge
    // in sync with whatever cell the pointer is over, without re-deriving or
    // re-formatting what's already computed right here.
    hovertemplate:
      "%{customdata[0]}<br>Depth: %{y} m<br>" +
      `${zlabel}: %{z}${units ? " " + units : ""}` +
      "%{customdata[1]}<extra></extra>",
    customdata: z.map((row, di) => {
      const nRow = nGrid ? nGrid[keep[di][1]] : null;
      return row.map((_, j) => {
        const label = `Station ${shard.stations[j].sta} · ${x[j].toFixed(0)} km`;
        const n = nRow ? nRow[j] : null;
        if (n == null || !baseline) return [label, ""];
        const thin = n <= baseline.min_cruises;
        const line = thin
          ? `<br>baseline: ${n} cruise${n === 1 ? "" : "s"} — at the release's own minimum`
          : `<br>baseline: ${n} cruises (${baseline.yr_min}–${baseline.yr_max})`;
        return [label, line, n];
      });
    }),
    colorbar: {
      title: { text: anom ? (units ? "\u0394 " + units : "\u0394") : units,
               side: "right" },
      thickness: 12, outlinewidth: 0, tickfont: { color: t.ink, size: 11 },
      titlefont: { color: t.ink, size: 11 },
    },
  }, {
    // contour lines over the same field, labelled. The numbers are what make a
    // section readable rather than merely pretty — and they are also the
    // secondary encoding that keeps the plot usable without colour.
    type: "contour",
    x, y, z,
    /* Same reason as the heatmap above — and it must be stated again here,
     * because contour DEFAULTS it to false and the two traces draw the same z.
     *
     * A contour trace with gaps does not merely skip them: it clips the whole
     * trace group to a mask of the cells that have data. That mask cuts LINES
     * AND LABELS, mid-glyph — the labels came out sliced in half horizontally,
     * and every isotherm was shredded into dozens of stubs that read as noise
     * in the near-uniform deep water. It looks like the contours are being
     * painted over by something on top of them; nothing is. The holes are the
     * thinned series' unsampled depths, exactly as for the heatmap, so bridge
     * them the same way and the two traces describe the same field. */
    connectgaps: true,
    contours: { ...contourLevels(z), coloring: "none", showlabels: true,
                labelfont: { size: 10, color: t.ink } },
    line: { color: t.contour, width: 1 },
    showscale: false,       // without this the contour adds a SECOND colorbar
    showlegend: false,
    hoverinfo: "skip",
  }];

  /* Seafloor silhouette, clipped to the plotted depth range.
   *
   * `floor` is GEBCO sampled every 500 m ALONG the line, not one sounding per
   * station. Station-only sampling drew the Channel Islands bank on line 86.7 as
   * a single triangle 74 km wide and 1.5 km tall, because its neighbours are
   * 37 km away in deep water — terrain that does not exist, sitting right where
   * the thermocline is read.
   *
   * 2 km was not enough either: GEBCO's own cell is ~390 m, so line 93.3's banks
   * came through as three spikes. Fortymile Bank is a ~14 km rise to a 178 m
   * crest that 2 km sampling reduced to four soundings. */
  /* Under the comparable ruler the x-axis IS along-line distance, so the floor
   * must come from the line's own profile (carried once in index.json) rather
   * than from the copy warped onto this cruise's occupied stations — that copy
   * would put the shelf break tens of km from where it is. */
  const fl = useLine ? (lineByName(shard.line) || {}).floor : shard.floor;
  if (fl && fl.dist_km.length > 1) {
    const n = fl.dist_km.length;
    traces.push({
      type: "scatter",
      mode: "lines",
      x: fl.dist_km.concat([fl.dist_km[n - 1], fl.dist_km[0]]),
      y: fl.bathy_m.map((d) => Math.min(d, maxDepth)).concat([maxDepth, maxDepth]),
      fill: "toself",
      fillcolor: t.floor,
      line: { color: t.floorLine, width: 1 },
      hoverinfo: "skip",
      showlegend: false,
    });
  }

  /* Station tick marks along the top, drawn on the SECOND x-axis so that axis's
   * tick labels are the station numbers (an axis with no trace on it does not
   * render). The two rulers are the same ruler: `+proj=calcofi` is equidistant
   * along a line at 7.386 km = 3.988 nmi per station unit (constant to 0.04 %
   * over line 90's 665 km), so station number is a LINEAR rescaling of distance
   * and both can label one axis with nothing distorted. */
  traces.push({
    type: "scatter",
    mode: "markers",
    xaxis: "x2",
    x, y: x.map(() => 0),
    marker: { symbol: "triangle-down", size: 9, color: t.ink },
    hovertemplate: shard.stations.map(
      (s) => `Station ${s.sta}<br>${s.dist_km.toFixed(0)} km offshore` +
             (s.bathy_m != null ? `<br>Seafloor ${s.bathy_m.toFixed(0)} m` : "") +
             "<extra></extra>"),
    showlegend: false,
  });

  // shared with thinStationTicks below: the axis range it thins against must
  // be the SAME range the axis actually renders with, not recomputed
  const xRange = useLine
    ? [lineExtent(shard.line) ?? Math.max(...x), 0]
    : [Math.max(...x), Math.min(...x)];
  const ticks = thinStationTicks(x, shard.stations, xRange);

  const layout = {
    // r leaves room for the colorbar; without it the bar renders outside the
    // panel and lands on top of the map
    // t carries the plot title AND the station axis above the panel; pinning the
    // title to the container top keeps the two off each other
    margin: { l: PLOT_MARGIN_L, r: PLOT_MARGIN_R, t: 92, b: 52 },
    title: {
      text: `Line ${shard.line} · ${shard.cruise_key.slice(0, 7)} · ${zlabel}`,
      font: { size: 15, color: t.ink },
      yref: "container", y: 0.985, yanchor: "top",
    },
    font: { color: t.ink },
    xaxis: {
      title: {
        text: useLine ? "Distance along line (km)" : "Distance offshore (km)",
        font: { size: 12 },
      },
      zeroline: false, gridcolor: t.grid,
      /* OFFSHORE ON THE LEFT, the coast on the right — the section is laid out
       * the way the map beside it is, so a feature read off one is found in the
       * same place on the other. A CalCOFI line runs west-south-west from the
       * coast, so distance offshore DESCENDS left to right. */
      // On the shared ruler the range is the LINE's full extent, not this
      // cruise's — an axis that resizes with the cruise is not a comparison.
      range: xRange,
    },
    /* The station numbers, on top — the classic hydrographic-section convention,
     * and the labels for the tick marks that were already there. Same ruler, so
     * `matches` keeps it locked to the km axis through any zoom. */
    xaxis2: {
      title: { text: "Station", font: { size: 12 } },
      overlaying: "x", side: "top", matches: "x",
      showgrid: false, zeroline: false,
      tickmode: "array",
      tickvals: ticks.tickvals,
      ticktext: ticks.ticktext,
      tickfont: { size: 11, color: t.ink },
    },
    yaxis: {
      title: { text: "Depth (m)", font: { size: 12 } },
      autorange: "reversed", range: [maxDepth, 0], gridcolor: t.grid,
      // depth 0 IS the y-origin, so Plotly's zeroline draws a dark rule right
      // along the sea surface. It is invisible under a full-width section and
      // very visible under the comparable ruler, where it runs on alone past the
      // last station the cruise occupied and reads as data.
      zeroline: false,
    },
    plot_bgcolor: t.panel,
    paper_bgcolor: "rgba(0,0,0,0)",
    hovermode: "closest",
  };

  Plotly.react($("plot"), traces, layout,
    { responsive: true, displaylogo: false });
  wireBaselineBadgeSync($("plot"));
  // a fresh section means whatever cell the badge was synced to no longer
  // exists on screen — start each render back at the release-wide total
  resetBaselineBadge();
}

function lineExtent(name) {
  const l = lineByName(name);
  return l && l.extent_km != null ? l.extent_km : null;
}

/* Station tick labels are placed by km position, not by index, so wherever
 * stations bunch up — nearly always near the coast, where CalCOFI's station
 * spacing tightens — their labels collide. Plotly's own auto-rotation cannot
 * save it: two short labels ("30", "26.7") can still merge edge-to-edge.
 * Skip a label instead of drawing it if it would land too close, in PIXELS, to
 * the last label actually kept. Pixels, not km: the same cruise should thin
 * differently in a narrow window than a wide one, because what overlaps is a
 * rendered-width fact, not a distance fact. The triangle markers below (the
 * OTHER trace on this axis) still mark every occupied station either way —
 * only the printed numbers thin. */
function thinStationTicks(x, stations, axisRange) {
  const el = $("plot");
  const innerPx = Math.max(
    (el && el.offsetWidth ? el.offsetWidth : 900) - PLOT_MARGIN_L - PLOT_MARGIN_R, 100);
  const span = Math.abs(axisRange[0] - axisRange[1]) || 1;
  const pxPerUnit = innerPx / span;
  const MIN_GAP_PX = 26;   // roughly one rotated "###.#"-width label
  let lastKeptX = null;
  const tickvals = [], ticktext = [];
  stations.forEach((s, i) => {
    const gapPx = lastKeptX == null ? Infinity : Math.abs(x[i] - lastKeptX) * pxPerUnit;
    if (gapPx >= MIN_GAP_PX) {
      tickvals.push(x[i]);
      ticktext.push(String(s.sta));
      lastKeptX = x[i];
    }
  });
  return { tickvals, ticktext };
}

/* ── the map ─────────────────────────────────────────────────────────────── */

/* Plotly's built-in geo layer draws Natural Earth coastlines locally, so the map
 * needs no tile server and no second mapping library. */
/* Three classes, because two conflate the question a reader actually has.
 *
 * A station missing from the section is missing for one of two entirely
 * different reasons: it is on ANOTHER line (irrelevant), or it is on THIS line
 * and this cruise did not reach it (a coverage gap, and the reason the section
 * stops where it does). Drawn identically, the shortened line 93.3 transects
 * since 2025-01 look like the line simply ends at station 90. */
function drawMap(shard) {
  const dark = darkMode();
  const on = new Set(shard.stations.map((s) => s.grid_key));
  const onLine = state.stations.filter(
    (s) => s.line === shard.line && !on.has(s.grid_key));
  const off = state.stations.filter(
    (s) => s.line !== shard.line && !on.has(s.grid_key));

  const traces = [{
    type: "scattergeo",
    lon: off.map((s) => s.lon), lat: off.map((s) => s.lat),
    mode: "markers",
    marker: { size: 3, color: dark ? "rgba(150,160,172,0.5)"
                                   : "rgba(110,122,134,0.55)" },
    hoverinfo: "skip", showlegend: false,
  }, {
    // on this line but not occupied: hollow, in the transect's own hue, so it
    // reads as "part of this transect, absent" rather than as another line
    type: "scattergeo",
    lon: onLine.map((s) => s.lon), lat: onLine.map((s) => s.lat),
    mode: "markers",
    marker: { size: 7, color: "rgba(0,0,0,0)",
              line: { color: "#e34948", width: 1.5 } },
    text: onLine.map((s) => `Station ${s.sta} · not occupied on this cruise`),
    hovertemplate: "%{text}<extra></extra>",
    showlegend: false,
  }, {
    // the transect, drawn in the warm pole of the diverging ramp so it reads as
    // "the selected thing" against the grey grid without introducing a new hue
    type: "scattergeo",
    lon: shard.stations.map((s) => s.lon),
    lat: shard.stations.map((s) => s.lat),
    mode: "lines+markers",
    line: { color: "#e34948", width: 2 },
    marker: { size: 7, color: "#e34948" },
    text: shard.stations.map(
      (s) => `Station ${s.sta} · ${s.dist_km.toFixed(0)} km offshore`),
    hovertemplate: "%{text}<extra></extra>",
    showlegend: false,
  }];

  const nMissed = onLine.length;
  $("map-note").textContent = nMissed
    ? `This cruise occupied ${shard.stations.length} of the ` +
      `${shard.stations.length + nMissed} stations on line ${shard.line}.`
    : `This cruise occupied every station on line ${shard.line}.`;

  Plotly.react($("map"), traces, {
    margin: { l: 0, r: 0, t: 0, b: 0 },
    geo: {
      scope: "north america",
      resolution: 50,
      lonaxis: { range: [-127, -115.5] },
      lataxis: { range: [28.5, 39] },
      showland: true,  landcolor:  dark ? "#222c36" : "#eceff1",
      showocean: true, oceancolor: dark ? "#101a24" : "#dbe7f0",
      showcoastlines: true, coastlinecolor: dark ? "#4a5b6b" : "#90a4ae",
      showframe: false,
      bgcolor: "rgba(0,0,0,0)",
    },
    paper_bgcolor: "rgba(0,0,0,0)",
  }, { responsive: true, displayModeBar: false });
}

/* ── render ──────────────────────────────────────────────────────────────── */

async function render(sel) {
  sel = resolve(sel);
  const { cruise } = syncControls(sel);

  const shard = await getJSON("data/" + cruise.file);
  state.shard = shard;

  const stage = shard.data_stage;
  const badge = $("stage-badge");
  badge.textContent = STAGE_LABELS[stage] || stage || "";
  badge.hidden = !stage;
  badge.className = "badge " + (stage === "final" ? "badge-final" : "badge-prelim");

  const note = $("plot-note");
  if (STAGE_NOTE[stage]) { note.textContent = STAGE_NOTE[stage]; note.hidden = false; }
  else note.hidden = true;

  // Offer the anomaly view only where a baseline exists for this variable. A
  // mode picker that silently draws an empty panel is worse than one that says
  // the anomaly is unavailable.
  const zv = shard.vars[sel.var];
  const za = shard.anom && shard.anom[sel.var];
  let nVal = 0, nAnom = 0;
  if (zv) {
    for (let i = 0; i < zv.length; i++)
      for (let j = 0; j < zv[i].length; j++)
        if (zv[i][j] != null) {
          nVal++;
          if (za && za[i] && za[i][j] != null) nAnom++;
        }
  }
  const hasAnom = nAnom > 0;
  const pctAnom = nVal ? Math.round((100 * nAnom) / nVal) : 0;
  const modeEl = $("sel-mode");
  modeEl.options[1].disabled = !hasAnom;
  const mode = hasAnom && sel.mode === "anomaly" ? "anomaly" : "value";
  modeEl.value = mode;

  const anomNote = $("anom-note");
  if (mode === "anomaly") {
    const b = state.index.baseline;
    anomNote.textContent =
      `Departure from the ${b.yr_min}–${b.yr_max} mean for this station, depth ` +
      `and calendar month — the release's own climatology table (${b.n_cruises} ` +
      `cruises, at least ${b.min_cruises} cruises per cell). ${pctAnom}% of this section's measurements have ` +
      `such a baseline; the rest are drawn from neighbouring values and should ` +
      `not be read closely. See Methods below.`;
    anomNote.hidden = false;
  } else if (!hasAnom && sel.mode === "anomaly") {
    anomNote.textContent =
      "No climatological baseline covers this variable, so the anomaly view is " +
      "unavailable; showing measured values.";
    anomNote.hidden = false;
  } else {
    anomNote.hidden = true;
  }

  // The comparable ruler needs a full-line distance on every station
  const haveLine = shard.stations.every((s) => s.line_dist_km != null);
  $("sel-ruler").disabled = !haveLine;
  $("sel-ruler").checked = haveLine && sel.ruler === "line";
  $("ctl-ruler").title = haveLine ? "" :
    "No along-line distances for this line, so the comparable axis is unavailable.";

  // written now, not before the fetch: `mode` may have been downgraded to
  // "value" above, and a URL promising an anomaly that is not on screen is a
  // link that does not reproduce what the sender saw
  writeURL({ ...sel, mode, ruler: $("sel-ruler").checked ? "line" : "occupied" });

  const maxDepth = Number($("sel-depth").value);
  drawSection(shard, sel.var, maxDepth, mode,
              $("sel-ruler").checked ? "line" : "occupied");
  drawMap(shard);

  /* Plotly measures its container at draw time, and on the FIRST render that is
   * before the map, the notes and the badge have settled the grid — so the plot
   * was laid out a little wider than its panel and the colorbar landed on top of
   * the map until the user happened to resize the window. Re-measure once the
   * browser has finished this frame. */
  requestAnimationFrame(() => {
    for (const id of ["plot", "map"]) {
      const el = $(id);
      if (el && el.offsetWidth) Plotly.Plots.resize(el);
    }
  });
}

function currentSel() {
  return {
    line: $("sel-line").value,
    cruise: $("sel-cruise").value,
    var: $("sel-var").value,
    mode: $("sel-mode").value,
    ruler: $("sel-ruler").checked ? "line" : "occupied",
  };
}

/* `citation` is an optional block on data/version.json (WS-A4, written by
 * scripts/fetch_citation.py's addition to refresh.yml) carrying what the
 * release's own catalog.json/metadata.json say — never authored here:
 *   { release: "…citation string…", release_doi: "10.…" | null,
 *     dataset: "…citation_main…" | null, license: "CC-BY-4.0" | null }
 * A release cut before 2026-09-03 (or a refresh that hasn't picked up the
 * script change yet) has no `citation` key, and every line here stays
 * hidden — never a placeholder. */
function renderCitation(citation) {
  if (!citation) return;
  if (citation.dataset) {
    $("cite-ctd-text").textContent = citation.dataset + (citation.license ? ` (${citation.license})` : "");
    $("cite-ctd").hidden = false;
  }
  if (citation.release) {
    $("cite-release-text").textContent = citation.release;
    $("cite-release").hidden = false;
    const foot = $("cite-foot");
    foot.textContent = "Cite: " + citation.release;
    foot.hidden = false;
  }
}

async function init() {
  try {
    const version = await fetch("data/version.json")
      .then((r) => (r.ok ? r.json() : null)).catch(() => null);
    if (version && version.release) {
      CACHE_BUST = "?v=" + encodeURIComponent(version.release);
      const chip = $("release");
      chip.querySelector("b").textContent = version.release;
      chip.href = "https://calcofi.io/db-schema/#erd?v=" + encodeURIComponent(version.release);
      chip.hidden = false;
      $("release-foot").textContent = version.release;
      renderCitation(version.citation);
    }

    state.index = await getJSON("data/index.json");
    state.stations = await getJSON("data/stations.json");

    const url = readURL();
    const sel = {
      line: url.line || state.index.default.line,
      cruise: url.cruise || state.index.default.cruise_key,
      var: url.var || state.index.default.var,
      // default to the anomaly against climatology — the question the sections
      // are drawn to answer; ?mode=value asks for the measured field
      mode: url.mode || "anomaly",
      ruler: url.ruler,
    };

    const b = state.index.baseline;
    if (b) {
      const baseBadge = $("baseline-badge");
      // "total" matters here: this is the release-wide cruise count behind the
      // whole climatology table, not a per-cell figure — without it "baseline
      // 84 cruises" reads like a single fixed baseline size, which it isn't
      // (any one cell's baseline is built from far fewer — see the hover,
      // which syncs this same badge to whatever cell is under the pointer).
      baseBadge.querySelector("b").textContent = baselineBadgeDefault(b);
      baseBadge.title = `${b.yr_min}–${b.yr_max} climatology baseline, ` +
        `${b.n_cells.toLocaleString()} station × depth × month cells, ` +
        `≥${b.min_cruises} cruises required per cell`;
      baseBadge.hidden = false;
      if (!baseBadge.dataset.wired) {
        baseBadge.dataset.wired = "1";
        baseBadge.addEventListener("click", (e) => {
          const target = document.getElementById("methods-baseline");
          const panel = document.getElementById("methods");
          if (!target || !panel) return;
          e.preventDefault();
          panel.open = true;
          /* One frame so the just-opened <details> has actually reflowed —
           * otherwise target's position is still the COLLAPSED one, and the
           * scroll lands short. The header is measured live, not guessed: a
           * fixed CSS offset drifts the moment brand/v2 resizes its own bar
           * (it did — 6rem of scroll-margin-top still left the heading
           * partly under it), a live measurement never can. */
          requestAnimationFrame(() => {
            const headerH = document.querySelector(".cc-header").getBoundingClientRect().height;
            const y = target.getBoundingClientRect().top + window.scrollY - headerH - 16;
            window.scrollTo({ top: Math.max(y, 0), behavior: "smooth" });
            history.pushState(null, "", "#methods-baseline");
          });
        });
      }

      $("baseline-text").innerHTML =
        `Anomalies on this page are differences from a <strong>${b.yr_min}–` +
        `${b.yr_max}</strong> baseline — the integrated database's own ` +
        `<code>climatology</code> table, built once when the release is cut and ` +
        `shared with the <a href="https://calcofi.io/explore/?lens=section">CalCOFI ` +
        `Explorer</a>, so the two cannot disagree. It holds ${b.n_cruises} cruises ` +
        `in ${b.n_cells.toLocaleString()} station × depth × month cells, each ` +
        `requiring at least ${b.min_cruises} distinct cruises. That window is long ` +
        `enough to average over the 1997–99 El Niño and La Niña, and it ends before ` +
        `the 2014–16 marine heatwave, so the heatwave and everything after it read ` +
        `as departures rather than being folded into the normal. It is not a ` +
        `30-year WMO normal: the 1 m-binned CTD record does not reach back far ` +
        `enough for one.`;
    }

    for (const id of ["sel-line", "sel-cruise", "sel-var", "sel-mode"]) {
      $(id).addEventListener("change", () => render(currentSel()));
    }
    $("sel-ruler").addEventListener("change", () => render(currentSel()));
    $("sel-depth").addEventListener("input", (e) => {
      $("out-depth").textContent = e.target.value;
      if (state.shard) {
        const c = currentSel();
        drawSection(state.shard, c.var, Number(e.target.value), c.mode, c.ruler);
      }
    });

    // the 🌓 toggle (brand/v2/theme.js) announces a change on the document;
    // theme() and scaleFor() are read on every draw, so redrawing is enough
    document.addEventListener("cc:theme", () => {
      if (!state.shard) return;
      const c = currentSel();
      drawSection(state.shard, c.var, Number($("sel-depth").value), c.mode, c.ruler);
      drawMap(state.shard);
    });

    await render(sel);
  } catch (err) {
    $("plot").innerHTML =
      `<p class="error">Could not load the section data: ${err.message}</p>`;
    console.error(err);
  }
}

init();
