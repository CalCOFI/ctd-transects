# build_station_bathymetry.R — seafloor profile along each CalCOFI line
#
#   Rscript scripts/build_station_bathymetry.R
#
# Writes TWO COMMITTED INPUTS to the CI build (not CI outputs). Run by hand; they
# are only stale if the CalCOFI grid itself changes, which it does not.
#
#   metadata/station_bathymetry.csv  one row per grid station
#   metadata/line_bathymetry.csv     the seafloor sampled every 2 km ALONG each
#                                    line, which is what the app draws
#
# WHY THE DENSE PROFILE
#   Sampling the seafloor only at stations and joining those points with straight
#   lines invents terrain. Line 86.7 is the case that forced this: station 50 sits
#   on a Channel Islands bank at 80 m, between neighbours at 1,654 m and 1,190 m
#   that are 37 km away on either side. Station-only sampling drew that bank as a
#   single triangle 74 km wide rising 1.5 km off the seafloor — a mountain that
#   does not exist, at the exact depth range where someone is reading the
#   thermocline.
#
#   apps/ctd-viz has the same limitation and documents it (get_transect_bathy()
#   deliberately does not interpolate between casts), but it draws a line rather
#   than a filled silhouette, so the artefact is less assertive there.
#
# Why not sample GEBCO in CI? The refresh workflow would then need the 4.28 MB
# GeoTIFF (apps/ctd-viz/data/gebco_calcofi.tif, itself a crop of a much larger
# source) plus terra and its GDAL stack, on every run, to recompute 218 numbers
# that never change. apps/ctd-viz ships that raster inside the app and extracts
# per transect at request time because it lets the user pick arbitrary transects;
# here the station set is fixed, so the sensible move is to bake it once.
#
# The app draws this as the grey seafloor silhouette under each section.

suppressMessages({
  library(dplyr); library(readr); library(terra); library(glue)
  library(purrr); library(tidyr); library(geosphere)
})

# sample spacing along a line, km. 2 km resolves the Channel Islands banks
# without making the files big: ~700 km per line x 11 lines is ~3,900 rows.
STEP_KM <- 2

gebco_tif <- "../apps/ctd-viz/data/gebco_calcofi.tif"
grid_pq   <- "public/data/_grid.parquet"

stopifnot(
  "run scripts/build_sections.sql first — _grid.parquet is missing" =
    file.exists(grid_pq),
  "GEBCO raster not found; expected the sibling ctd-viz app checkout" =
    file.exists(gebco_tif))

d_grid <- arrow::read_parquet(grid_pq)
bathy  <- rast(gebco_tif)

# the raster is positive-DOWN depth in metres with land already clamped to 0
# (see apps/ctd-viz/prep_db.R), so no sign flip here. bilinear rather than nearest
# so a station near a steep slope is not pinned to one 15-arc-second cell.
sample_bathy <- function(lon, lat)
  terra::extract(bathy, cbind(lon, lat), method = "bilinear")[, 1] |>
    pmax(0) |>
    round(1)

# `line_dist_km` is distance along the line from its most-inshore grid station.
# It is the common ruler that lets the app place the dense profile on a section
# whose x-axis is built from whichever stations a given cruise actually occupied.
d_out <- d_grid |>
  arrange(line, sta) |>
  group_by(line) |>
  mutate(
    line_dist_km = c(0, cumsum(distHaversine(
      cbind(head(lon, -1), head(lat, -1)),
      cbind(tail(lon, -1), tail(lat, -1))) / 1000)) |> round(3)) |>
  ungroup() |>
  mutate(bathy_m = sample_bathy(lon, lat)) |>
  select(grid_key, line, sta, lon, lat, shore, bathy_m, line_dist_km)

# --- dense along-line profile -------------------------------------------------
# walk each adjacent station pair and sample the great-circle path between them
densify_line <- function(d) {
  d <- arrange(d, sta)
  if (nrow(d) < 2) return(NULL)
  seg <- map_dfr(seq_len(nrow(d) - 1), function(i) {
    a <- d[i, ]; b <- d[i + 1, ]
    len_km <- (b$line_dist_km - a$line_dist_km)
    n <- max(2L, ceiling(len_km / STEP_KM) + 1L)
    f <- seq(0, 1, length.out = n)
    # gcIntermediate would allocate a list per segment; a linear blend in lon/lat
    # is within metres of the great circle over a 40 km leg at this latitude
    tibble(
      line_dist_km = a$line_dist_km + f * len_km,
      lon = a$lon + f * (b$lon - a$lon),
      lat = a$lat + f * (b$lat - a$lat))
  })
  seg |>
    distinct(line_dist_km, .keep_all = TRUE) |>
    arrange(line_dist_km)
}

d_line <- d_out |>
  group_by(line) |>
  group_modify(~ densify_line(.x)) |>
  ungroup() |>
  mutate(bathy_m = sample_bathy(lon, lat),
         line_dist_km = round(line_dist_km, 2)) |>
  filter(!is.na(bathy_m)) |>
  select(line, line_dist_km, bathy_m)

# A NULL here is expected, not a failure: the GEBCO crop covers the modern CalCOFI
# area (29.3-38.35N, -126.98 to -116.78) while `grid` also carries the historical
# northern lines (line 10 sits at 48N, off Washington) and the far-offshore
# stations beyond the crop's western edge. What matters is that every station the
# app actually plots has a depth — assert that, and report the rest as context.
d_sec <- arrow::read_parquet("public/data/_stations.parquet")
missing_plotted <- d_out |>
  filter(is.na(bathy_m), grid_key %in% unique(d_sec$grid_key))

stopifnot(
  "a station that appears in a section has no seafloor depth" =
    nrow(missing_plotted) == 0)

cat(glue(
  "{sum(is.na(d_out$bathy_m))} of {nrow(d_out)} grid stations lie outside the ",
  "GEBCO crop (historical northern lines + far offshore); none of them is ",
  "plotted"), "\n")

# na = "" — never let readr's default write the two-character string "NA", which
# DuckDB's read_csv_auto reads back as a literal value rather than a null
write_csv(d_out,  "metadata/station_bathymetry.csv", na = "")
write_csv(d_line, "metadata/line_bathymetry.csv",    na = "")

cat(glue(
  "metadata/station_bathymetry.csv: {nrow(d_out)} stations, ",
  "depth {min(d_out$bathy_m, na.rm = TRUE)}-{max(d_out$bathy_m, na.rm = TRUE)} m"),
  "\n")
cat(glue(
  "metadata/line_bathymetry.csv   : {nrow(d_line)} samples across ",
  "{n_distinct(d_line$line)} lines at {STEP_KM} km spacing"), "\n")
