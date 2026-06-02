"""
Download and process JRC Global Surface Water occurrence layer.

Clips the JRC occurrence tile for the AWD plot extent, extracts permanent water
(occurrence >= 75%), polygonizes, and saves as data/raw/water_jrc_permanent.gpkg.

Tile: occurrence_80E_20Nv1_4_2021 (covers 80–90°E, 20–30°N — includes AWD extent)
Audit raster retained as: data/raw/jrc_occurrence_clipped.tif

Usage (inside Docker):
  python scripts/download_jrc_water.py
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio
from rasterio.features import shapes as rasterio_shapes
from shapely.geometry import shape
from loguru import logger

RAW_DIR = Path("/app/data/raw")
OUTPUTS_DIR = Path("/app/data/outputs")
BUFFER_DEG = 0.05

OUTPUT_GPKG       = RAW_DIR / "water_jrc_permanent.gpkg"
OUTPUT_TIF        = RAW_DIR / "jrc_occurrence_clipped.tif"
OUTPUT_STATS_JSON = RAW_DIR / "jrc_occurrence_stats.json"

JRC_TILE_URL = (
    "https://storage.googleapis.com/global-surface-water/downloads2021/occurrence/"
    "occurrence_80E_20Nv1_4_2021.tif"
)

OCCURRENCE_THRESHOLD = 75  # >= 75% occurrence = permanent water
MIN_AREA_HA = 0.5
WORKING_EPSG = 32643


def get_plot_bbox() -> tuple[float, float, float, float]:
    files = sorted(OUTPUTS_DIR.glob("valid_plots_*.gpkg"))
    if not files:
        logger.error("No valid_plots_*.gpkg found in {}. Run Phase 1 first.", OUTPUTS_DIR)
        sys.exit(1)
    gdf = gpd.read_file(str(files[-1]))
    if gdf.crs and gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs(epsg=4326)
    minx, miny, maxx, maxy = gdf.total_bounds
    w = minx - BUFFER_DEG
    s = miny - BUFFER_DEG
    e = maxx + BUFFER_DEG
    n = maxy + BUFFER_DEG
    logger.info(
        "Plot extent (+{:.2f}°): W={:.4f} S={:.4f} E={:.4f} N={:.4f}",
        BUFFER_DEG, w, s, e, n,
    )
    return w, s, e, n


def download_clip_jrc(w: float, s: float, e: float, n: float) -> bool:
    """Clip JRC occurrence raster to AWD bbox via GDAL vsicurl. Returns True on success."""
    logger.info("Clipping JRC occurrence tile via vsicurl ...")
    logger.info("  Source: {}", JRC_TILE_URL)

    if OUTPUT_TIF.exists():
        OUTPUT_TIF.unlink()

    cmd = [
        "gdal_translate",
        "-projwin", str(w), str(n), str(e), str(s),  # west north east south
        "-of", "GTiff",
        "-co", "COMPRESS=LZW",
        "-co", "TILED=YES",
        f"/vsicurl/{JRC_TILE_URL}",
        str(OUTPUT_TIF),
    ]
    logger.info("Running: {}", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)

    if result.returncode != 0 or not OUTPUT_TIF.exists() or OUTPUT_TIF.stat().st_size == 0:
        logger.error("gdal_translate failed:\n{}", result.stderr[:500])
        return False

    size_mb = OUTPUT_TIF.stat().st_size / 1_048_576
    logger.success("jrc_occurrence_clipped.tif → {:.2f} MB", size_mb)
    return True


def polygonize_jrc(min_area_ha: float = MIN_AREA_HA) -> tuple[gpd.GeoDataFrame, dict]:
    """Extract permanent water polygons from clipped JRC raster.

    Returns (GeoDataFrame, stats_dict). The stats dict is also written to
    OUTPUT_STATS_JSON for use by the Phase 2B checker without re-reading the raster.
    """
    logger.info("Polygonizing JRC water (occurrence >= {}) ...", OCCURRENCE_THRESHOLD)

    with rasterio.open(str(OUTPUT_TIF)) as src:
        arr = src.read(1)
        transform = src.transform
        crs_epsg = src.crs.to_epsg() if src.crs else 4326
        nodata = src.nodata

    # Correct valid_pixels logic: in JRC, pixel value 0 means "never observed as
    # water" — a valid survey result, NOT a NoData pixel. Only use src.nodata when
    # the raster explicitly declares a NoData value; otherwise all pixels are valid.
    if nodata is not None:
        valid_pixels = int((arr != nodata).sum())
        nodata_pixels = int((arr == nodata).sum())
    else:
        valid_pixels = arr.size   # all pixels are valid survey observations
        nodata_pixels = 0

    # Compute water stats before the early-return guard so they always go in the sidecar.
    water_px = int((arr >= OCCURRENCE_THRESHOLD).sum())
    coverage_pct = round(valid_pixels / arr.size * 100, 2) if arr.size > 0 else 0.0

    if water_px == 0 and coverage_pct == 100.0:
        result = "CONFIRMED_NO_PERMANENT_WATER"
    elif water_px == 0:
        result = "CONFIRMED_NO_PERMANENT_WATER_PARTIAL_COVERAGE"
    else:
        result = "WATER_FOUND"

    stats: dict = {
        "total_pixels": int(arr.size),
        "nodata_pixels": nodata_pixels,
        "surveyed_pixels": valid_pixels,
        "pixels_above_threshold": water_px,
        "coverage_pct": coverage_pct,
        "result": result,
    }

    logger.info(
        "JRC raster: total={:,} nodata={:,} surveyed={:,} above_{}%={:,} coverage={:.1f}%",
        arr.size, nodata_pixels, valid_pixels, OCCURRENCE_THRESHOLD, water_px, coverage_pct,
    )

    # Save sidecar JSON for Phase 2B checker (avoids re-reading the raster at pipeline time).
    with open(str(OUTPUT_STATS_JSON), "w") as fh:
        json.dump(stats, fh, indent=2)
    logger.info("JRC raster stats → {}", OUTPUT_STATS_JSON.name)

    if valid_pixels == 0:
        # Genuinely no survey coverage — tile likely did not clip correctly.
        logger.warning(
            "JRC tile has zero surveyed pixels — tile may not cover the AWD extent. "
            "Falling through to OSM-only."
        )
        empty = gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")
        return empty, stats

    if water_px == 0:
        logger.info(
            "JRC survey complete: 0 of {:,} pixels meet >={}% occurrence threshold. "
            "Region confirmed as having no JRC-tracked permanent water.",
            valid_pixels, OCCURRENCE_THRESHOLD,
        )
        empty = gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")
        if OUTPUT_GPKG.exists():
            OUTPUT_GPKG.unlink()
        empty.to_file(str(OUTPUT_GPKG), driver="GPKG", layer="water_jrc_permanent")
        return empty, stats

    mask_arr = (arr >= OCCURRENCE_THRESHOLD).astype(np.uint8)

    water_shapes = [
        (shape(geom), val)
        for geom, val in rasterio_shapes(mask_arr, mask=(mask_arr == 1), transform=transform)
        if val == 1
    ]

    if not water_shapes:
        logger.warning("No JRC water polygons extracted after threshold filter.")
        empty = gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")
        return empty, stats

    geometries = [s for s, _ in water_shapes]
    gdf = gpd.GeoDataFrame({"geometry": geometries}, crs=f"EPSG:{crs_epsg}")

    if gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs(epsg=4326)

    logger.info("  Raw polygons (before area filter): {:>10,}", len(gdf))

    gdf_metric = gdf.to_crs(epsg=WORKING_EPSG)
    gdf_metric["area_ha"] = gdf_metric.geometry.area / 10_000
    gdf = gdf_metric[gdf_metric["area_ha"] >= min_area_ha].copy()
    gdf = gdf.to_crs(epsg=4326)

    logger.info("  After area filter (>= {} ha):      {:>10,}", min_area_ha, len(gdf))

    gdf["source"] = "JRC_Global_Surface_Water_v1.4_2021"
    gdf["occurrence_threshold"] = OCCURRENCE_THRESHOLD
    gdf["min_area_ha"] = min_area_ha

    gdf = gdf.reset_index(drop=True)
    gdf.insert(0, "jrc_id", range(1, len(gdf) + 1))

    if OUTPUT_GPKG.exists():
        OUTPUT_GPKG.unlink()
    gdf.to_file(str(OUTPUT_GPKG), driver="GPKG", layer="water_jrc_permanent")
    size_mb = OUTPUT_GPKG.stat().st_size / 1_048_576
    logger.success("water_jrc_permanent.gpkg → {:,} polygons | {:.2f} MB", len(gdf), size_mb)

    if len(gdf) > 0:
        gdf_m = gdf.to_crs(epsg=WORKING_EPSG)
        total_ha = gdf_m.geometry.area.sum() / 10_000
        logger.success(
            "  Total JRC water area: {:.2f} ha ({:.2f} km²)", total_ha, total_ha / 100
        )

    return gdf, stats


def main() -> None:
    logger.info("=" * 60)
    logger.info("JRC Global Surface Water — Permanent Water Download")
    logger.info("  Tile:      80E_20N  (covers 80–90°E, 20–30°N)")
    logger.info("  Threshold: occurrence >= {}%", OCCURRENCE_THRESHOLD)
    logger.info("  Min area:  >= {} ha", MIN_AREA_HA)
    logger.info("=" * 60)

    w, s, e, n = get_plot_bbox()

    ok = download_clip_jrc(w, s, e, n)
    if not ok:
        logger.error("JRC raster download failed — aborting.")
        sys.exit(1)

    gdf, stats = polygonize_jrc()

    logger.info("=" * 60)
    logger.info("SUMMARY")
    logger.info("  Clipped raster (audit): {}", OUTPUT_TIF)
    logger.info("  Vector output:          {}", OUTPUT_GPKG)
    logger.info("  Raster stats JSON:      {}", OUTPUT_STATS_JSON)
    logger.info("  Polygons:               {:,}", len(gdf))
    logger.info("  JRC result:             {}", stats["result"])
    if len(gdf) > 0:
        gdf_m = gdf.to_crs(epsg=WORKING_EPSG)
        total_ha = gdf_m.geometry.area.sum() / 10_000
        logger.info("  Total water area:       {:.2f} ha", total_ha)
    logger.info("=" * 60)
    logger.info("Next: rerun Phase 2 pipeline:")
    logger.info("  docker compose run --rm awd-validation python scripts/run_pipeline_phase2.py")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
