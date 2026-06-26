"""
Phase 2C — Man-Made Non-Eligible Area Check

Checks AWD plots against infrastructure layers:
  - Roads:       5m buffer around centrelines
  - Railways:   10m buffer around centrelines
  - Settlements: direct intersection, no buffer
  - Buildings:   direct intersection, no buffer

BLOCKED until infrastructure layers are received from Jibotosh.
"""
from __future__ import annotations

import glob
import json
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

import geopandas as gpd
from loguru import logger
from shapely.validation import make_valid

from awd_validation.utils.config import compute_working_epsg


class InfrastructureChecker:
    """
    Phase 2C — Infrastructure overlap check.

    Input:  2B_plots_water_checked_{run_id}.gpkg    (interim)
    Output: 2C_plots_infra_checked_{run_id}.gpkg    (interim)
            2C_infra_report_{run_id}.json            (outputs)
            2C_infra_report_{run_id}.csv             (outputs)
    """

    # Infrastructure types with buffer distances
    INFRA_TYPES = {
        "roads":       {"buffer_key": "roads_buffer_m",       "layer_key": "roads_layer_path"},
        "railways":    {"buffer_key": "railways_buffer_m",     "layer_key": "railways_layer_path"},
        "settlements": {"buffer_key": "settlements_buffer_m",  "layer_key": "settlements_layer_path"},
        "buildings":   {"buffer_key": "buildings_buffer_m",    "layer_key": "buildings_layer_path"},
    }

    def __init__(self, config) -> None:
        self.config = config
        self.interim_dir = Path(config.input["interim_dir"])
        self.outputs_dir = Path(config.input["outputs_dir"])
        self.output_epsg = config.crs.get("output_epsg", 4326)
        self.fallback_epsg = config.crs.get("fallback_working_epsg", 32644)
        self.working_epsg: int = self.fallback_epsg  # set dynamically in _load_input
        self.phase2_cfg = config.get("phase2", default={})
        self.infra_cfg = self.phase2_cfg.get("infrastructure", {})
        self.enabled = self.infra_cfg.get("enabled", False)

    def run(self, run_id: str) -> Tuple[gpd.GeoDataFrame, dict]:
        logger.info("=" * 60)
        logger.info("PHASE 2C — INFRASTRUCTURE OVERLAP CHECK")
        logger.info("=" * 60)

        report: dict = {
            "run_id": run_id,
            "phase": "2C",
            "timestamp": datetime.utcnow().isoformat(),
            "enabled": self.enabled,
            "checks": {},
        }

        gdf = self._load_input(run_id, report)

        if not self.enabled:
            self._log_blocked()
            gdf = self._apply_skipped_status(gdf)
            report["checks"]["infrastructure_overlap"] = {
                "status": "SKIPPED",
                "reason": (
                    "BLOCKED — awaiting infrastructure GIS layers from Jibotosh "
                    "(roads, railways, settlements, buildings). "
                    "Set phase2.infrastructure.enabled=true and layer paths in settings.yaml."
                ),
                "buffer_rules": {
                    "roads_m": self.infra_cfg.get("roads_buffer_m", 5),
                    "railways_m": self.infra_cfg.get("railways_buffer_m", 10),
                    "settlements_m": 0,
                    "buildings_m": 0,
                },
            }
        else:
            for infra_type, keys in self.INFRA_TYPES.items():
                layer_path = self.infra_cfg.get(keys["layer_key"])
                buffer_m = self.infra_cfg.get(keys["buffer_key"], 0)
                if layer_path:
                    infra_layer = self._load_infra_layer(
                        layer_path, infra_type, buffer_m, report
                    )
                    if infra_layer is not None:
                        gdf = self._check_infra_overlap(
                            gdf, infra_layer, infra_type, buffer_m, report
                        )
                else:
                    logger.warning(
                        f"  {infra_type}: layer_path not set — skipping"
                    )

        self._write_output(gdf, run_id)
        self._write_json_report(report, run_id)
        self._write_csv_report(gdf, run_id)
        self._log_summary(gdf, report)

        return gdf, report

    def _load_input(self, run_id: str, report: dict) -> gpd.GeoDataFrame:
        pattern = str(self.interim_dir / "2B_plots_water_checked_*.gpkg")
        files = sorted(glob.glob(pattern))
        if not files:
            raise FileNotFoundError(
                "No Phase 2B output found. Run Phase 2B first."
            )
        latest = files[-1]
        logger.info(f"Loading Phase 2B output: {Path(latest).name}")
        gdf = gpd.read_file(latest)
        self.working_epsg = compute_working_epsg(gdf, self.fallback_epsg)
        if gdf.crs is None or gdf.crs.to_epsg() != self.working_epsg:
            gdf = gdf.to_crs(epsg=self.working_epsg)
        logger.info(f"Loaded {len(gdf):,} plots | working CRS: EPSG:{self.working_epsg}")
        report["input_file"] = latest
        report["total_plots"] = len(gdf)
        report["working_epsg"] = self.working_epsg
        return gdf

    def _load_infra_layer(
        self,
        layer_path: str,
        infra_type: str,
        buffer_m: float,
        report: dict,
    ) -> Optional[gpd.GeoDataFrame]:
        path = Path(layer_path)
        if not path.exists():
            logger.warning(f"  {infra_type}: layer not found at {path}")
            return None
        logger.info(f"Loading {infra_type} layer: {path.name}")
        layer = gpd.read_file(path)
        if layer.crs.to_epsg() != self.working_epsg:
            layer = layer.to_crs(epsg=self.working_epsg)
        invalid = (~layer.geometry.is_valid).sum()
        if invalid > 0:
            layer["geometry"] = layer.geometry.apply(make_valid)
        if buffer_m > 0:
            logger.info(f"  Applying {buffer_m}m buffer to {infra_type}...")
            layer = layer.copy()
            layer["geometry"] = layer.geometry.buffer(buffer_m)
        report[f"{infra_type}_layer"] = {
            "path": str(path),
            "features": len(layer),
            "buffer_m": buffer_m,
        }
        return layer

    def _check_infra_overlap(
        self,
        plots: gpd.GeoDataFrame,
        infra: gpd.GeoDataFrame,
        infra_type: str,
        buffer_m: float,
        report: dict,
    ) -> gpd.GeoDataFrame:
        logger.info(f"Checking {infra_type} overlap (buffer={buffer_m}m)...")
        plots = plots.copy()
        col_overlap = f"chk_{infra_type}_overlap"
        col_area = f"{infra_type}_overlap_ha"
        col_pct = f"{infra_type}_overlap_pct"

        if col_overlap not in plots.columns:
            plots[col_overlap] = False
            plots[col_area] = 0.0
            plots[col_pct] = 0.0

        joined = gpd.sjoin(
            plots[["plot_id", "area_ha", "geometry"]],
            infra[["geometry"]],
            how="inner",
            predicate="intersects",
        )

        if len(joined) == 0:
            logger.info(f"  No {infra_type} overlaps found")
            report["checks"][f"{infra_type}_overlap"] = {
                "status": "PASS",
                "plots_with_overlap": 0,
            }
            return plots

        for plot_id, group in joined.groupby("plot_id"):
            plot_geom = plots.loc[
                plots["plot_id"] == plot_id, "geometry"
            ].values[0]
            total_m2 = sum(
                plot_geom.intersection(infra.loc[idx, "geometry"]).area
                for idx in group.index_right
            )
            overlap_ha = total_m2 / 10000
            mask = plots["plot_id"] == plot_id
            plot_area_ha = plots.loc[mask, "area_ha"].values[0]
            pct = (overlap_ha / plot_area_ha * 100) if plot_area_ha > 0 else 0.0
            plots.loc[mask, col_area] = round(overlap_ha, 6)
            plots.loc[mask, col_pct] = round(pct, 2)
            plots.loc[mask, col_overlap] = overlap_ha > 0

        n = int(plots[col_overlap].sum())
        total_ha = round(float(plots[col_area].sum()), 4)
        logger.info(f"  {infra_type}: {n:,} plots affected | {total_ha:,} ha")
        report["checks"][f"{infra_type}_overlap"] = {
            "status": "PASS" if n == 0 else "FAIL",
            "plots_with_overlap": n,
            "total_overlap_ha": total_ha,
            "buffer_m": buffer_m,
        }
        return plots

    def _apply_skipped_status(self, gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
        gdf = gdf.copy()
        for infra_type in self.INFRA_TYPES:
            gdf[f"chk_{infra_type}_overlap"] = False
            gdf[f"{infra_type}_overlap_ha"] = 0.0
            gdf[f"{infra_type}_overlap_pct"] = 0.0
        gdf["phase_2c_status"] = "SKIPPED"
        return gdf

    def _log_blocked(self) -> None:
        logger.warning("PHASE 2C — BLOCKED: awaiting infrastructure layers from Jibotosh")

    def _write_output(self, gdf: gpd.GeoDataFrame, run_id: str) -> None:
        self.interim_dir.mkdir(parents=True, exist_ok=True)
        path = self.interim_dir / f"2C_plots_infra_checked_{run_id}.gpkg"
        gdf.to_crs(epsg=self.output_epsg).to_file(
            path, driver="GPKG", layer="plots_infra_checked"
        )
        logger.info(f"Output written: {path.name}")

    def _write_json_report(self, report: dict, run_id: str) -> None:
        self.outputs_dir.mkdir(parents=True, exist_ok=True)
        path = self.outputs_dir / f"2C_infra_report_{run_id}.json"
        with open(path, "w") as f:
            json.dump(report, f, indent=2, default=str)

    def _write_csv_report(self, gdf: gpd.GeoDataFrame, run_id: str) -> None:
        base_cols = ["plot_id", "area_ha", "phase_2c_status"]
        infra_cols = [
            col for infra in self.INFRA_TYPES
            for col in [
                f"chk_{infra}_overlap",
                f"{infra}_overlap_ha",
                f"{infra}_overlap_pct",
            ]
        ]
        all_cols = base_cols + infra_cols
        available = [c for c in all_cols if c in gdf.columns]
        path = self.outputs_dir / f"2C_infra_report_{run_id}.csv"
        gdf[available].to_csv(path, index=False, encoding="utf-8")

    def _log_summary(self, gdf: gpd.GeoDataFrame, report: dict) -> None:
        status_counts = (
            gdf["phase_2c_status"].value_counts().to_dict()
            if "phase_2c_status" in gdf.columns else {}
        )
        logger.info("=" * 60)
        logger.info("PHASE 2C SUMMARY")
        logger.info("=" * 60)
        logger.info(f"  Enabled: {self.enabled}")
        for status, count in sorted(status_counts.items()):
            logger.info(f"  {status:<15} {count:>8,} plots")
        logger.info("=" * 60)
