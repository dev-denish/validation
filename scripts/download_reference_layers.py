"""Download and clip three reference layers to the AWD plot extent.

Layers:
  1. Microsoft Global ML Building Footprints  → data/raw/buildings.gpkg
  2. GFW Hansen tree cover 2000 (raster)      → data/raw/treecover_hansen_2000.tif
  3. WRIS water bodies (WFS)                  → data/raw/wris_water_bodies.gpkg

All clipped to: latest valid_plots_*.gpkg bounding box + 0.05° buffer.
Run inside Docker where /app maps to the project root.
"""
from __future__ import annotations

import gzip
import io
import json
import math
import subprocess
import sys
from pathlib import Path
from typing import Optional

import geopandas as gpd
import pandas as pd
import requests
from loguru import logger
from shapely.geometry import box, shape

RAW_DIR = Path("/app/data/raw")
OUTPUTS_DIR = Path("/app/data/outputs")
BUFFER_DEG = 0.05

# GFW Hansen GCS tile that covers 20–30°N, 80–90°E
GFW_TILE_URL = (
    "https://storage.googleapis.com/earthenginepartners-hansen/"
    "GFC-2023-v1.11/Hansen_GFC-2023-v1.11_treecover2000_30N_080E.tif"
)

# Microsoft Global ML Building Footprints index
MS_INDEX_URL = (
    "https://minedbuildings.z5.web.core.windows.net/"
    "global-buildings/dataset-links.csv"
)

# WRIS India WFS endpoint (National Water Informatics Centre)
WRIS_WFS_URL = (
    "https://indiawris.gov.in/wris/rest/services/NWIC/WaterBodies/MapServer/WFSServer"
)
WRIS_WFS_PARAMS = {
    "service": "WFS",
    "version": "2.0.0",
    "request": "GetFeature",
    "typeName": "WaterBodies",
    "outputFormat": "application/json",
    "count": "50000",
}


# ── BBOX ─────────────────────────────────────────────────────────────────────

def get_bbox() -> tuple[float, float, float, float]:
    files = sorted(OUTPUTS_DIR.glob("valid_plots_*.gpkg"))
    if not files:
        logger.error("No valid_plots_*.gpkg found")
        sys.exit(1)
    gdf = gpd.read_file(str(files[-1]))
    minx, miny, maxx, maxy = gdf.total_bounds
    w, s, e, n = minx - BUFFER_DEG, miny - BUFFER_DEG, maxx + BUFFER_DEG, maxy + BUFFER_DEG
    logger.info("AWD bbox (+{:.2f}°): W={:.6f} S={:.6f} E={:.6f} N={:.6f}",
                BUFFER_DEG, w, s, e, n)
    return w, s, e, n


# ── QUADKEY HELPERS (no external dep) ────────────────────────────────────────

def _quadkey_to_bounds(qk: str) -> tuple[float, float, float, float]:
    """Return (west, south, east, north) for a Web Mercator quadkey."""
    x, y, zoom = 0, 0, len(qk)
    for i, ch in enumerate(qk):
        mask = 1 << (zoom - 1 - i)
        if ch in ("1", "3"):
            x |= mask
        if ch in ("2", "3"):
            y |= mask
    n_tiles = 2 ** zoom
    lon_w = x / n_tiles * 360 - 180
    lon_e = (x + 1) / n_tiles * 360 - 180
    lat_n = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n_tiles))))
    lat_s = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 1) / n_tiles))))
    return lon_w, lat_s, lon_e, lat_n


def _tiles_intersect(qk: str, w: float, s: float, e: float, n: float) -> bool:
    tw, ts, te, tn = _quadkey_to_bounds(qk)
    return not (te < w or tw > e or tn < s or ts > n)


# ── 1. MICROSOFT BUILDING FOOTPRINTS ─────────────────────────────────────────

