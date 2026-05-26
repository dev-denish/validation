"""Download OSM roads, water bodies, and settlements for the AWD plot extent.

Uses osmnx to fetch features for the bounding box of the latest valid_plots gpkg
expanded by 0.05°. Saves each layer as a separate GeoPackage in data/raw/.
Runs inside Docker where /app maps to the project root.
"""

import inspect
import sys
from pathlib import Path

import geopandas as gpd
import osmnx as ox
from loguru import logger

RAW_DIR = Path("/app/data/raw")
OUTPUTS_DIR = Path("/app/data/outputs")
BUFFER_DEG = 0.05

LAYERS = {
    "roads": {
        "output": RAW_DIR / "roads.gpkg",
        "tags": {"highway": True},
    },
    "railways": {
        "output": RAW_DIR / "railways.gpkg",
        "tags": {"railway": True},
    },
    "water_bodies": {
        "output": RAW_DIR / "water_bodies.gpkg",
        "tags": {
            "natural": ["water", "wetland"],
            "waterway": True,
            "landuse": "reservoir",
        },
    },
    "settlements": {
        "output": RAW_DIR / "settlements.gpkg",
        "tags": {
            "place": ["city", "town", "village", "hamlet"],
            "landuse": ["residential", "commercial", "industrial"],
        },
    },
}


def get_bbox() -> tuple[float, float, float, float]:
    """Return (west, south, east, north) from latest valid_plots + buffer."""
    gpkg_files = sorted(OUTPUTS_DIR.glob("valid_plots_*.gpkg"))
    if not gpkg_files:
        logger.error("No valid_plots_*.gpkg found in {}", OUTPUTS_DIR)
        sys.exit(1)
    latest = gpkg_files[-1]
    logger.info("Reading bbox from: {}", latest.name)
    gdf = gpd.read_file(latest)
    minx, miny, maxx, maxy = gdf.total_bounds
    west = minx - BUFFER_DEG
    south = miny - BUFFER_DEG
    east = maxx + BUFFER_DEG
    north = maxy + BUFFER_DEG
    logger.info(
        "Bbox (with {:.2f}° buffer): W={:.6f} S={:.6f} E={:.6f} N={:.6f}",
        BUFFER_DEG, west, south, east, north,
    )
    return west, south, east, north


def _osmnx_uses_new_api() -> bool:
    """Detect whether osmnx uses the new bbox=(west,south,east,north) API."""
    sig = inspect.signature(ox.features_from_bbox)
    return "bbox" in sig.parameters and "north" not in sig.parameters


def fetch_features(
    west: float, south: float, east: float, north: float, tags: dict
) -> gpd.GeoDataFrame:
    """Call features_from_bbox handling osmnx 1.x and 2.x APIs."""
    if _osmnx_uses_new_api():
        # osmnx >= 2.0: bbox=(left, bottom, right, top)
        return ox.features_from_bbox(bbox=(west, south, east, north), tags=tags)
    else:
        # osmnx < 2.0: positional (north, south, east, west)
        return ox.features_from_bbox(north, south, east, west, tags=tags)


def download_layer(
    name: str,
    output: Path,
    tags: dict,
    west: float,
    south: float,
    east: float,
    north: float,
) -> None:
    logger.info("Downloading: {}", name)
    try:
        gdf = fetch_features(west, south, east, north, tags)
    except Exception as exc:
        logger.error("Failed to download {}: {}", name, exc)
        return

    if gdf.empty:
        logger.warning("{}: 0 features returned — skipping save.", name)
        return

    if output.exists():
        output.unlink()

    gdf.to_file(str(output), driver="GPKG")

    size_mb = output.stat().st_size / 1_048_576
    logger.success(
        "{}: {:,} features | {:.2f} MB | {}",
        name, len(gdf), size_mb, output.name,
    )


def main() -> None:
    logger.info("=== Download OSM Layers  (osmnx {}) ===", ox.__version__)
    west, south, east, north = get_bbox()

    for name, cfg in LAYERS.items():
        download_layer(name, cfg["output"], cfg["tags"], west, south, east, north)

    logger.success("=== Done ===")
    for name, cfg in LAYERS.items():
        p = cfg["output"]
        if p.exists():
            gdf = gpd.read_file(str(p))
            logger.success("  {}: {:,} features", name, len(gdf))
        else:
            logger.warning("  {}: file not created", name)


if __name__ == "__main__":
    main()
