"""
Download and process Hansen GFC v1.12 (2024) forest cover layer.

Methodology reference:
  Maharashtra FSI definition: canopy >= 10%, area >= 1.0 ha
  CDM DNA fallback:           canopy >= 10%, height >= 5m, area >= 0.5 ha
  This script applies:        treecover2000 >= 10% AND lossyear == 0 (no loss
                              through 2023) — satisfies both definitions.

Tile coverage logic:
  Hansen tiles are 10°×10° named by their NW corner (e.g. 30N_070E covers
  20–30°N, 70–80°E). Maharashtra bbox ~15.6–22.0°N, 72.6–80.9°E — spans
  four tiles. Script auto-detects which tiles are needed from the plot extent.

Output:
  data/raw/forest_hansen_canopy_2023.gpkg  — vector polygons, EPSG:4326
  data/raw/hansen_v112_treecover_clipped.tif  — masked raster (kept for audit)

Usage (inside Docker):
  python scripts/download_hansen_v112.py

Author:  AWD Validation Pipeline
Version: v1.12 (GFC-2024-v1.12)
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

import geopandas as gpd
import numpy as np
import rasterio
from rasterio.merge import merge as rasterio_merge
from rasterio.mask import mask as rasterio_mask
from rasterio.features import shapes as rasterio_shapes
from shapely.geometry import box, shape, mapping
from shapely.ops import unary_union
from loguru import logger

# ── CONFIG ────────────────────────────────────────────────────────────────────

RAW_DIR        = Path("/app/data/raw")
OUTPUTS_DIR    = Path("/app/data/outputs")
OUTPUT_GPKG    = RAW_DIR / "forest_hansen_canopy_2023.gpkg"
OUTPUT_TIF     = RAW_DIR / "hansen_v112_treecover_clipped.tif"

# Hansen v1.12 GCS base URL
HANSEN_BASE = (
    "https://storage.googleapis.com/earthenginepartners-hansen/GFC-2024-v1.12"
)

# Forest definition thresholds (Maharashtra FSI confirmed)
CANOPY_THRESHOLD_PCT = 10      # treecover2000 >= 10%
MIN_AREA_HA          = 1.0     # minimum polygon area after polygonize
BBOX_BUFFER_DEG      = 0.05    # buffer around plot extent before clipping tiles

# lossyear == 0 means the pixel had NO recorded deforestation 2001–2023
# Any lossyear > 0 means forest was cleared in that year → not eligible
LOSSYEAR_INTACT      = 0


# ── TILE HELPERS ──────────────────────────────────────────────────────────────

def _hansen_tile_bounds(tile_name: str) -> tuple[float, float, float, float]:
    """
    Return (west, south, east, north) for a Hansen tile name.
    Hansen tile naming: NW corner, e.g. '30N_070E' → 20–30°N, 70–80°E.
    """
    parts = tile_name.split("_")
    lat_str, lon_str = parts[0], parts[1]

    lat_n = int(lat_str[:-1]) * (1 if lat_str[-1] == "N" else -1)
    lon_w = int(lon_str[:-1]) * (1 if lon_str[-1] == "E" else -1)

    return lon_w, lat_n - 10, lon_w + 10, lat_n


def _get_required_tiles(
    w: float, s: float, e: float, n: float
) -> list[str]:
    """
    Return all Hansen 10°×10° tile names that intersect the given bbox.
    Rounds to Hansen grid (multiples of 10°, lat snaps to 10/20/30/40/50/60/70/80).
    """
    valid_lat_tops = list(range(10, 90, 10))   # 10, 20, 30, … 80
    valid_lon_wests = list(range(-180, 180, 10))

    aoi = box(w, s, e, n)
    required: list[str] = []

    for lat_top in valid_lat_tops:
        for lon_west in valid_lon_wests:
            tile_box = box(lon_west, lat_top - 10, lon_west + 10, lat_top)
            if tile_box.intersects(aoi):
                lat_str = f"{lat_top:02d}N"
                lon_str = (
                    f"{abs(lon_west):03d}{'E' if lon_west >= 0 else 'W'}"
                )
                required.append(f"{lat_str}_{lon_str}")

    return required


def _tile_url(tile: str, band: str) -> str:
    """Build full GCS URL for a Hansen band tile."""
    return f"{HANSEN_BASE}/Hansen_GFC-2024-v1.12_{band}_{tile}.tif"


# ── BBOX FROM PLOTS ───────────────────────────────────────────────────────────

def get_plot_bbox() -> tuple[float, float, float, float]:
    """Read latest Phase 1F valid_plots output and return padded bbox."""
    files = sorted(OUTPUTS_DIR.glob("valid_plots_*.gpkg"))
    if not files:
        logger.error(
            "No valid_plots_*.gpkg found in {}. Run Phase 1 first.", OUTPUTS_DIR
        )
        sys.exit(1)
    gdf = gpd.read_file(str(files[-1]))
    if gdf.crs and gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs(epsg=4326)
    minx, miny, maxx, maxy = gdf.total_bounds
    w = minx - BBOX_BUFFER_DEG
    s = miny - BBOX_BUFFER_DEG
    e = maxx + BBOX_BUFFER_DEG
    n = maxy + BBOX_BUFFER_DEG
    logger.info(
        "Plot extent (+{:.2f}°): W={:.4f} S={:.4f} E={:.4f} N={:.4f}",
        BBOX_BUFFER_DEG, w, s, e, n
    )
    return w, s, e, n


# ── RASTER DOWNLOAD ───────────────────────────────────────────────────────────

def _vsicurl_clip(
    tile_url: str,
    w: float,
    s: float,
    e: float,
    n: float,
    out_path: Path,
) -> bool:
    """
    Clip a single remote GeoTIFF to bbox via GDAL vsicurl (streaming — no full
    tile download). Returns True on success.
    """
    cmd = [
        "gdal_translate",
        "-projwin", str(w), str(n), str(e), str(s),  # west north east south
        "-of", "GTiff",
        "-co", "COMPRESS=LZW",
        "-co", "TILED=YES",
        f"/vsicurl/{tile_url}",
        str(out_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if result.returncode != 0 or not out_path.exists() or out_path.stat().st_size == 0:
        logger.warning("vsicurl clip failed for {}: {}", tile_url, result.stderr[:300])
        return False
    size_mb = out_path.stat().st_size / 1_048_576
    logger.info("  Downloaded: {} ({:.2f} MB)", out_path.name, size_mb)
    return True


def download_tiles(
    tiles: list[str],
    w: float,
    s: float,
    e: float,
    n: float,
    tmpdir: Path,
) -> tuple[list[Path], list[Path]]:
    """
    Download treecover2000 and lossyear clipped tiles for all required Hansen
    tiles. Returns (treecover_paths, lossyear_paths).
    """
    tc_paths: list[Path] = []
    ly_paths: list[Path] = []

    for tile in tiles:
        tc_url = _tile_url(tile, "treecover2000")
        ly_url = _tile_url(tile, "lossyear")

        tc_out = tmpdir / f"treecover_{tile}.tif"
        ly_out = tmpdir / f"lossyear_{tile}.tif"

        logger.info("Tile {} — downloading treecover2000 ...", tile)
        tc_ok = _vsicurl_clip(tc_url, w, s, e, n, tc_out)

        logger.info("Tile {} — downloading lossyear ...", tile)
        ly_ok = _vsicurl_clip(ly_url, w, s, e, n, ly_out)

        if tc_ok and ly_ok:
            tc_paths.append(tc_out)
            ly_paths.append(ly_out)
        else:
            logger.warning(
                "Tile {} skipped — one or both bands failed to download.", tile
            )

    if not tc_paths:
        logger.error("No tiles downloaded successfully. Aborting.")
        sys.exit(1)

    return tc_paths, ly_paths


# ── MOSAIC ────────────────────────────────────────────────────────────────────

def _mosaic_tiles(
    paths: list[Path], out_path: Path
) -> None:
    """Merge multiple clipped tiles into a single raster."""
    if len(paths) == 1:
        import shutil
        shutil.copy(str(paths[0]), str(out_path))
        return

    datasets = [rasterio.open(str(p)) for p in paths]
    mosaic_arr, mosaic_transform = rasterio_merge(datasets)
    meta = datasets[0].meta.copy()
    meta.update({
        "driver":    "GTiff",
        "height":    mosaic_arr.shape[1],
        "width":     mosaic_arr.shape[2],
        "transform": mosaic_transform,
        "compress":  "lzw",
    })
    for ds in datasets:
        ds.close()
    with rasterio.open(str(out_path), "w", **meta) as dst:
        dst.write(mosaic_arr)
    logger.info(
        "Mosaic written: {} ({} tiles → {:.2f} MB)",
        out_path.name, len(paths), out_path.stat().st_size / 1_048_576
    )


# ── MASK + CLIP TO AWD EXTENT ─────────────────────────────────────────────────

def apply_forest_mask(
    tc_mosaic: Path,
    ly_mosaic: Path,
    w: float,
    s: float,
    e: float,
    n: float,
    out_tif: Path,
) -> None:
    """
    Apply forest mask:
      treecover2000 >= CANOPY_THRESHOLD_PCT  (canopy cover threshold)
      AND lossyear == 0                      (no recorded deforestation)

    Output: binary raster (1 = eligible forest, 0 = not forest / deforested).
    """
    logger.info(
        "Applying mask: treecover2000 >= {}% AND lossyear == {} (intact) ...",
        CANOPY_THRESHOLD_PCT, LOSSYEAR_INTACT
    )

    aoi_geom = [mapping(box(w, s, e, n))]

    with rasterio.open(str(tc_mosaic)) as tc_src:
        tc_arr, tc_transform = rasterio_mask(
            tc_src, aoi_geom, crop=True, nodata=0
        )
        meta = tc_src.meta.copy()
        meta.update(transform=tc_transform, height=tc_arr.shape[1], width=tc_arr.shape[2])
        tc_data = tc_arr[0].astype(np.uint8)

    with rasterio.open(str(ly_mosaic)) as ly_src:
        ly_arr, _ = rasterio_mask(
            ly_src, aoi_geom, crop=True, nodata=255
        )
        ly_data = ly_arr[0].astype(np.uint8)

    # Core forest mask:
    #   pixel value >= 10 in treecover2000 → meets canopy threshold
    #   lossyear == 0 → pixel was NEVER flagged as deforested (2001–2023)
    forest_mask = (tc_data >= CANOPY_THRESHOLD_PCT) & (ly_data == LOSSYEAR_INTACT)

    # Stats for audit log
    total_px     = forest_mask.size
    forest_px    = int(forest_mask.sum())
    cleared_px   = int(((tc_data >= CANOPY_THRESHOLD_PCT) & (ly_data > 0)).sum())
    pct_forest   = forest_px / total_px * 100 if total_px > 0 else 0

    logger.info(
        "  Total pixels:          {:>12,}", total_px
    )
    logger.info(
        "  Forest (intact):       {:>12,}  ({:.2f}%)", forest_px, pct_forest
    )
    logger.info(
        "  Deforested (excluded): {:>12,}  (treecover >= {}% but lossyear > 0)",
        cleared_px, CANOPY_THRESHOLD_PCT
    )

    result = forest_mask.astype(np.uint8)

    meta.update(dtype="uint8", count=1, nodata=0, compress="lzw")
    out_tif.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(str(out_tif), "w", **meta) as dst:
        dst.write(result, 1)

    logger.info(
        "Masked raster saved: {} ({:.2f} MB)",
        out_tif.name, out_tif.stat().st_size / 1_048_576
    )


# ── POLYGONIZE ────────────────────────────────────────────────────────────────

def polygonize_forest(
    masked_tif: Path,
    out_gpkg: Path,
    min_area_ha: float = MIN_AREA_HA,
) -> gpd.GeoDataFrame:
    """
    Convert binary forest raster → vector polygons.
    Filters to: value == 1 (forest) AND area >= min_area_ha.

    Uses rasterio.features.shapes (pure Python — no GDAL polygonize binary
    required). More reliable than subprocess gdal_polygonize in Docker.
    """
    logger.info("Polygonizing forest raster (min {} ha) ...", min_area_ha)

    with rasterio.open(str(masked_tif)) as src:
        arr = src.read(1)
        transform = src.transform
        crs = src.crs

    # Extract shapes for value == 1 (forest pixels only)
    forest_shapes = [
        (shape(geom), val)
        for geom, val in rasterio_shapes(arr, mask=(arr == 1), transform=transform)
        if val == 1
    ]

    if not forest_shapes:
        logger.warning("No forest polygons extracted — check raster mask.")
        return gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")

    geometries = [s for s, _ in forest_shapes]
    gdf = gpd.GeoDataFrame(
        {"geometry": geometries},
        crs=crs.to_epsg() if crs else 4326
    )

    if gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs(epsg=4326)

    logger.info("  Raw polygons (before area filter): {:,}", len(gdf))

    # Area filter — reproject to metric CRS for accurate ha calculation
    gdf_metric = gdf.to_crs(epsg=32643)
    gdf_metric["area_ha"] = gdf_metric.geometry.area / 10_000
    gdf = gdf_metric[gdf_metric["area_ha"] >= min_area_ha].copy()
    gdf = gdf.to_crs(epsg=4326)

    logger.info(
        "  After area filter (>= {} ha): {:,} polygons",
        min_area_ha, len(gdf)
    )

    # Add provenance columns
    gdf["source"]           = "Hansen_GFC-2024-v1.12"
    gdf["canopy_threshold"] = CANOPY_THRESHOLD_PCT
    gdf["lossyear_mask"]    = "intact_only"
    gdf["min_area_ha"]      = min_area_ha
    gdf["definition"]       = "maharashtra_fsi"

    # Reset index and add sequential feature ID
    gdf = gdf.reset_index(drop=True)
    gdf.insert(0, "hansen_id", range(1, len(gdf) + 1))

    out_gpkg.parent.mkdir(parents=True, exist_ok=True)
    if out_gpkg.exists():
        out_gpkg.unlink()
    gdf.to_file(str(out_gpkg), driver="GPKG", layer="forest_hansen_v112")
    size_mb = out_gpkg.stat().st_size / 1_048_576

    logger.success(
        "forest_hansen_canopy_2023.gpkg → {:,} polygons | {:.2f} MB",
        len(gdf), size_mb
    )

    return gdf


# ── SETTINGS UPDATE ───────────────────────────────────────────────────────────

def update_settings_yaml(settings_path: Path) -> None:
    """
    Update config/settings.yaml:
      - hansen_layer_path → forest_hansen_canopy_2023.gpkg
      - hansen_enabled    → true (already true, but enforce)
    Non-destructive: only replaces the two target lines.
    """
    if not settings_path.exists():
        logger.warning("settings.yaml not found at {} — skipping update.", settings_path)
        return

    text = settings_path.read_text()

    old_path = "hansen_layer_path: \"/app/data/raw/forest_hansen_canopy.gpkg\""
    new_path = "hansen_layer_path: \"/app/data/raw/forest_hansen_canopy_2023.gpkg\""

    if old_path in text:
        text = text.replace(old_path, new_path)
        logger.info("settings.yaml: updated hansen_layer_path → forest_hansen_canopy_2023.gpkg")
    elif "forest_hansen_canopy_2023.gpkg" in text:
        logger.info("settings.yaml: hansen_layer_path already points to v1.12 layer — no change.")
    else:
        logger.warning(
            "settings.yaml: could not find expected hansen_layer_path line. "
            "Please manually update phase2.forest.hansen_layer_path to: "
            "/app/data/raw/forest_hansen_canopy_2023.gpkg"
        )

    settings_path.write_text(text)


# ── SUMMARY ───────────────────────────────────────────────────────────────────

def print_summary(gdf: gpd.GeoDataFrame, tiles_used: list[str]) -> None:
    logger.info("=" * 60)
    logger.info("HANSEN v1.12 DOWNLOAD — SUMMARY")
    logger.info("=" * 60)
    logger.info("  Source:          GFC-2024-v1.12 (Hansen 2024)")
    logger.info("  Tiles processed: {}", ", ".join(tiles_used))
    logger.info("  Canopy filter:   >= {}%", CANOPY_THRESHOLD_PCT)
    logger.info("  Lossyear filter: == 0 (intact through 2023)")
    logger.info("  Min area:        >= {} ha", MIN_AREA_HA)
    logger.info("  Output polygons: {:,}", len(gdf))
    if len(gdf) > 0:
        gdf_m = gdf.to_crs(epsg=32643)
        total_ha = gdf_m.geometry.area.sum() / 10_000
        logger.info("  Total forest area: {:.2f} ha ({:.2f} km²)", total_ha, total_ha / 100)
    logger.info("  Output file:     {}", OUTPUT_GPKG)
    logger.info("  Audit raster:    {}", OUTPUT_TIF)
    logger.info("=" * 60)
    logger.info("Next steps:")
    logger.info("  1. settings.yaml updated — hansen_layer_path now v1.12")
    logger.info("  2. Rerun Phase 2:")
    logger.info("     docker compose run --rm awd-validation \\")
    logger.info("       python scripts/run_pipeline_phase2.py")
    logger.info("=" * 60)


# ── MAIN ──────────────────────────────────────────────────────────────────────

def main() -> None:
    logger.info("=" * 60)
    logger.info("Hansen GFC v1.12 Download + Processing")
    logger.info("Forest definition: Maharashtra FSI")
    logger.info("  canopy >= {}% + lossyear == 0 + area >= {} ha",
                CANOPY_THRESHOLD_PCT, MIN_AREA_HA)
    logger.info("=" * 60)

    # Step 1 — get plot extent
    w, s, e, n = get_plot_bbox()

    # Step 2 — determine which Hansen tiles cover the extent
    tiles = _get_required_tiles(w, s, e, n)
    logger.info("Hansen tiles required for extent: {}", ", ".join(tiles))

    with tempfile.TemporaryDirectory(prefix="hansen_v112_") as tmpdir:
        tmp = Path(tmpdir)

        # Step 3 — download treecover2000 + lossyear for each tile
        tc_paths, ly_paths = download_tiles(tiles, w, s, e, n, tmp)

        # Step 4 — mosaic (if multiple tiles)
        tc_mosaic = tmp / "treecover_mosaic.tif"
        ly_mosaic = tmp / "lossyear_mosaic.tif"

        if len(tc_paths) > 1:
            logger.info("Mosaicking {} treecover tiles ...", len(tc_paths))
            _mosaic_tiles(tc_paths, tc_mosaic)
            logger.info("Mosaicking {} lossyear tiles ...", len(ly_paths))
            _mosaic_tiles(ly_paths, ly_mosaic)
        else:
            tc_mosaic = tc_paths[0]
            ly_mosaic = ly_paths[0]

        # Step 5 — apply forest mask (canopy threshold + lossyear filter)
        apply_forest_mask(tc_mosaic, ly_mosaic, w, s, e, n, OUTPUT_TIF)

    # Step 6 — polygonize (tempdir cleaned up, work from OUTPUT_TIF)
    gdf = polygonize_forest(OUTPUT_TIF, OUTPUT_GPKG, min_area_ha=MIN_AREA_HA)

    # Step 7 — update settings.yaml
    settings_path = Path("/app/config/settings.yaml")
    update_settings_yaml(settings_path)

    # Step 8 — summary
    print_summary(gdf, tiles)


if __name__ == "__main__":
    main()
