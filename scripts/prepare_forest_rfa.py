"""Clip Bharatmaps_RFA.geojsonl to AWD plot bounding box + 0.05° buffer.

Saves the result as data/raw/forest_maharashtra_rfa.gpkg.
Runs inside Docker where /app maps to the project root.
"""

import glob
import os
import subprocess
import sys
from pathlib import Path

import geopandas as gpd
from loguru import logger

RAW_DIR = Path("/app/data/raw")
OUTPUTS_DIR = Path("/app/data/outputs")
SOURCE = RAW_DIR / "Bharatmaps_RFA.geojsonl"
OUTPUT = RAW_DIR / "forest_maharashtra_rfa.gpkg"
BUFFER_DEG = 0.05


def get_bbox_from_latest_valid_plots() -> tuple[float, float, float, float]:
    gpkg_files = sorted(OUTPUTS_DIR.glob("valid_plots_*.gpkg"))
    if not gpkg_files:
        logger.error("No valid_plots_*.gpkg found in {}", OUTPUTS_DIR)
        sys.exit(1)
    latest = gpkg_files[-1]
    logger.info("Reading bbox from: {}", latest.name)
    gdf = gpd.read_file(latest)
    minx, miny, maxx, maxy = gdf.total_bounds
    logger.info(
        "Raw plot extent: minx={:.6f} miny={:.6f} maxx={:.6f} maxy={:.6f}",
        minx, miny, maxx, maxy,
    )
    return (
        minx - BUFFER_DEG,
        miny - BUFFER_DEG,
        maxx + BUFFER_DEG,
        maxy + BUFFER_DEG,
    )


def clip_rfa(minx: float, miny: float, maxx: float, maxy: float) -> None:
    if not SOURCE.exists():
        logger.error("Source file not found: {}", SOURCE)
        sys.exit(1)

    if OUTPUT.exists():
        OUTPUT.unlink()
        logger.info("Removed existing output file.")

    logger.info(
        "Clipping to bbox: minx={:.6f} miny={:.6f} maxx={:.6f} maxy={:.6f}",
        minx, miny, maxx, maxy,
    )
    logger.info("Source size: {:.1f} MB", SOURCE.stat().st_size / 1_048_576)

    cmd = [
        "ogr2ogr",
        "-f", "GPKG",
        str(OUTPUT),
        str(SOURCE),
        "-spat", str(minx), str(miny), str(maxx), str(maxy),
        "-progress",
    ]
    logger.info("Running: {}", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=False, text=True)
    if result.returncode != 0:
        logger.error("ogr2ogr failed with code {}", result.returncode)
        sys.exit(result.returncode)


def report() -> None:
    if not OUTPUT.exists():
        logger.error("Output file not created.")
        sys.exit(1)

    size_mb = OUTPUT.stat().st_size / 1_048_576
    gdf = gpd.read_file(OUTPUT)
    logger.success("=" * 60)
    logger.success("Output : {}", OUTPUT)
    logger.success("Size   : {:.2f} MB", size_mb)
    logger.success("Features: {:,}", len(gdf))
    logger.success("CRS    : {}", gdf.crs)
    logger.success("Columns: {}", list(gdf.columns))
    logger.success("=" * 60)


def main() -> None:
    logger.info("=== Prepare Forest RFA ===")
    minx, miny, maxx, maxy = get_bbox_from_latest_valid_plots()
    clip_rfa(minx, miny, maxx, maxy)
    report()


if __name__ == "__main__":
    main()