def download_buildings(w: float, s: float, e: float, n: float) -> None:
    out = RAW_DIR / "buildings.gpkg"
    logger.info("=== [1/3] Microsoft Building Footprints ===")

    logger.info("Fetching dataset index …")
    try:
        resp = requests.get(MS_INDEX_URL, timeout=30)
        resp.raise_for_status()
    except Exception as exc:
        logger.error("Failed to fetch MS index: {}", exc)
        return

    df = pd.read_csv(io.StringIO(resp.text))
    india = df[df["Location"].str.strip() == "India"].copy()
    logger.info("India tiles in index: {:,}", len(india))

    # Filter to tiles that intersect the bbox
    india["intersects"] = india["QuadKey"].astype(str).apply(
        lambda qk: _tiles_intersect(qk, w, s, e, n)
    )
    hits = india[india["intersects"]]
    logger.info("Tiles intersecting AWD bbox: {:,}", len(hits))

    if hits.empty:
        logger.warning("No Microsoft building tiles found for this extent.")
        return

    aoi = box(w, s, e, n)
    all_features: list[dict] = []

    for _, row in hits.iterrows():
        url = row["Url"]
        qk = row["QuadKey"]
        logger.info("  Downloading tile {} ({}) …", qk, row["Size"])
        try:
            tile_resp = requests.get(url, timeout=120)
            tile_resp.raise_for_status()
            # Tiles are GeoJSONL lines compressed as .csv.gz
            raw = gzip.decompress(tile_resp.content).decode("utf-8")
            kept = 0
            for line in raw.splitlines():
                line = line.strip()
                if not line:
                    continue
                feat = json.loads(line)
                geom = shape(feat["geometry"])
                if geom.intersects(aoi):
                    all_features.append(feat)
                    kept += 1
            logger.info("    tile {}: {:,} raw → {:,} in bbox", qk,
                        len(raw.splitlines()), kept)
        except Exception as exc:
            logger.warning("    tile {} failed: {}", qk, exc)

    if not all_features:
        logger.warning("No building features found within AOI after clipping.")
        return

    gdf = gpd.GeoDataFrame.from_features(all_features, crs="EPSG:4326")
    if out.exists():
        out.unlink()
    gdf.to_file(str(out), driver="GPKG")
    size_mb = out.stat().st_size / 1_048_576
    logger.success("buildings.gpkg → {:,} features | {:.2f} MB", len(gdf), size_mb)


# ── 2. GFW HANSEN TREE COVER ─────────────────────────────────────────────────

def download_hansen(w: float, s: float, e: float, n: float) -> None:
    out = RAW_DIR / "treecover_hansen_2000.tif"
    logger.info("=== [2/3] GFW Hansen Tree Cover 2000 ===")
    logger.info("Source tile: 30N_080E (covers 20–30°N, 80–90°E)")
    logger.info("Clipping via GDAL vsicurl — no full-tile download needed …")

    if out.exists():
        out.unlink()

    cmd = [
        "gdal_translate",
        "-projwin", str(w), str(n), str(e), str(s),   # west north east south
        "-of", "GTiff",
        "-co", "COMPRESS=LZW",
        "-co", "TILED=YES",
        f"/vsicurl/{GFW_TILE_URL}",
        str(out),
    ]
    logger.info("Running: {}", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)

    if result.returncode != 0:
        logger.error("gdal_translate failed:\n{}", result.stderr)
        return

    if not out.exists() or out.stat().st_size == 0:
        logger.error("Output file not created or empty.")
        return

    size_mb = out.stat().st_size / 1_048_576

    # Report raster dimensions and pixel statistics
    info_cmd = ["gdalinfo", "-mm", str(out)]
    info = subprocess.run(info_cmd, capture_output=True, text=True)
    lines = info.stdout.splitlines()
    size_line = next((l for l in lines if "Size is" in l), "")
    min_max = next((l for l in lines if "Computed Min/Max" in l), "")

    logger.success("treecover_hansen_2000.tif → {:.2f} MB", size_mb)
    logger.success("  Raster dimensions: {}", size_line.strip())
    logger.success("  Pixel values (% canopy cover 0–100): {}", min_max.strip())
    logger.info(
        "  NOTE: raster — no feature count. "
        "Vectorise with gdal_polygonize if needed for Phase 2A."
    )


