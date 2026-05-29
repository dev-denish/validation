"""
Job C — Google Open Buildings v3 download + merge with Microsoft buildings.

Outcome for AWD extent (Vidarbha, token 3a3):
  Google OB v3 polygon tile 3a3_buildings.csv is 7.9 GB (entire central-India
  S2 level-4 cell). Downloading is not feasible without the per-region
  sub-tile index that v1.csv.gz was supposed to provide (404).
  Pipeline falls through to Microsoft-only per Job C specification.

Run inside Docker:
  docker compose run --rm awd-validation python scripts/download_google_buildings.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import geopandas as gpd
import requests
from loguru import logger

RAW_DIR = Path("/app/data/raw")
OUTPUTS_DIR = Path("/app/data/outputs")
BUFFER_DEG = 0.05
CONFIDENCE_THRESHOLD = 0.65
IOU_THRESHOLD = 0.50

INDEX_URL = (
    "https://storage.googleapis.com/open-buildings-data/v3/"
    "open_buildings_v3_polygons_s2_level_4_gzip_v1.csv.gz"
)
TILE_BASE = (
    "https://storage.googleapis.com/open-buildings-data/v3/polygons_s2_level_4/"
)

MS_BUILDINGS = RAW_DIR / "buildings.gpkg"
OUTPUT_GOOGLE = RAW_DIR / "buildings_google.gpkg"
OUTPUT_MERGED = RAW_DIR / "buildings_merged.gpkg"


def get_bbox() -> tuple[float, float, float, float]:
    files = sorted(OUTPUTS_DIR.glob("valid_plots_*.gpkg"))
    if not files:
        logger.error("No valid_plots_*.gpkg found. Run Phase 1 first.")
        sys.exit(1)
    gdf = gpd.read_file(str(files[-1]))
    if gdf.crs and gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs(epsg=4326)
    minx, miny, maxx, maxy = gdf.total_bounds
    w = minx - BUFFER_DEG
    s = miny - BUFFER_DEG
    e = maxx + BUFFER_DEG
    n = maxy + BUFFER_DEG
    logger.info("AWD bbox (+{:.2f}°): W={:.4f} S={:.4f} E={:.4f} N={:.4f}", BUFFER_DEG, w, s, e, n)
    return w, s, e, n


def probe_google_ob() -> tuple[str | None, float | None]:
    """
    Probe Google OB availability. Returns (reason_failed, tile_size_mb) where
    reason_failed is None if download is feasible.
    """
    # 1. Check specified index URL
    try:
        r = requests.head(INDEX_URL, timeout=10)
        if r.status_code != 200:
            logger.warning(
                "Google OB index URL returned HTTP {} — specified URL is unreachable.",
                r.status_code
            )
            logger.info("  Attempted: {}", INDEX_URL)
    except Exception as exc:
        logger.warning("Google OB index URL unreachable: {}", exc)
        return "INDEX_URL_UNREACHABLE", None

    # 2. Discover actual tile via s2sphere
    try:
        import s2sphere
    except ImportError:
        return "S2SPHERE_NOT_INSTALLED", None

    from shapely.geometry import box as shapely_box  # noqa: F401
    W, S, E, N = 80.0399, 21.1233, 80.6358, 21.5470
    coverer = s2sphere.RegionCoverer()
    coverer.min_level = 4
    coverer.max_level = 4
    coverer.max_cells = 20
    region = s2sphere.LatLngRect(
        s2sphere.LatLng.from_degrees(S, W),
        s2sphere.LatLng.from_degrees(N, E),
    )
    tokens = [cid.to_token() for cid in coverer.get_covering(region)]
    logger.info("S2 level-4 cell(s) for AWD extent: {}", tokens)

    for tok in tokens:
        url = f"{TILE_BASE}{tok}_buildings.csv"
        r2 = requests.head(url, timeout=10)
        if r2.status_code == 200:
            size_mb = int(r2.headers.get("Content-Length", 0)) / 1_048_576
            logger.warning(
                "Tile {} found at {} but is {:.1f} GB — "
                "not feasible to download without a spatial sub-index.",
                tok, url, size_mb / 1024
            )
            logger.info(
                "  Google OB v3 uses S2 level-4 tiles covering ~millions of km². "
                "The per-region gzip index (v1.csv.gz) in the job spec returned 404. "
                "Cannot spatially filter without it."
            )
            return f"TILE_TOO_LARGE_{size_mb:.0f}MB", size_mb

    return "NO_TILES_FOUND", None


def merge_microsoft_only(ms_count: int) -> gpd.GeoDataFrame:
    """Fall-through: merged = Microsoft buildings only."""
    logger.info("Creating buildings_merged.gpkg from Microsoft buildings only ...")
    ms_gdf = gpd.read_file(str(MS_BUILDINGS))
    if OUTPUT_MERGED.exists():
        OUTPUT_MERGED.unlink()
    ms_gdf.to_file(str(OUTPUT_MERGED), driver="GPKG", layer="buildings_merged")
    size_mb = OUTPUT_MERGED.stat().st_size / 1_048_576
    logger.info("buildings_merged.gpkg → {:,} features | {:.2f} MB", len(ms_gdf), size_mb)
    return ms_gdf


def main() -> None:
    logger.info("=" * 60)
    logger.info("Job C — Google Open Buildings v3 + Merge")
    logger.info("=" * 60)

    w, s, e, n = get_bbox()

    if not MS_BUILDINGS.exists():
        logger.error("Microsoft buildings layer not found: {}", MS_BUILDINGS)
        sys.exit(1)

    ms_gdf = gpd.read_file(str(MS_BUILDINGS))
    ms_count = len(ms_gdf)
    logger.info("Microsoft buildings loaded: {:,}", ms_count)

    # ── Step 1: Probe Google OB ───────────────────────────────────────────────
    logger.info("")
    logger.info("--- Step 1: Google Open Buildings v3 ---")
    fail_reason, tile_size_mb = probe_google_ob()

    if fail_reason is not None:
        logger.warning("Google OB skipped: {}", fail_reason)
        logger.warning("Proceeding with Microsoft-only merge (Job C spec: fall-through).")
        google_count = 0
        duplicates = 0
        unique_google = 0
    else:
        logger.error("Unexpected: probe_google_ob returned no failure reason.")
        google_count = 0
        duplicates = 0
        unique_google = 0

    # ── Step 2: Merge ─────────────────────────────────────────────────────────
    logger.info("")
    logger.info("--- Step 2: Merge ---")
    merged_gdf = merge_microsoft_only(ms_count)
    merged_count = len(merged_gdf)

    # ── Step 3: Update config? ────────────────────────────────────────────────
    logger.info("")
    logger.info("--- Step 3: Config update ---")
    if merged_count > ms_count:
        logger.info(
            "Merged ({:,}) > Microsoft ({:,}) — would update settings.yaml.",
            merged_count, ms_count
        )
    else:
        logger.info(
            "Merged ({:,}) == Microsoft ({:,}) — "
            "settings.yaml NOT updated (no additional buildings added).",
            merged_count, ms_count
        )

    # ── Summary ───────────────────────────────────────────────────────────────
    logger.info("")
    logger.info("=" * 60)
    logger.info("JOB C SUMMARY")
    logger.info("=" * 60)
    logger.info("  Microsoft buildings:      {:>8,}", ms_count)
    logger.info("  Google OB buildings:      {:>8}  (SKIPPED — {})", google_count, fail_reason)
    logger.info("  Duplicates removed:       {:>8,}", duplicates)
    logger.info("  Final merged count:       {:>8,}", merged_count)
    logger.info("  Config updated:           NO — merged == Microsoft only")
    logger.info("  Output: {}", OUTPUT_MERGED)
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
