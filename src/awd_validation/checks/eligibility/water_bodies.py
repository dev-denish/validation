"""
Phase 2B — Permanent Water Body Overlap Check

Checks whether AWD plots overlap with permanent water bodies
(rivers, lakes, reservoirs > 0.5 ha).

BLOCKED until water bodies layer is received from Jibotosh.
When enabled: set phase2.water_bodies.enabled = true
              set phase2.water_bodies.layer_path in settings.yaml
"""
from __future__ import annotations

import glob
import json
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

import geopandas as gpd
import pandas as pd
from loguru import logger
from shapely.ops import unary_union
from shapely.validation import make_valid


def _derive_jrc_status(polygons_count: int, raster_stats: Optional[dict]) -> str:
    """Map raster stats + polygon count to a human-readable JRC status string."""
    if raster_stats is None:
        return "UNKNOWN"
    result = raster_stats.get("result", "")
    if "CONFIRMED_NO_PERMANENT_WATER" in result:
        return "CONFIRMED_NO_WATER"
    if polygons_count > 0:
        return "WATER_FOUND"
    return "UNKNOWN"


class WaterBodyChecker:
    """
    Phase 2B — Permanent water body overlap check.

    Input:  2A_plots_forest_checked_{run_id}.gpkg  (interim)
    Output: 2B_plots_water_checked_{run_id}.gpkg   (interim)
            2B_water_report_{run_id}.json           (outputs)
            2B_water_report_{run_id}.csv            (outputs)
    """

    WORKING_EPSG = 32643
    OUTPUT_EPSG = 4326

    def __init__(self, config) -> None:
        self.config = config
        self.interim_dir = Path(config.input["interim_dir"])
        self.outputs_dir = Path(config.input["outputs_dir"])
        self.phase2_cfg = config.get("phase2", default={})
        self.water_cfg = self.phase2_cfg.get("water_bodies", {})
        self.enabled = self.water_cfg.get("enabled", False)
        self.layer_path = self.water_cfg.get("layer_path")
        self.min_area_ha = self.water_cfg.get("min_area_ha", 0.5)
        self.jrc_layer_path = self.water_cfg.get("jrc_layer_path")
        self.jrc_enabled = self.water_cfg.get("jrc_enabled", False)

    def run(self, run_id: str) -> Tuple[gpd.GeoDataFrame, dict]:
        logger.info("=" * 60)
        logger.info("PHASE 2B — WATER BODY OVERLAP CHECK")
        logger.info("=" * 60)

        report: dict = {
            "run_id": run_id,
            "phase": "2B",
            "timestamp": datetime.utcnow().isoformat(),
            "enabled": self.enabled,
            "min_area_ha": self.min_area_ha,
            "checks": {},
        }

        gdf = self._load_input(run_id, report)

        if not self.enabled:
            self._log_blocked()
            gdf = self._apply_skipped_status(gdf)
            report["checks"]["water_overlap"] = {
                "status": "SKIPPED",
                "reason": (
                    "BLOCKED — awaiting water bodies / wetlands GIS layer "
                    "from Jibotosh. Set phase2.water_bodies.enabled=true "
                    "and phase2.water_bodies.layer_path in settings.yaml."
                ),
            }
        else:
            water_osm = self._load_water_layer(report)
            water_jrc = self._load_jrc_layer(report)
            gdf = self._check_water_overlap(gdf, water_osm, water_jrc, report)

        self._write_output(gdf, run_id)
        self._write_json_report(report, run_id)
        self._write_csv_report(gdf, run_id)
        self._log_summary(gdf, report)

        return gdf, report

    def _load_input(self, run_id: str, report: dict) -> gpd.GeoDataFrame:
        pattern = str(self.interim_dir / "2A_plots_forest_checked_*.gpkg")
        files = sorted(glob.glob(pattern))
        if not files:
            raise FileNotFoundError(
                "No Phase 2A output found. Run Phase 2A first."
            )
        latest = files[-1]
        logger.info(f"Loading Phase 2A output: {Path(latest).name}")
        gdf = gpd.read_file(latest)
        if gdf.crs.to_epsg() != self.WORKING_EPSG:
            gdf = gdf.to_crs(epsg=self.WORKING_EPSG)
        logger.info(f"Loaded {len(gdf):,} plots")
        report["input_file"] = latest
        report["total_plots"] = len(gdf)
        return gdf

    def _load_water_layer(self, report: dict) -> gpd.GeoDataFrame:
        if not self.layer_path:
            raise ValueError(
                "phase2.water_bodies.layer_path is not set in settings.yaml."
            )
        layer_path = Path(self.layer_path)
        if not layer_path.exists():
            raise FileNotFoundError(f"Water body layer not found: {layer_path}")
        logger.info(f"Loading water body layer: {layer_path.name}")
        water_gdf = gpd.read_file(layer_path)
        if water_gdf.crs.to_epsg() != self.WORKING_EPSG:
            water_gdf = water_gdf.to_crs(epsg=self.WORKING_EPSG)
        invalid = (~water_gdf.geometry.is_valid).sum()
        if invalid > 0:
            water_gdf["geometry"] = water_gdf.geometry.apply(make_valid)
        # Filter: only permanent water bodies > min_area_ha
        water_gdf["layer_area_ha"] = water_gdf.geometry.area / 10000
        before = len(water_gdf)
        water_gdf = water_gdf[
            water_gdf["layer_area_ha"] >= self.min_area_ha
        ].copy()
        logger.info(
            f"Water layer: {len(water_gdf):,} polygons "
            f"(filtered {before - len(water_gdf)} below {self.min_area_ha} ha)"
        )
        report["water_layer"] = {
            "path": str(layer_path),
            "polygons_after_filter": len(water_gdf),
            "min_area_filter_ha": self.min_area_ha,
        }
        return water_gdf

    def _load_jrc_layer(self, report: dict) -> Optional[gpd.GeoDataFrame]:
        if not self.jrc_enabled or not self.jrc_layer_path:
            return None
        jrc_path = Path(self.jrc_layer_path)
        if not jrc_path.exists():
            logger.warning(
                f"JRC water layer not found: {jrc_path} — skipping JRC, OSM-only."
            )
            report["jrc_layer"] = {"status": "NOT_FOUND", "path": str(jrc_path)}
            return None
        logger.info(f"Loading JRC permanent water layer: {jrc_path.name}")
        jrc_gdf = gpd.read_file(jrc_path)
        if len(jrc_gdf) > 0 and jrc_gdf.crs.to_epsg() != self.WORKING_EPSG:
            jrc_gdf = jrc_gdf.to_crs(epsg=self.WORKING_EPSG)
        if len(jrc_gdf) > 0:
            invalid = (~jrc_gdf.geometry.is_valid).sum()
            if invalid > 0:
                jrc_gdf["geometry"] = jrc_gdf.geometry.apply(make_valid)
            jrc_gdf["layer_area_ha"] = jrc_gdf.geometry.area / 10_000
            before = len(jrc_gdf)
            jrc_gdf = jrc_gdf[jrc_gdf["layer_area_ha"] >= self.min_area_ha].copy()
        else:
            before = 0

        # Load raster stats sidecar written by download_jrc_water.py.
        stats_path = jrc_path.parent / "jrc_occurrence_stats.json"
        raster_stats: Optional[dict] = None
        if stats_path.exists():
            with open(stats_path) as fh:
                raster_stats = json.load(fh)

        jrc_status = _derive_jrc_status(len(jrc_gdf), raster_stats)

        if jrc_status == "CONFIRMED_NO_WATER" and raster_stats:
            logger.info(
                "JRC water check: CONFIRMED — 0 of {:,} surveyed pixels meet >={}% "
                "occurrence threshold (region has no JRC-tracked permanent water)",
                raster_stats["surveyed_pixels"],
                self.water_cfg.get("jrc_occurrence_threshold", 75),
            )
        else:
            logger.info(
                f"JRC layer: {len(jrc_gdf):,} polygons "
                f"(filtered {before - len(jrc_gdf)} below {self.min_area_ha} ha) "
                f"— status: {jrc_status}"
            )

        jrc_block: dict = {
            "path": str(jrc_path),
            "polygons_after_filter": len(jrc_gdf),
            "min_area_filter_ha": self.min_area_ha,
            "occurrence_threshold": self.water_cfg.get("jrc_occurrence_threshold", 75),
            "jrc_status": jrc_status,
            "raster_stats": raster_stats,
        }
        if raster_stats is None:
            jrc_block["raster_stats_warning"] = (
                f"{stats_path.name} not found — re-run download_jrc_water.py to generate"
            )
        report["jrc_layer"] = jrc_block
        return jrc_gdf

    def _check_water_overlap(
        self,
        plots: gpd.GeoDataFrame,
        water_osm: Optional[gpd.GeoDataFrame],
        water_jrc: Optional[gpd.GeoDataFrame],
        report: dict,
    ) -> gpd.GeoDataFrame:
        logger.info("Calculating water body overlap (OSM + JRC union) ...")
        plots = plots.copy()
        plots["chk_water_overlap"] = False
        plots["water_overlap_ha"] = 0.0
        plots["water_overlap_pct"] = 0.0
        plots["water_overlap_source"] = "None"

        # Build combined layer with source tag
        layer_gdfs = []
        if water_osm is not None and len(water_osm) > 0:
            osm = water_osm[["geometry"]].copy()
            osm["_src"] = "OSM"
            layer_gdfs.append(osm)
        if water_jrc is not None and len(water_jrc) > 0:
            jrc = water_jrc[["geometry"]].copy()
            jrc["_src"] = "JRC"
            layer_gdfs.append(jrc)

        if not layer_gdfs:
            logger.info("No water polygons from either source — all plots PASS")
            plots["phase_2b_status"] = "PASS"
            report["checks"]["water_overlap"] = {
                "status": "PASS",
                "plots_with_overlap": 0,
                "total_overlap_ha": 0.0,
                "source_breakdown": {"OSM": 0, "JRC": 0, "Both": 0},
            }
            return plots

        combined = gpd.GeoDataFrame(
            pd.concat(layer_gdfs, ignore_index=True), crs=layer_gdfs[0].crs
        )

        joined = gpd.sjoin(
            plots[["plot_id", "area_ha", "geometry"]],
            combined[["_src", "geometry"]],
            how="inner",
            predicate="intersects",
        )

        if len(joined) == 0:
            logger.info("No water body overlaps found")
            plots["phase_2b_status"] = "PASS"
            report["checks"]["water_overlap"] = {
                "status": "PASS",
                "plots_with_overlap": 0,
                "total_overlap_ha": 0.0,
                "source_breakdown": {"OSM": 0, "JRC": 0, "Both": 0},
            }
            return plots

        source_counts: dict[str, int] = {"OSM": 0, "JRC": 0, "Both": 0}

        for plot_id, group in joined.groupby("plot_id"):
            plot_geom = plots.loc[plots["plot_id"] == plot_id, "geometry"].values[0]
            intersection_geoms = []
            sources_hit: set[str] = set()

            for idx in group["index_right"]:
                water_geom = combined.loc[idx, "geometry"]
                src = combined.loc[idx, "_src"]
                inter = plot_geom.intersection(water_geom)
                if not inter.is_empty and inter.area > 0:
                    intersection_geoms.append(inter)
                    sources_hit.add(src)

            if not intersection_geoms:
                continue

            # Union avoids double-counting where OSM and JRC polygons coincide
            union_geom = unary_union(intersection_geoms)
            overlap_ha = union_geom.area / 10_000

            if "OSM" in sources_hit and "JRC" in sources_hit:
                source = "Both"
            elif "OSM" in sources_hit:
                source = "OSM"
            else:
                source = "JRC"

            source_counts[source] += 1

            mask = plots["plot_id"] == plot_id
            plot_area_ha = plots.loc[mask, "area_ha"].values[0]
            pct = (overlap_ha / plot_area_ha * 100) if plot_area_ha > 0 else 0.0
            plots.loc[mask, "water_overlap_ha"] = round(overlap_ha, 6)
            plots.loc[mask, "water_overlap_pct"] = round(pct, 2)
            plots.loc[mask, "chk_water_overlap"] = True
            plots.loc[mask, "water_overlap_source"] = source

        plots_with_overlap = int(plots["chk_water_overlap"].sum())
        total_overlap_ha = round(float(plots["water_overlap_ha"].sum()), 4)
        logger.info(
            f"Water overlap — {plots_with_overlap:,} plots | {total_overlap_ha:,} ha total"
        )
        logger.info(
            f"  OSM-only: {source_counts['OSM']:,} | "
            f"JRC-only: {source_counts['JRC']:,} | "
            f"Both: {source_counts['Both']:,}"
        )

        plots["phase_2b_status"] = plots["chk_water_overlap"].apply(
            lambda x: "FAIL" if x else "PASS"
        )
        report["checks"]["water_overlap"] = {
            "status": "PASS" if plots_with_overlap == 0 else "FAIL",
            "plots_with_overlap": plots_with_overlap,
            "total_overlap_ha": total_overlap_ha,
            "source_breakdown": source_counts,
        }
        return plots

    def _apply_skipped_status(self, gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
        gdf = gdf.copy()
        gdf["chk_water_overlap"] = False
        gdf["water_overlap_ha"] = 0.0
        gdf["water_overlap_pct"] = 0.0
        gdf["water_overlap_source"] = "None"
        gdf["phase_2b_status"] = "SKIPPED"
        return gdf

    def _log_blocked(self) -> None:
        logger.warning("PHASE 2B — BLOCKED: awaiting water body layer from Jibotosh")

    def _write_output(self, gdf: gpd.GeoDataFrame, run_id: str) -> None:
        self.interim_dir.mkdir(parents=True, exist_ok=True)
        path = self.interim_dir / f"2B_plots_water_checked_{run_id}.gpkg"
        gdf.to_crs(epsg=self.OUTPUT_EPSG).to_file(
            path, driver="GPKG", layer="plots_water_checked"
        )
        logger.info(f"Output written: {path.name}")

    def _write_json_report(self, report: dict, run_id: str) -> None:
        self.outputs_dir.mkdir(parents=True, exist_ok=True)
        path = self.outputs_dir / f"2B_water_report_{run_id}.json"
        with open(path, "w") as f:
            json.dump(report, f, indent=2, default=str)

    def _write_csv_report(self, gdf: gpd.GeoDataFrame, run_id: str) -> None:
        cols = [
            "plot_id", "area_ha", "phase_2b_status",
            "chk_water_overlap", "water_overlap_ha", "water_overlap_pct",
            "water_overlap_source",
        ]
        available = [c for c in cols if c in gdf.columns]
        path = self.outputs_dir / f"2B_water_report_{run_id}.csv"
        gdf[available].to_csv(path, index=False, encoding="utf-8")

    def _log_summary(self, gdf: gpd.GeoDataFrame, report: dict) -> None:
        status_counts = (
            gdf["phase_2b_status"].value_counts().to_dict()
            if "phase_2b_status" in gdf.columns else {}
        )
        logger.info("=" * 60)
        logger.info("PHASE 2B SUMMARY")
        logger.info("=" * 60)
        logger.info(f"  Enabled: {self.enabled}")
        for status, count in sorted(status_counts.items()):
            logger.info(f"  {status:<15} {count:>8,} plots")
        logger.info("=" * 60)
