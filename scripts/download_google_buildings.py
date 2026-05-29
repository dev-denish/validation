"""
Job C — Google Open Buildings v3 via GEE + Merge with Microsoft buildings.

Strategy:
  - GEE asset: GOOGLE/Research/open-buildings/v3/polygons
  - Filter server-side: .filterBounds(bbox) + confidence >= 0.65
  - Paginate with 4×4 spatial tiles to avoid O(n²) offset degradation
    (444K features in bbox — direct offset pagination to row 400K is very slow)
  - Each tile: toList(BATCH, offset) loop until empty
  - Deduplicate against Microsoft buildings via IoU >= 0.50
  - Merge → data/raw/buildings_merged.gpkg

Usage (inside Docker):
  docker compose run --rm awd-validation python scripts/download_google_buildings.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Optional

import ee
import geopandas as gpd
import pandas as pd
from loguru import logger
from shapely.geometry import shape
from shapely.ops import unary_union
from shapely.strtree import STRtree
from shapely.validation import make_valid

RAW_DIR = Path("/app/data/raw")
OUTPUTS_DIR = Path("/app/data/outputs")
BUFFER_DEG = 0.05

CONFIDENCE_THRESHOLD = 0.65
IOU_THRESHOLD = 0.50
BATCH_SIZE = 2000        # features per GEE getInfo call
GRID_N = 4              # 4×4 spatial tiles = 16 sub-queries
WORKING_EPSG = 32643

MS_BUILDINGS = RAW_DIR / "buildings.gpkg"
OUTPUT_GOOGLE = RAW_DIR / "buildings_google.gpkg"
OUTPUT_MERGED = RAW_DIR / "buildings_merged.gpkg"
GEE_ASSET = "GOOGLE/Research/open-buildings/v3/polygons"


# ── BBOX ─────────────────────────────────────────────────────────────────────

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


# ── GEE DOWNLOAD ─────────────────────────────────────────────────────────────

def _tile_cells(
    w: float, s: float, e: float, n: float, grid_n: int
) -> list[tuple[float, float, float, float]]:
    """Split bbox into grid_n × grid_n sub-tiles."""
    xs = [w + (e - w) * i / grid_n for i in range(grid_n + 1)]
    ys = [s + (n - s) * j / grid_n for j in range(grid_n + 1)]
    return [
        (xs[i], ys[j], xs[i + 1], ys[j + 1])
        for j in range(grid_n)
        for i in range(grid_n)
    ]


def _download_tile(
    fc: ee.FeatureCollection,
    tw: float, ts: float, te: float, tn: float,
    tile_idx: int,
    total_tiles: int,
) -> list[dict]:
    """Download one spatial tile via paginated toList calls."""
    tile_bbox = ee.Geometry.Rectangle([tw, ts, te, tn])
    tile_fc = fc.filterBounds(tile_bbox)

    try:
        tile_count = tile_fc.size().getInfo()
    except Exception as exc:
        logger.warning("Tile {}/{}: size() failed — {}", tile_idx, total_tiles, exc)
        return []

    if tile_count == 0:
        return []

    logger.info(
        "Tile {}/{}: {:,} features  (W={:.4f} S={:.4f} E={:.4f} N={:.4f})",
        tile_idx, total_tiles, tile_count, tw, ts, te, tn,
    )

    features: list[dict] = []
    offset = 0
    retries = 0

    while True:
        try:
            batch = tile_fc.toList(BATCH_SIZE, offset).getInfo()
        except Exception as exc:
            if retries < 3:
                retries += 1
                logger.warning("  offset={}: retrying ({}/3) — {}", offset, retries, exc)
                time.sleep(5 * retries)
                continue
            logger.error("  offset={}: failed after 3 retries — skipping rest of tile.", offset)
            break

        if not batch:
            break

        features.extend(batch)
        offset += len(batch)
        retries = 0

        if offset < tile_count:
            logger.info("  fetched {:,}/{:,} ...", offset, tile_count)

        if len(batch) < BATCH_SIZE:
            break  # last partial page

    logger.info("  tile {}/{}: {:,} features downloaded", tile_idx, total_tiles, len(features))
    return features


def download_google_buildings(
    w: float, s: float, e: float, n: float
) -> Optional[gpd.GeoDataFrame]:
    """Download Google Open Buildings v3 for bbox via GEE. Returns GeoDataFrame or None."""
    logger.info("=== Step 1: Google Open Buildings v3 via GEE ===")
    logger.info("  Asset:      {}", GEE_ASSET)
    logger.info("  Confidence: >= {}", CONFIDENCE_THRESHOLD)
    logger.info("  Grid:       {}×{} tiles", GRID_N, GRID_N)

    try:
        ee.Initialize()
        logger.info("  GEE Initialize: OK")
    except Exception as exc:
        logger.error("GEE Initialize failed: {} — aborting Google OB step.", exc)
        return None

    bbox = ee.Geometry.Rectangle([w, s, e, n])
    fc = (
        ee.FeatureCollection(GEE_ASSET)
        .filterBounds(bbox)
        .filter(ee.Filter.gte("confidence", CONFIDENCE_THRESHOLD))
    )

    # Total count (server-side)
    try:
        total_count = fc.size().getInfo()
    except Exception as exc:
        logger.error("GEE size() failed: {}", exc)
        return None
    logger.info("  Server-side count (confidence >= {}): {:,}", CONFIDENCE_THRESHOLD, total_count)

    if total_count == 0:
        logger.warning("No Google OB features found for this bbox.")
        return None

    # Paginate over 4×4 spatial tiles
    tiles = _tile_cells(w, s, e, n, GRID_N)
    all_features: list[dict] = []
    t0 = time.time()

    for i, (tw, ts, te, tn) in enumerate(tiles, start=1):
        feats = _download_tile(fc, tw, ts, te, tn, i, len(tiles))
        all_features.extend(feats)

    elapsed = round(time.time() - t0, 1)
    logger.info("Download complete: {:,} raw features in {:.1f}s", len(all_features), elapsed)

    if not all_features:
        logger.warning("No features collected after paginating.")
        return None

    # Convert to GeoDataFrame
    logger.info("Converting {:,} GEE features to GeoDataFrame ...", len(all_features))
    try:
        gdf = gpd.GeoDataFrame.from_features(all_features, crs="EPSG:4326")
    except Exception as exc:
        logger.error("from_features failed: {}", exc)
        return None

    # Drop duplicates from tile boundaries (same feature in two adjacent tiles)
    before = len(gdf)
    if "id" in gdf.columns:
        gdf = gdf.drop_duplicates(subset=["id"]).copy()
    else:
        # Use geometry WKT as dedup key
        gdf["_wkt"] = gdf.geometry.apply(lambda g: g.wkt if g else "")
        gdf = gdf.drop_duplicates(subset=["_wkt"]).drop(columns=["_wkt"]).copy()
    logger.info(
        "Tile-boundary dedup: {:,} → {:,} (removed {:,})",
        before, len(gdf), before - len(gdf),
    )

    # Confidence summary
    if "confidence" in gdf.columns:
        c = gdf["confidence"]
        logger.info(
            "Confidence: min={:.3f}  mean={:.3f}  max={:.3f}",
            c.min(), c.mean(), c.max(),
        )

    if OUTPUT_GOOGLE.exists():
        OUTPUT_GOOGLE.unlink()
    gdf.to_file(str(OUTPUT_GOOGLE), driver="GPKG", layer="buildings_google")
    size_mb = OUTPUT_GOOGLE.stat().st_size / 1_048_576
    logger.success("buildings_google.gpkg → {:,} features | {:.2f} MB", len(gdf), size_mb)
    return gdf


# ── MERGE + DEDUP ─────────────────────────────────────────────────────────────

def merge_and_dedup(
    google_gdf: Optional[gpd.GeoDataFrame],
) -> Optional[gpd.GeoDataFrame]:
    logger.info("=== Step 2: Merge Microsoft + Google buildings ===")

    if not MS_BUILDINGS.exists():
        logger.error("Microsoft buildings not found: {}", MS_BUILDINGS)
        return None

    ms_gdf = gpd.read_file(str(MS_BUILDINGS))
    ms_count = len(ms_gdf)
    logger.info("Microsoft buildings: {:,}", ms_count)

    if google_gdf is None or len(google_gdf) == 0:
        logger.warning("Google OB not available — merged = Microsoft-only.")
        if OUTPUT_MERGED.exists():
            OUTPUT_MERGED.unlink()
        ms_gdf.to_file(str(OUTPUT_MERGED), driver="GPKG", layer="buildings_merged")
        return ms_gdf

    google_count = len(google_gdf)
    logger.info("Google buildings (confidence >= {}): {:,}", CONFIDENCE_THRESHOLD, google_count)

    # Reproject both to metric CRS for accurate IoU
    ms_metric = ms_gdf.to_crs(epsg=WORKING_EPSG)
    google_metric = google_gdf.to_crs(epsg=WORKING_EPSG)

    # Fix any invalid geometries
    ms_geoms = [
        make_valid(g) if g and not g.is_valid else g
        for g in ms_metric.geometry
    ]
    google_geoms = [
        make_valid(g) if g and not g.is_valid else g
        for g in google_metric.geometry
    ]

    logger.info("Building STRtree on {:,} Microsoft polygons ...", ms_count)
    tree = STRtree(ms_geoms)

    logger.info("Deduplicating {:,} Google buildings (IoU >= {}) ...", google_count, IOU_THRESHOLD)
    t0 = time.time()
    keep_mask: list[bool] = []
    duplicates = 0

    for idx, g_geom in enumerate(google_geoms):
        if g_geom is None or g_geom.is_empty:
            keep_mask.append(False)
            duplicates += 1
            continue

        candidates = tree.query(g_geom, predicate="intersects")
        is_dup = False
        for ms_idx in candidates:
            m_geom = ms_geoms[ms_idx]
            if m_geom is None or m_geom.is_empty:
                continue
            try:
                inter_area = g_geom.intersection(m_geom).area
                union_area = g_geom.area + m_geom.area - inter_area
                if union_area > 0 and inter_area / union_area >= IOU_THRESHOLD:
                    is_dup = True
                    break
            except Exception:
                pass
        keep_mask.append(not is_dup)
        if is_dup:
            duplicates += 1

        if (idx + 1) % 50_000 == 0:
            logger.info("  dedup progress: {:,}/{:,} ({:.0f}%)", idx + 1, google_count, (idx + 1) / google_count * 100)

    elapsed = round(time.time() - t0, 1)
    google_unique_count = sum(keep_mask)
    logger.info(
        "Dedup done in {:.1f}s — {:,} duplicates removed, {:,} unique Google buildings kept",
        elapsed, duplicates, google_unique_count,
    )

    # Build merged GeoDataFrame (EPSG:4326)
    google_unique = google_gdf[keep_mask].copy()
    google_unique = google_unique.reset_index(drop=True)

    ms_out = ms_gdf[["geometry"]].copy()
    ms_out["source"] = "Microsoft"

    google_out = google_unique[["geometry"]].copy()
    if "confidence" in google_unique.columns:
        google_out["confidence"] = google_unique["confidence"].values
    google_out["source"] = "Google"

    merged = pd.concat([ms_out, google_out], ignore_index=True)
    merged = gpd.GeoDataFrame(merged, crs="EPSG:4326")

    if OUTPUT_MERGED.exists():
        OUTPUT_MERGED.unlink()
    merged.to_file(str(OUTPUT_MERGED), driver="GPKG", layer="buildings_merged")
    size_mb = OUTPUT_MERGED.stat().st_size / 1_048_576
    logger.success(
        "buildings_merged.gpkg → {:,} features | {:.2f} MB",
        len(merged), size_mb,
    )
    logger.success(
        "  Microsoft: {:,}  |  Google unique: {:,}  |  Duplicates removed: {:,}",
        ms_count, google_unique_count, duplicates,
    )
    return merged


# ── SETTINGS UPDATE ───────────────────────────────────────────────────────────

def update_settings(ms_count: int, merged_count: int) -> bool:
    """Update settings.yaml buildings_layer_path if merged > Microsoft alone."""
    if merged_count <= ms_count:
        logger.info(
            "Merged ({:,}) <= Microsoft ({:,}) — settings.yaml NOT updated.",
            merged_count, ms_count,
        )
        return False

    settings_path = Path("/app/config/settings.yaml")
    text = settings_path.read_text()
    old = 'buildings_layer_path: "/app/data/raw/buildings.gpkg"'
    new = 'buildings_layer_path: "/app/data/raw/buildings_merged.gpkg"'
    if old not in text:
        logger.warning("Could not find buildings_layer_path line to update.")
        return False
    settings_path.write_text(text.replace(old, new))
    logger.info("settings.yaml updated: buildings_layer_path → buildings_merged.gpkg")
    return True


# ── MAIN ─────────────────────────────────────────────────────────────────────

def main() -> None:
    logger.info("=" * 60)
    logger.info("Job C — Google Open Buildings v3 via GEE + Merge")
    logger.info("=" * 60)

    w, s, e, n = get_bbox()
    ms_count = len(gpd.read_file(str(MS_BUILDINGS))) if MS_BUILDINGS.exists() else 0
    logger.info("Microsoft buildings (pre-merge): {:,}", ms_count)

    # Step 1 — Download Google OB
    google_gdf = download_google_buildings(w, s, e, n)
    google_count = len(google_gdf) if google_gdf is not None else 0

    # Step 2 — Merge + Dedup
    logger.info("")
    merged_gdf = merge_and_dedup(google_gdf)
    merged_count = len(merged_gdf) if merged_gdf is not None else 0
    duplicates = ms_count + google_count - merged_count

    # Step 3 — Update config
    logger.info("")
    updated = update_settings(ms_count, merged_count)

    # Summary
    logger.info("")
    logger.info("=" * 60)
    logger.info("JOB C SUMMARY")
    logger.info("=" * 60)
    logger.info("  Microsoft buildings:      {:>10,}", ms_count)
    logger.info("  Google OB downloaded:     {:>10,}  (confidence >= {})", google_count, CONFIDENCE_THRESHOLD)
    logger.info("  Duplicates removed:       {:>10,}  (IoU >= {})", max(0, duplicates), IOU_THRESHOLD)
    logger.info("  Final merged count:       {:>10,}", merged_count)
    logger.info("  New buildings from GEE:   {:>10,}", merged_count - ms_count)
    logger.info("  Config updated:           {}", "YES → buildings_merged.gpkg" if updated else "NO")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
