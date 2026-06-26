"""
Phase 1B - Geometry Validity Checks

Checks performed:
  1. is_valid       — GEOS validity (self-intersections, ring issues)
  2. is_simple      — self-intersecting rings
  3. degenerate     — slivers below sliver_area_ha threshold

Area threshold check DISABLED — awaiting AWD methodology
confirmation from Jibotosh on minimum plot size requirement.
"""
from pathlib import Path
from datetime import datetime
from typing import Tuple
import geopandas as gpd
import pandas as pd
from shapely.validation import explain_validity
from loguru import logger
import json
import csv


class GeometryValidator:
    """
    Phase 1B — Geometry validity checks on standardized plot GeoPackage.

    Input:  1A_plots_standardized_RUNID.gpkg
    Output: 1B_plots_geometry_checked_RUNID.gpkg
            1B_geometry_report_RUNID.json
            1B_geometry_report_RUNID.csv
            1B_error_geometries_RUNID.gpkg
    """

    def __init__(self, config):
        self.config = config
        self.interim_dir = Path(config.input["interim_dir"])
        self.outputs_dir = Path(config.input["outputs_dir"])
        self.sliver_ha   = config.thresholds["sliver_area_ha"]
        self.output_epsg = config.crs.get("output_epsg", 4326)
        # Area threshold check disabled
        self.area_threshold_enabled = config.checks.get(
            "area_threshold", False
        )

    def run(self, run_id: str) -> Tuple[gpd.GeoDataFrame, dict]:
        logger.info("=" * 60)
        logger.info("PHASE 1B — GEOMETRY VALIDITY CHECKS")
        logger.info("=" * 60)

        report = {
            "run_id": run_id,
            "phase": "1B",
            "timestamp": datetime.utcnow().isoformat(),
            "checks": {},
        }

        gdf = self._load_input(run_id, report)
        gdf = self._check_null_geometry(gdf, report)
        gdf = self._check_multipolygon(gdf, report)
        gdf = self._check_validity(gdf, report)
        gdf = self._check_simplicity(gdf, report)
        gdf = self._check_degenerate(gdf, report)

        # Area threshold only if enabled in config
        if self.area_threshold_enabled:
            gdf = self._check_area_thresholds(gdf, report)
        else:
            logger.info(
                "Check area_threshold — SKIPPED "
                "(disabled in config — awaiting methodology confirmation)"
            )
            report["checks"]["area_threshold"] = {
                "status": "SKIPPED",
                "reason": "Disabled — awaiting AWD methodology minimum plot size confirmation",
            }

        gdf = self._assign_phase_status(gdf, report)
        self._write_output(gdf, run_id)
        self._write_error_geometries(gdf, run_id)
        self._write_json_report(report, run_id)
        self._write_csv_report(gdf, report, run_id)
        self._log_summary(gdf, report)

        return gdf, report

    # ── INPUT ─────────────────────────────────────────────────────────────

    def _load_input(self, run_id: str, report: dict) -> gpd.GeoDataFrame:
        import glob
        pattern = str(self.interim_dir / "1A_plots_standardized_*.gpkg")
        files = sorted(glob.glob(pattern))
        if not files:
            raise FileNotFoundError(
                "No Phase 1A output found. Run Phase 1A first."
            )
        latest = files[-1]
        logger.info(f"Loading Phase 1A output: {Path(latest).name}")
        gdf = gpd.read_file(latest, layer="plots_standardized")
        logger.info(f"Loaded {len(gdf):,} plots")
        report["input_file"] = latest
        report["total_plots"] = len(gdf)
        return gdf

    # ── CHECKS ────────────────────────────────────────────────────────────

    def _check_null_geometry(
        self, gdf: gpd.GeoDataFrame, report: dict
    ) -> gpd.GeoDataFrame:
        logger.info("Check 1/3 — Null geometry...")
        null_mask = gdf.geometry.isna()
        count = int(null_mask.sum())
        gdf = gdf.copy()
        gdf["chk_null_geom"] = null_mask
        gdf.loc[null_mask, "chk_null_geom_reason"] = (
            "Geometry is None — degenerate in source KML"
        )
        gdf.loc[~null_mask, "chk_null_geom_reason"] = ""
        logger.info(f"  Null geometries: {count}")
        for pid in gdf.loc[null_mask, "plot_id"].values:
            logger.warning(f"  NULL GEOMETRY — plot_id: {pid}")
        report["checks"]["null_geometry"] = {
            "status": "PASS" if count == 0 else "WARNING",
            "null_count": count,
            "plot_ids": list(gdf.loc[null_mask, "plot_id"].values),
        }
        return gdf

    def _check_multipolygon(
        self, gdf: gpd.GeoDataFrame, report: dict
    ) -> gpd.GeoDataFrame:
        logger.info("Check — MultiPolygon detection...")
        gdf = gdf.copy()
        has_geom = gdf.geometry.notna()
        gdf["chk_multipolygon"] = False
        gdf["chk_multipolygon_reason"] = ""
        gdf["chk_multipolygon_part_count"] = 0
        gdf["chk_multipolygon_centroids"] = ""

        mp_mask = has_geom & (gdf.geometry.geom_type == "MultiPolygon")
        count = int(mp_mask.sum())

        if count > 0:
            gdf.loc[mp_mask, "chk_multipolygon"] = True
            for idx in gdf.loc[mp_mask].index:
                geom = gdf.at[idx, "geometry"]
                parts = list(geom.geoms)
                centroids = [
                    f"({p.centroid.x:.6f},{p.centroid.y:.6f})" for p in parts
                ]
                gdf.at[idx, "chk_multipolygon_reason"] = (
                    f"MultiPolygon detected — one ID has {len(parts)} separate farm shapes"
                )
                gdf.at[idx, "chk_multipolygon_part_count"] = len(parts)
                gdf.at[idx, "chk_multipolygon_centroids"] = "; ".join(centroids)
                logger.warning(
                    f"  MULTIPOLYGON — plot_id: {gdf.at[idx, 'plot_id']} | "
                    f"parts: {len(parts)}"
                )

        logger.info(f"  MultiPolygon geometries: {count}")
        report["checks"]["multipolygon"] = {
            "status": "PASS" if count == 0 else "FAIL",
            "multipolygon_count": count,
            "plot_ids": list(gdf.loc[mp_mask, "plot_id"].values[:50]),
        }
        return gdf

    def _check_validity(
        self, gdf: gpd.GeoDataFrame, report: dict
    ) -> gpd.GeoDataFrame:
        logger.info("Check 2/3 — GEOS validity (is_valid)...")
        gdf = gdf.copy()
        has_geom = gdf.geometry.notna()
        gdf["chk_is_valid"] = True
        gdf["chk_is_valid_reason"] = ""
        invalid_mask = has_geom & ~gdf.geometry.is_valid
        count = int(invalid_mask.sum())
        if count > 0:
            gdf.loc[invalid_mask, "chk_is_valid"] = False
            gdf.loc[invalid_mask, "chk_is_valid_reason"] = (
                gdf.loc[invalid_mask, "geometry"]
                .apply(
                    lambda g: explain_validity(g)
                    if g is not None else ""
                )
            )
            for _, row in gdf.loc[invalid_mask].iterrows():
                logger.warning(
                    f"  INVALID — plot_id: {row['plot_id']} | "
                    f"reason: {row['chk_is_valid_reason']}"
                )
        logger.info(f"  Invalid geometries: {count}")
        reasons = (
            gdf.loc[invalid_mask, "chk_is_valid_reason"]
            .value_counts().to_dict()
            if count > 0 else {}
        )
        report["checks"]["geometry_validity"] = {
            "status": "PASS" if count == 0 else "FAIL",
            "invalid_count": count,
            "reason_distribution": reasons,
            "invalid_plot_ids": list(
                gdf.loc[invalid_mask, "plot_id"].values[:50]
            ),
        }
        return gdf

    def _check_simplicity(
        self, gdf: gpd.GeoDataFrame, report: dict
    ) -> gpd.GeoDataFrame:
        logger.info("Check 3/3 — Self-intersection (is_simple)...")
        gdf = gdf.copy()
        has_geom = gdf.geometry.notna()
        gdf["chk_is_simple"] = True
        gdf["chk_is_simple_reason"] = ""
        not_simple_mask = has_geom & ~gdf.geometry.is_simple
        count = int(not_simple_mask.sum())
        if count > 0:
            gdf.loc[not_simple_mask, "chk_is_simple"] = False
            gdf.loc[not_simple_mask, "chk_is_simple_reason"] = (
                "Ring self-intersection or self-touching boundary"
            )
            for pid in gdf.loc[not_simple_mask, "plot_id"].values[:10]:
                logger.warning(f"  NOT SIMPLE — plot_id: {pid}")
        logger.info(f"  Non-simple geometries: {count}")
        report["checks"]["simplicity"] = {
            "status": "PASS" if count == 0 else "FAIL",
            "not_simple_count": count,
            "plot_ids": list(
                gdf.loc[not_simple_mask, "plot_id"].values[:50]
            ),
        }
        return gdf

    def _check_degenerate(
        self, gdf: gpd.GeoDataFrame, report: dict
    ) -> gpd.GeoDataFrame:
        logger.info(
            f"Check — Degenerate/sliver "
            f"(below {self.sliver_ha} ha = "
            f"{self.sliver_ha * 10000:.0f} sqm)..."
        )
        gdf = gdf.copy()
        has_geom = gdf.geometry.notna()
        gdf["chk_degenerate"] = False
        gdf["chk_degenerate_reason"] = ""
        sliver_mask = has_geom & (gdf["area_ha"] < self.sliver_ha)
        count = int(sliver_mask.sum())
        if count > 0:
            gdf.loc[sliver_mask, "chk_degenerate"] = True
            gdf.loc[sliver_mask, "chk_degenerate_reason"] = (
                gdf.loc[sliver_mask, "area_ha"].apply(
                    lambda a: (
                        f"Sliver: {a:.6f} ha = {a*10000:.2f} sqm "
                        f"— below {self.sliver_ha} ha"
                    )
                )
            )
            for _, row in gdf.loc[sliver_mask].iterrows():
                logger.warning(
                    f"  SLIVER — plot_id: {row['plot_id']} | "
                    f"area: {row['area_ha']:.6f} ha"
                )
        logger.info(f"  Sliver geometries: {count}")
        report["checks"]["degenerate_geometry"] = {
            "status": "PASS" if count == 0 else "WARNING",
            "sliver_count": count,
            "threshold_ha": self.sliver_ha,
            "plot_ids": list(
                gdf.loc[sliver_mask, "plot_id"].values
            ),
        }
        return gdf

    # ── STATUS ASSIGNMENT ─────────────────────────────────────────────────

    def _assign_phase_status(
        self, gdf: gpd.GeoDataFrame, report: dict
    ) -> gpd.GeoDataFrame:
        """
        NEEDS_REVIEW → null geometry, invalid geometry, non-simple, MultiPolygon
        PASS         → all checks clean
        """
        gdf = gdf.copy()
        needs_review = (
            gdf["chk_null_geom"] |
            ~gdf["chk_is_valid"] |
            ~gdf["chk_is_simple"] |
            gdf["chk_multipolygon"]
        )
        gdf["phase_1b_status"] = "PASS"
        gdf.loc[needs_review, "phase_1b_status"] = "NEEDS_REVIEW"
        status_counts = gdf["phase_1b_status"].value_counts().to_dict()
        report["phase_1b_status_summary"] = status_counts
        return gdf

    # ── OUTPUTS ───────────────────────────────────────────────────────────

    def _write_output(self, gdf: gpd.GeoDataFrame, run_id: str) -> None:
        self.interim_dir.mkdir(parents=True, exist_ok=True)
        output_path = (
            self.interim_dir /
            f"1B_plots_geometry_checked_{run_id}.gpkg"
        )
        logger.info(f"Writing output: {output_path.name}")
        gdf.to_file(
            output_path, driver="GPKG",
            layer="plots_geometry_checked"
        )
        logger.info("Output written successfully")

    def _write_error_geometries(
        self, gdf: gpd.GeoDataFrame, run_id: str
    ) -> None:
        error_mask = gdf["phase_1b_status"] == "FAIL"
        error_gdf  = gdf.loc[error_mask].copy()
        if len(error_gdf) == 0:
            logger.info("No error geometries to export — all PASS")
            return
        error_gdf = error_gdf.to_crs(epsg=self.output_epsg)
        output_path = (
            self.outputs_dir /
            f"1B_error_geometries_{run_id}.gpkg"
        )
        self.outputs_dir.mkdir(parents=True, exist_ok=True)
        error_gdf.to_file(
            output_path, driver="GPKG",
            layer="error_geometries"
        )
        logger.info(
            f"Error geometries exported: {output_path.name} "
            f"({len(error_gdf)} plots)"
        )

    def _write_json_report(self, report: dict, run_id: str) -> None:
        self.outputs_dir.mkdir(parents=True, exist_ok=True)
        report_path = (
            self.outputs_dir /
            f"1B_geometry_report_{run_id}.json"
        )
        with open(report_path, "w") as f:
            json.dump(report, f, indent=2, default=str)
        logger.info(f"JSON report written: {report_path.name}")

    def _write_csv_report(
        self, gdf: gpd.GeoDataFrame, report: dict, run_id: str
    ) -> None:
        """
        CSV report — one row per plot with all check results.
        Suitable for sharing with field team and project managers.
        """
        self.outputs_dir.mkdir(parents=True, exist_ok=True)
        report_path = (
            self.outputs_dir /
            f"1B_geometry_report_{run_id}.csv"
        )

        # Select relevant columns for the report
        report_cols = [
            "plot_id",
            "area_ha",
            "phase_1b_status",
            "chk_null_geom",
            "chk_null_geom_reason",
            "chk_multipolygon",
            "chk_multipolygon_reason",
            "chk_multipolygon_part_count",
            "chk_is_valid",
            "chk_is_valid_reason",
            "chk_is_simple",
            "chk_is_simple_reason",
            "chk_degenerate",
            "chk_degenerate_reason",
        ]

        # Only include columns that exist
        available_cols = [c for c in report_cols if c in gdf.columns]
        report_df = gdf[available_cols].copy()

        # Add human readable area columns
        report_df["area_sqm"]  = (report_df["area_ha"] * 10000).round(2)
        report_df["area_sqft"] = (report_df["area_ha"] * 107639).round(0)

        # Sort — FAIL first, then PASS
        sort_order = {"FAIL": 0, "WARNING": 1, "PASS": 2}
        report_df["_sort"] = report_df["phase_1b_status"].map(sort_order)
        report_df = report_df.sort_values("_sort").drop(columns=["_sort"])

        report_df.to_csv(report_path, index=False, encoding="utf-8")

        fail_count = int((report_df["phase_1b_status"] == "FAIL").sum())
        pass_count = int((report_df["phase_1b_status"] == "PASS").sum())

        logger.info(f"CSV report written: {report_path.name}")
        logger.info(
            f"  CSV rows — FAIL: {fail_count:,} | "
            f"PASS: {pass_count:,} | "
            f"Total: {len(report_df):,}"
        )

    def _log_summary(
        self, gdf: gpd.GeoDataFrame, report: dict
    ) -> None:
        checks = report["checks"]
        status_summary = report.get("phase_1b_status_summary", {})
        logger.info("=" * 60)
        logger.info("PHASE 1B SUMMARY")
        logger.info("=" * 60)
        for check_name, result in checks.items():
            status = result.get("status", "UNKNOWN")
            logger.info(f"  {check_name:<35} {status}")
        logger.info("-" * 60)
        logger.info("  PLOT STATUS BREAKDOWN:")
        for status, count in sorted(status_summary.items()):
            logger.info(f"    {status:<15} {count:>8,} plots")
        logger.info("=" * 60)