# ── 3. WRIS WATER BODIES ─────────────────────────────────────────────────────

def download_wris(w: float, s: float, e: float, n: float) -> None:
    out = RAW_DIR / "wris_water_bodies.gpkg"
    logger.info("=== [3/3] WRIS Water Bodies (WFS) ===")

    bbox_str = f"{s},{w},{n},{e}"   # WFS 2.0 uses lat/lon order
    params = {**WRIS_WFS_PARAMS, "BBOX": bbox_str}

    logger.info("Querying WRIS WFS endpoint …")
    try:
        resp = requests.get(WRIS_WFS_URL, params=params, timeout=60)
        resp.raise_for_status()
        ct = resp.headers.get("Content-Type", "")
        if "json" not in ct and "xml" not in ct:
            raise ValueError(f"Unexpected content-type: {ct}")
        data = resp.json()
        features = data.get("features", [])
        if not features:
            logger.warning("WRIS WFS returned 0 features for this bbox.")
            return
        gdf = gpd.GeoDataFrame.from_features(features, crs="EPSG:4326")
        if out.exists():
            out.unlink()
        gdf.to_file(str(out), driver="GPKG")
        size_mb = out.stat().st_size / 1_048_576
        logger.success("wris_water_bodies.gpkg → {:,} features | {:.2f} MB", len(gdf), size_mb)

    except requests.exceptions.ConnectionError:
        logger.error("WRIS WFS: connection refused — endpoint may require VPN/auth or be offline.")
    except requests.exceptions.Timeout:
        logger.error("WRIS WFS: request timed out.")
    except requests.exceptions.HTTPError as exc:
        logger.error("WRIS WFS: HTTP {} — {}", exc.response.status_code, exc.response.reason)
        _try_wris_fallback(w, s, e, n, out)
    except Exception as exc:
        logger.error("WRIS WFS: unexpected error — {}", exc)
        _try_wris_fallback(w, s, e, n, out)


def _try_wris_fallback(w: float, s: float, e: float, n: float, out: Path) -> None:
    """Try alternate WRIS REST endpoint."""
    logger.info("Trying WRIS ArcGIS REST fallback …")
    url = (
        "https://indiawris.gov.in/wris/rest/services/NWIC/WaterBodies"
        "/MapServer/0/query"
    )
    params = {
        "geometry": f"{w},{s},{e},{n}",
        "geometryType": "esriGeometryEnvelope",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "*",
        "f": "geojson",
        "returnGeometry": "true",
    }
    try:
        resp = requests.get(url, params=params, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        features = data.get("features", [])
        if not features:
            logger.warning("WRIS REST fallback: 0 features returned.")
            return
        gdf = gpd.GeoDataFrame.from_features(features, crs="EPSG:4326")
        if out.exists():
            out.unlink()
        gdf.to_file(str(out), driver="GPKG")
        size_mb = out.stat().st_size / 1_048_576
        logger.success("wris_water_bodies.gpkg (REST) → {:,} features | {:.2f} MB",
                        len(gdf), size_mb)
    except Exception as exc:
        logger.error("WRIS REST fallback also failed: {}", exc)
        logger.warning(
            "WRIS data requires manual download from https://indiawris.gov.in — "
            "authentication or a direct shapefile download from their portal is needed."
        )


# ── MAIN ─────────────────────────────────────────────────────────────────────

def main() -> None:
    logger.info("=== Download Reference Layers ===")
    w, s, e, n = get_bbox()

    download_buildings(w, s, e, n)
    download_hansen(w, s, e, n)
    download_wris(w, s, e, n)

    logger.info("=== Summary ===")
    for path in [
        RAW_DIR / "buildings.gpkg",
        RAW_DIR / "treecover_hansen_2000.tif",
        RAW_DIR / "wris_water_bodies.gpkg",
    ]:
        if path.exists():
            mb = path.stat().st_size / 1_048_576
            logger.success("  {} — {:.2f} MB", path.name, mb)
        else:
            logger.warning("  {} — NOT CREATED", path.name)


if __name__ == "__main__":
    main()
