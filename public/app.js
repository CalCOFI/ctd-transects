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

const darkMode = () =>
  window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;

function scaleFor(varName) {
  if (!DIVERGING.has(varName)) return RAMP_SEQ;
  return darkMode() ? RAMP_DIV_DARK : RAMP_DIV_LIGHT;
}

/* Dark mode is selected, not flipped: the ramps keep their poles and only the
 * surfaces and ink change, so a field looks the same in both. */
const theme = () => darkMode()
  ? { ink: "#e6edf3", grid: "#22303c", panel: "#1b242e",
      contour: "rgba(255,255,255,0.5)", floor: "rgba(150,158,166,0.85)",
      floorLine: "#0f151b" }
  : { ink: "#14202b", grid: "#dde3e9", panel: "#f2f4f6",
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

const STAGE_NOTE = {
  preliminary_without_bottle:
    "This cruise has not been through the bottle merge yet, so the " +
    "bottle-corrected salinity, oxygen and chlorophyll do not exist for it. " +
    "The uncorrected sensor series are shown instead where available.",
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
  return { line: p.get("line"), cruise: p.get("cruise"), var: p.get("var") };
}

function writeURL(sel) {
  const p = new URLSearchParams(
    { line: sel.line, cruise: sel.cruise, var: sel.var });
  history.replaceState(null, "", `${location.pathname}?${p}`);
}

/* ── selection ───────────────────────────────────────────────────────────── */

function lineByName(name) {
  return state.index.lines.find((l) => l.line === name);
}

/* Which variables can actually be drawn for this cruise, in display order.
 *
 * An uncorrected series is offered ONLY when the corrected one it stands in for
 * is absent — otherwise every picker would carry two near-identical salinities
 * and the user would have to know which to trust. On a sensor-only cruise the
 * corrected series is genuinely missing, and the raw one is the only salinity
 * there is. */
function availableVars(cruise) {
  const have = new Set(cruise.vars);
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
  return { line: line.line, cruise: cruise.cruise_key, var: v ? v.var : null };
}

/* ── the section plot ────────────────────────────────────────────────────── */

function drawSection(shard, varName, maxDepth) {
  const meta = state.index.variables.find((v) => v.var === varName);
  const x = shard.stations.map((s) => s.dist_km);
  const keep = shard.depths.map((d, i) => [d, i]).filter(([d]) => d <= maxDepth);
  const y = keep.map(([d]) => d);
  const z = keep.map(([, i]) => shard.vars[varName][i]);

  const t = theme();

  const traces = [{
    type: "heatmap",
    x, y, z,
    // zsmooth is what replaces an interpolation step: the renderer resamples the
    // station x depth matrix into the smooth field an ODV-style section wants
    zsmooth: "best",
    colorscale: scaleFor(varName),
    connectgaps: true,
    hovertemplate:
      "%{customdata}<br>Depth: %{y} m<br>" +
      `${meta.label}: %{z}${meta.units ? " " + meta.units : ""}<extra></extra>`,
    customdata: z.map((row) =>
      row.map((_, j) => `Station ${shard.stations[j].sta} · ${x[j].toFixed(0)} km`)),
    colorbar: {
      title: { text: meta.units || "", side: "right" },
      thickness: 12, outlinewidth: 0, tickfont: { color: t.ink, size: 11 },
      titlefont: { color: t.ink, size: 11 },
    },
  }, {
    // contour lines over the same field, labelled. The numbers are what make a
    // section readable rather than merely pretty — and they are also the
    // secondary encoding that keeps the plot usable without colour.
    type: "contour",
    x, y, z,
    contours: { coloring: "none", showlabels: true,
                labelfont: { size: 10, color: t.ink } },
    line: { color: t.contour, width: 1 },
    showscale: false,       // without this the contour adds a SECOND colorbar
    showlegend: false,
    hoverinfo: "skip",
  }];

  /* Seafloor silhouette, clipped to the plotted depth range.
   *
   * `floor` is GEBCO sampled every 2 km ALONG the line, not one sounding per
   * station. Station-only sampling drew the Channel Islands bank on line 86.7 as
   * a single triangle 74 km wide and 1.5 km tall, because its neighbours are
   * 37 km away in deep water — terrain that does not exist, sitting right where
   * the thermocline is read. */
  const fl = shard.floor;
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

  // station tick marks along the top
  traces.push({
    type: "scatter",
    mode: "markers",
    x, y: x.map(() => 0),
    marker: { symbol: "triangle-down", size: 9, color: t.ink },
    hovertemplate: shard.stations.map(
      (s) => `Station ${s.sta}<br>${s.dist_km.toFixed(0)} km offshore` +
             (s.bathy_m != null ? `<br>Seafloor ${s.bathy_m.toFixed(0)} m` : "") +
             "<extra></extra>"),
    showlegend: false,
  });

  const layout = {
    // r leaves room for the colorbar; without it the bar renders outside the
    // panel and lands on top of the map
    margin: { l: 58, r: 86, t: 44, b: 52 },
    title: {
      text: `Line ${shard.line} · ${shard.cruise_key.slice(0, 7)} · ${meta.label}`,
      font: { size: 15, color: t.ink },
    },
    font: { color: t.ink },
    xaxis: {
      title: { text: "Distance offshore (km)", font: { size: 12 } },
      zeroline: false, gridcolor: t.grid,
      range: [Math.min(...x), Math.max(...x)],
    },
    yaxis: {
      title: { text: "Depth (m)", font: { size: 12 } },
      autorange: "reversed", range: [maxDepth, 0], gridcolor: t.grid,
    },
    plot_bgcolor: t.panel,
    paper_bgcolor: "rgba(0,0,0,0)",
    hovermode: "closest",
  };

  Plotly.react($("plot"), traces, layout,
    { responsive: true, displaylogo: false });
}

/* ── the map ─────────────────────────────────────────────────────────────── */

/* Plotly's built-in geo layer draws Natural Earth coastlines locally, so the map
 * needs no tile server and no second mapping library. */
function drawMap(shard) {
  const dark = darkMode();
  const on = new Set(shard.stations.map((s) => s.grid_key));
  const off = state.stations.filter((s) => !on.has(s.grid_key));

  const traces = [{
    type: "scattergeo",
    lon: off.map((s) => s.lon), lat: off.map((s) => s.lat),
    mode: "markers",
    marker: { size: 3, color: dark ? "rgba(150,160,172,0.5)"
                                   : "rgba(110,122,134,0.55)" },
    hoverinfo: "skip", showlegend: false,
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
  writeURL(sel);

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

  const maxDepth = Number($("sel-depth").value);
  drawSection(shard, sel.var, maxDepth);
  drawMap(shard);
}

function currentSel() {
  return {
    line: $("sel-line").value,
    cruise: $("sel-cruise").value,
    var: $("sel-var").value,
  };
}

async function init() {
  try {
    const version = await fetch("data/version.json")
      .then((r) => (r.ok ? r.json() : null)).catch(() => null);
    if (version && version.release) {
      CACHE_BUST = "?v=" + encodeURIComponent(version.release);
      $("release").textContent = version.release;
      $("release-foot").textContent = version.release;
    }

    state.index = await getJSON("data/index.json");
    state.stations = await getJSON("data/stations.json");

    const url = readURL();
    const sel = {
      line: url.line || state.index.default.line,
      cruise: url.cruise || state.index.default.cruise_key,
      var: url.var || state.index.default.var,
    };

    for (const id of ["sel-line", "sel-cruise", "sel-var"]) {
      $(id).addEventListener("change", () => render(currentSel()));
    }
    $("sel-depth").addEventListener("input", (e) => {
      $("out-depth").textContent = e.target.value;
      if (state.shard) drawSection(state.shard, $("sel-var").value,
                                   Number(e.target.value));
    });

    await render(sel);
  } catch (err) {
    $("plot").innerHTML =
      `<p class="error">Could not load the section data: ${err.message}</p>`;
    console.error(err);
  }
}

init();
