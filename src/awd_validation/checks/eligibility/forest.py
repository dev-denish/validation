"""
Phase 2A — Forest Overlap Check

Checks whether AWD plots overlap with forest-classified land.
Forest definition priority:
  1. Maharashtra State Forest Department definition (when confirmed)
  2. UNFCCC CDM DNA fallback: 10% canopy, 5m height, 0.5 ha minimum
     https://cdm.unfccc.int/DNA/index.html

BLOCKED until Jibotosh confirms forest definition and provides layer.
When enabled: set phase2.forest.enabled = true in settings.yaml
              set phase2.forest.layer_path to the forest layer path
              set phase2.forest.definition_source to confirmed source
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

from awd_validation.utils.config import compute_working_epsg


class ForestOverlapChecker:
    """
    Phase 2A — Forest overlap check on Phase 1F valid plots.

    Input:  valid_plots_{run_id}.gpkg  (from Phase 1F outputs)
    Output: 2A_plots_forest_checked_{run_id}.gpkg  (interim)
            2A_forest_report_{run_id}.json          (outputs)
            2A_forest_report_{run_id}.csv           (outputs)

    When disabled (awaiting data/confirmation):
        Passes all plots through with chk_forest_overlap = False
        and phase_2a_status = SKIPPED for full traceability.
    """

    def __init__(self, config) -> None:
        self.config = config
        self.interim_dir = Path(config.input["interim_dir"])
        self.outputs_dir = Path(config.input["outputs_dir"])
        self.output_epsg = config.crs.get("output_epsg", 4326)
        self.fallback_epsg = config.crs.get("fallback_working_epsg", 32644)
        self.working_epsg: int = self.fallback_epsg  # set dynamically in _load_input
        self.phase2_cfg = config.get("phase2", default={})
        self.forest_cfg = self.phase2_cfg.get("forest", {})
        self.enabled = self.forest_cfg.get("enabled", False)
        self.layer_path = self.forest_cfg.get("layer_path")
        self.definition_source = self.forest_cfg.get(
            "definition_source", "pending"
        )
        self.cdm_min_area_ha = self.forest_cfg.get(
            "minimum_forest_area_ha",
            self.forest_cfg.get("cdm_min_area_ha", 1.0),
        )
        self.canopy_pct = self.forest_cfg.get(
            "canopy_density_threshold_pct",
            self.forest_cfg.get("cdm_canopy_pct", 10),
        )
        self.hansen_layer_path = self.forest_cfg.get("hansen_layer_path")
        self.hansen_enabled = self.forest_cfg.get("hansen_enabled", False)

    def run(self, run_id: str) -> Tuple[gpd.GeoDataFrame, dict]:
        logger.info("=" * 60)
        logger.info("PHASE 2A — FOREST OVERLAP CHECK")
        logger.info("=" * 60)

        report: dict = {
            "run_id": run_id,
            "phase": "2A",
            "timestamp": datetime.utcnow().isoformat(),
            "forest_definition_source": self.definition_source,
            "enabled": self.enabled,
            "checks": {},
        }

        gdf = self._load_input(run_id, report)

        if not self.enabled:
            self._log_blocked()
            gdf = self._apply_skipped_status(gdf)
            report["checks"]["forest_overlap"] = {
                "status": "SKIPPED",
                "reason": (
                    "BLOCKED — awaiting Jibotosh confirmation of forest "
                    "definition (Maharashtra state or CDM DNA fallback) "
                    "and forest classification layer. "
                    "Set phase2.forest.enabled=true in settings.yaml "
                    "once layer and definition are confirmed."
                ),
                "cdm_dna_reference": "https://cdm.unfccc.int/DNA/index.html",
                "cdm_defaults": {
                    "canopy_pct": self.forest_cfg.get("cdm_canopy_pct", 10),
                    "height_m": self.forest_cfg.get("cdm_height_m", 5),
                    "min_area_ha": self.cdm_min_area_ha,
                },
            }
        else:
            self._validate_definition_source(report)
            forest_rfa = self._load_forest_layer(report)
            forest_hansen = self._load_hansen_layer(report)
            gdf = self._check_forest_overlap(gdf, forest_rfa, forest_hansen, report)

        self._write_output(gdf, run_id)
        self._write_json_report(report, run_id)
        self._write_csv_report(gdf, run_id)
        self._log_summary(gdf, report)

        return gdf, report

    # ── INPUT ─────────────────────────────────────────────────────────────

    def _load_input(self, run_id: str, report: dict) -> gpd.GeoDataFrame:
        """Load Phase 1F valid plots — only eligible plots enter Phase 2."""
        pattern = str(self.outputs_dir / "valid_plots_*.gpkg")
        files = sorted(glob.glob(pattern))
        if not files:
            raise FileNotFoundError(
                "No Phase 1F valid_plots output found. "
                "Run Phase 1 pipeline first."
            )
        latest = files[-1]
        logger.info(f"Loading Phase 1F valid plots: {Path(latest).name}")
        gdf = gpd.read_file(latest)
        # Compute UTM zone dynamically from data centroid
        self.working_epsg = compute_working_epsg(gdf, self.fallback_epsg)
        if gdf.crs is None or gdf.crs.to_epsg() != self.working_epsg:
            gdf = gdf.to_crs(epsg=self.working_epsg)
        logger.info(f"Loaded {len(gdf):,} valid plots | working CRS: EPSG:{self.working_epsg}")
        report["input_file"] = latest
        report["total_plots"] = len(gdf)
        return gdf

    def _load_forest_layer(self, report: dict) -> gpd.GeoDataFrame:
        """Load and validate forest classification layer."""
        if not self.layer_path:
            raise ValueError(
                "phase2.forest.layer_path is not set in settings.yaml. "
                "Provide the path to the forest classification layer."
            )
        layer_path = Path(self.layer_path)
        if not layer_path.exists():
            raise FileNotFoundError(
                f"Forest layer not found: {layer_path}. "
                "Confirm path in phase2.forest.layer_path."
            )
        logger.info(f"Loading forest layer: {layer_path.name}")
        forest_gdf = gpd.read_file(layer_path)
        # Filter to Maharashtra state only — layer covers multiple states
        if "st_name" in forest_gdf.columns:
            before_state = len(forest_gdf)
            forest_gdf = forest_gdf[
                forest_gdf["st_name"] == "MAHARASHTRA"
            ].copy()
            logger.info(
                f"State filter (st_name='MAHARASHTRA'): "
                f"{len(forest_gdf):,} features retained "
                f"(dropped {before_state - len(forest_gdf)} from other states)"
            )
        if forest_gdf.crs.to_epsg() != self.working_epsg:
            forest_gdf = forest_gdf.to_crs(epsg=self.working_epsg)
        # Validate geometry
        invalid = (~forest_gdf.geometry.is_valid).sum()
        if invalid > 0:
            logger.warning(
                f"Forest layer has {invalid} invalid geometries — fixing with make_valid"
            )
            forest_gdf["geometry"] = forest_gdf.geometry.apply(make_valid)
        # Filter by minimum area (CDM: 0.5 ha minimum)
        forest_gdf["layer_area_ha"] = forest_gdf.geometry.area / 10000
        before = len(forest_gdf)
        forest_gdf = forest_gdf[
            forest_gdf["layer_area_ha"] >= self.cdm_min_area_ha
        ].copy()
        logger.info(
            f"Forest layer loaded: {len(forest_gdf):,} polygons "
            f"(filtered {before - len(forest_gdf)} below {self.cdm_min_area_ha} ha)"
        )
        report["forest_layer"] = {
            "path": str(layer_path),
            "total_polygons": before,
            "polygons_after_area_filter": len(forest_gdf),
            "min_area_filter_ha": self.cdm_min_area_ha,
            "definition_source": self.definition_source,
        }
        return forest_gdf

    def _load_hansen_layer(self, report: dict) -> Optional[gpd.GeoDataFrame]:
        """Load Hansen canopy cover vector layer (polygonized GeoPackage)."""
        if not self.hansen_enabled or not self.hansen_layer_path:
            logger.info("Hansen canopy layer disabled or path not set — skipping")
            return None
        layer_path = Path(self.hansen_layer_path)
        if not layer_path.exists():
            logger.warning(f"Hansen layer not found: {layer_path} — skipping")
            return None
        logger.info(f"Loading Hansen canopy layer: {layer_path.name}")
        hansen_gdf = gpd.read_file(layer_path)
        if hansen_gdf.crs.to_epsg() != self.working_epsg:
            hansen_gdf = hansen_gdf.to_crs(epsg=self.working_epsg)
        invalid = (~hansen_gdf.geometry.is_valid).sum()
        if invalid > 0:
            logger.warning(
                f"Hansen layer: {invalid} invalid geometries — fixing with make_valid"
            )
            hansen_gdf["geometry"] = hansen_gdf.geometry.apply(make_valid)
        logger.info(
            f"Hansen canopy layer loaded: {len(hansen_gdf):,} polygons "
            f"(>={self.canopy_pct}% canopy cover, >={self.cdm_min_area_ha} ha)"
        )
        report["hansen_layer"] = {
            "path": str(layer_path),
            "polygons": len(hansen_gdf),
            "threshold_canopy_pct": self.canopy_pct,
            "min_area_ha": self.cdm_min_area_ha,
        }
        return hansen_gdf

    # ── CHECKS ────────────────────────────────────────────────────────────

    def _validate_definition_source(self, report: dict) -> None:
        """Warn if forest definition source is not confirmed."""
        valid_sources = {"maharashtra_state", "maharashtra_fsi", "cdm_dna"}
        if self.definition_source not in valid_sources:
            logger.warning(
                f"Forest definition source '{self.definition_source}' is not confirmed. "
                f"Valid values: {valid_sources}. "
                "Update phase2.forest.definition_source in settings.yaml."
            )
        else:
            logger.info(f"Forest definition source: {self.definition_source}")
        report["definition_source_validated"] = (
            self.definition_source in valid_sources
        )

    def _check_forest_overlap(
        self,
        plots: gpd.GeoDataFrame,
        forest_rfa: Optional[gpd.GeoDataFrame],
        forest_hansen: Optional[gpd.GeoDataFrame],
        report: dict,
    ) -> gpd.GeoDataFrame:
        """
        Overlay plots with both RFA and Hansen forest layers (union approach).

        A plot is flagged if it overlaps EITHER layer. Overlap area is computed
        from the geometric union of both layers to avoid double-counting where
        they coincide. Adds forest_overlap_source: RFA | Hansen | Both | None.
        """
        logger.info("Calculating forest overlap (RFA + Hansen union approach)...")
        plots = plots.copy()

        plots["chk_forest_overlap"] = False
        plots["forest_overlap_ha"] = 0.0
        plots["forest_overlap_pct"] = 0.0
        plots["forest_overlap_source"] = "None"
        plots["forest_definition_source"] = self.definition_source

        # Build combined layer tagged by source
        layer_gdfs = []
        if forest_rfa is not None and len(forest_rfa) > 0:
            rfa = forest_rfa[["geometry"]].copy()
            rfa["_src"] = "RFA"
            layer_gdfs.append(rfa)
        if forest_hansen is not None and len(forest_hansen) > 0:
            han = forest_hansen[["geometry"]].copy()
            han["_src"] = "Hansen"
            layer_gdfs.append(han)

        if not layer_gdfs:
            logger.info("No forest layers available — all plots pass")
            plots["phase_2a_status"] = "PASS"
            report["checks"]["forest_overlap"] = {
                "status": "PASS", "plots_with_overlap": 0, "total_overlap_ha": 0.0,
            }
            return plots

        combined = gpd.pd.concat(layer_gdfs, ignore_index=True)
        src_labels = combined["_src"].unique().tolist()
        logger.info(
            f"Combined forest layer: {len(combined):,} polygons "
            f"(sources: {', '.join(src_labels)})"
        )

        logger.info("Running spatial index join against combined forest layer...")
        joined = gpd.sjoin(
            plots[["plot_id", "area_ha", "geometry"]],
            combined[["_src", "geometry"]],
            how="inner",
            predicate="intersects",
        )

        if len(joined) == 0:
            logger.info("No forest overlaps found")
            plots["phase_2a_status"] = "PASS"
            report["checks"]["forest_overlap"] = {
                "status": "PASS", "plots_with_overlap": 0, "total_overlap_ha": 0.0,
            }
            return plots

        logger.info(
            f"Computing exact intersections for {len(joined):,} candidate pairs..."
        )

        for plot_id, group in joined.groupby("plot_id"):
            mask = plots["plot_id"] == plot_id
            plot_geom = plots.loc[mask, "geometry"].values[0]
            plot_area_ha = plots.loc[mask, "area_ha"].values[0]

            sources_hit: set[str] = set()
            intersection_geoms = []

            for idx in group["index_right"]:
                forest_geom = combined.loc[idx, "geometry"]
                src = combined.loc[idx, "_src"]
                intersection = plot_geom.intersection(forest_geom)
                if not intersection.is_empty:
                    intersection_geoms.append(intersection)
                    sources_hit.add(src)

            if not intersection_geoms:
                continue

            # Union all intersections — avoids double-counting where RFA and
            # Hansen overlap the same area within a plot
            total_intersection = unary_union(intersection_geoms)
            overlap_ha = total_intersection.area / 10000
            overlap_pct = (
                (overlap_ha / plot_area_ha * 100) if plot_area_ha > 0 else 0.0
            )

            if "RFA" in sources_hit and "Hansen" in sources_hit:
                source = "Both"
            elif "RFA" in sources_hit:
                source = "RFA"
            else:
                source = "Hansen"

            plots.loc[mask, "forest_overlap_ha"] = round(overlap_ha, 6)
            plots.loc[mask, "forest_overlap_pct"] = round(overlap_pct, 2)
            plots.loc[mask, "chk_forest_overlap"] = overlap_ha > 0
            plots.loc[mask, "forest_overlap_source"] = source

        plots_with_overlap = int(plots["chk_forest_overlap"].sum())
        total_overlap_ha = round(float(plots["forest_overlap_ha"].sum()), 4)
        source_counts = (
            plots.loc[plots["chk_forest_overlap"], "forest_overlap_source"]
            .value_counts()
            .to_dict()
        )

        logger.info("Forest overlap results:")
        logger.info(f"  Plots with overlap:  {plots_with_overlap:,}")
        logger.info(f"  Total overlap area:  {total_overlap_ha:,} ha")
        logger.info(f"  Source — RFA only:   {source_counts.get('RFA', 0):,}")
        logger.info(f"  Source — Hansen only:{source_counts.get('Hansen', 0):,}")
        logger.info(f"  Source — Both:       {source_counts.get('Both', 0):,}")

        report["checks"]["forest_overlap"] = {
            "status": "PASS" if plots_with_overlap == 0 else "FAIL",
            "plots_with_overlap": plots_with_overlap,
            "total_overlap_ha": total_overlap_ha,
            "source_breakdown": source_counts,
            "definition_source": self.definition_source,
        }

        plots["phase_2a_status"] = plots["chk_forest_overlap"].apply(
            lambda x: "FAIL" if x else "PASS"
        )
        return plots

    # ── SKIPPED STATE ─────────────────────────────────────────────────────

    def _apply_skipped_status(
        self, gdf: gpd.GeoDataFrame
    ) -> gpd.GeoDataFrame:
        """Apply neutral columns when phase is skipped — full traceability."""
        gdf = gdf.copy()
        gdf["chk_forest_overlap"] = False
        gdf["forest_overlap_ha"] = 0.0
        gdf["forest_overlap_pct"] = 0.0
        gdf["forest_overlap_source"] = "None"
        gdf["forest_definition_source"] = "pending"
        gdf["phase_2a_status"] = "SKIPPED"
        return gdf

    def _log_blocked(self) -> None:
        logger.warning("=" * 60)
        logger.warning("PHASE 2A — BLOCKED")
        logger.warning("  Awaiting from Jibotosh:")
        logger.warning("  1. Maharashtra forest definition confirmation")
        logger.warning("     OR CDM DNA fallback approval")
        logger.warning("     Ref: https://cdm.unfccc.int/DNA/index.html")
        logger.warning("  2. Forest classification GIS layer (.gpkg/.shp)")
        logger.warning("  Once received:")
        logger.warning("    - Set phase2.forest.enabled = true")
        logger.warning("    - Set phase2.forest.layer_path = <path>")
        logger.warning("    - Set phase2.forest.definition_source = <source>")
        logger.warning("  All plots passed through with SKIPPED status.")
        logger.warning("=" * 60)

    # ── OUTPUTS ───────────────────────────────────────────────────────────

    def _write_output(self, gdf: gpd.GeoDataFrame, run_id: str) -> None:
        self.interim_dir.mkdir(parents=True, exist_ok=True)
        output_path = (
            self.interim_dir / f"2A_plots_forest_checked_{run_id}.gpkg"
        )
        gdf.to_crs(epsg=self.output_epsg).to_file(
            output_path, driver="GPKG", layer="plots_forest_checked"
        )
        logger.info(f"Output written: {output_path.name}")

    def _write_json_report(self, report: dict, run_id: str) -> None:
        self.outputs_dir.mkdir(parents=True, exist_ok=True)
        path = self.outputs_dir / f"2A_forest_report_{run_id}.json"
        with open(path, "w") as f:
            json.dump(report, f, indent=2, default=str)
        logger.info(f"JSON report written: {path.name}")

    def _write_csv_report(self, gdf: gpd.GeoDataFrame, run_id: str) -> None:
        cols = [
            "plot_id", "area_ha", "phase_2a_status",
            "chk_forest_overlap", "forest_overlap_ha",
            "forest_overlap_pct", "forest_overlap_source",
            "forest_definition_source",
        ]
        available = [c for c in cols if c in gdf.columns]
        path = self.outputs_dir / f"2A_forest_report_{run_id}.csv"
        gdf[available].to_csv(path, index=False, encoding="utf-8")
        logger.info(f"CSV report written: {path.name}")

    def _log_summary(self, gdf: gpd.GeoDataFrame, report: dict) -> None:
        status_counts = (
            gdf["phase_2a_status"].value_counts().to_dict()
            if "phase_2a_status" in gdf.columns else {}
        )
        logger.info("=" * 60)
        logger.info("PHASE 2A SUMMARY")
        logger.info("=" * 60)
        logger.info(f"  Enabled:        {self.enabled}")
        logger.info(f"  Definition:     {self.definition_source}")
        for status, count in sorted(status_counts.items()):
            logger.info(f"  {status:<15} {count:>8,} plots")
        logger.info("=" * 60)
