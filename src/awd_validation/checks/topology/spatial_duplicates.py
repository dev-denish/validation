from pathlib import Path
from datetime import datetime
from typing import Tuple
import geopandas as gpd
import pandas as pd
import numpy as np
from shapely.strtree import STRtree
from loguru import logger
import json


class SpatialDuplicateDetector:

    def __init__(self, config):
        self.config = config
        self.interim_dir = Path(config.input["interim_dir"])
        self.outputs_dir = Path(config.input["outputs_dir"])
        self.centroid_distance_m = config.thresholds["centroid_distance_m"]

    def run(self, run_id: str) -> Tuple[gpd.GeoDataFrame, dict]:
        logger.info("=" * 60)
        logger.info("PHASE 1D - SPATIAL DUPLICATE DETECTION")
        logger.info("=" * 60)
        report = {
            "run_id": run_id,
            "phase": "1D",
            "timestamp": datetime.utcnow().isoformat(),
            "centroid_distance_threshold_m": self.centroid_distance_m,
            "checks": {},
        }
        gdf = self._load_input(run_id, report)
        gdf = self._check_centroid_duplicates(gdf, report)
        gdf = self._check_area_centroid_match(gdf, report)
        gdf = self._assign_phase_status(gdf, report)
        self._write_output(gdf, run_id)
        self._write_json_report(report, run_id)
        self._write_csv_report(gdf, run_id)
        self._log_summary(gdf, report)
        return gdf, report

    def _load_input(self, run_id: str, report: dict) -> gpd.GeoDataFrame:
        import glob
        pattern = str(self.interim_dir / "1C_plots_id_checked_*.gpkg")
        files = sorted(glob.glob(pattern))
        if not files:
            raise FileNotFoundError("No Phase 1C output found. Run Phase 1C first.")
        latest = files[-1]
        logger.info(f"Loading Phase 1C output: {Path(latest).name}")
        gdf = gpd.read_file(latest, layer="plots_id_checked")
        logger.info(f"Loaded {len(gdf):,} plots")
        report["input_file"] = latest
        report["total_plots"] = len(gdf)
        return gdf

    def _check_centroid_duplicates(self, gdf: gpd.GeoDataFrame, report: dict) -> gpd.GeoDataFrame:
        logger.info(f"Check 1/2 - Centroid proximity (threshold: {self.centroid_distance_m}m)...")
        gdf = gdf.copy()
        gdf["chk_centroid_duplicate"] = False
        gdf["chk_centroid_duplicate_pair"] = ""
        gdf["chk_centroid_distance_m"] = np.nan
        valid_mask = gdf.geometry.notna()
        valid_gdf = gdf.loc[valid_mask].copy()
        if len(valid_gdf) == 0:
            report["checks"]["centroid_duplicates"] = {"status": "SKIPPED"}
            return gdf
        centroids = valid_gdf.geometry.centroid
        centroid_list = list(centroids)
        tree = STRtree(centroid_list)
        duplicate_pairs = []
        logger.info(f"  Running STRtree on {len(valid_gdf):,} valid plots...")
        valid_index_list = list(valid_gdf.index)
        for pos_i, idx in enumerate(valid_index_list):
            row = valid_gdf.loc[idx]
            centroid = centroid_list[pos_i]
            candidate_positions = tree.query(centroid.buffer(self.centroid_distance_m))
            for pos_j in candidate_positions:
                if pos_j <= pos_i:
                    continue
                dist = centroid.distance(centroid_list[pos_j])
                if dist <= self.centroid_distance_m and dist > 0:
                    j_idx = valid_index_list[pos_j]
                    plot_id_i = row["plot_id"]
                    plot_id_j = valid_gdf.loc[j_idx, "plot_id"]
                    duplicate_pairs.append({
                        "plot_id_1": plot_id_i,
                        "plot_id_2": plot_id_j,
                        "centroid_distance_m": round(float(dist), 3),
                    })
                    logger.warning(f"  CENTROID DUPLICATE - '{plot_id_i}' <-> '{plot_id_j}' | distance: {dist:.2f}m")
        for pair in duplicate_pairs:
            for pid in [pair["plot_id_1"], pair["plot_id_2"]]:
                match = gdf["plot_id"] == pid
                gdf.loc[match, "chk_centroid_duplicate"] = True
                gdf.loc[match, "chk_centroid_duplicate_pair"] = f"Centroid within {self.centroid_distance_m}m of another plot"
                gdf.loc[match, "chk_centroid_distance_m"] = pair["centroid_distance_m"]
        count = len(duplicate_pairs)
        logger.info(f"  Centroid duplicate pairs found: {count}")
        report["checks"]["centroid_duplicates"] = {
            "status": "PASS" if count == 0 else "FAIL",
            "threshold_m": self.centroid_distance_m,
            "duplicate_pairs_count": count,
            "duplicate_pairs": duplicate_pairs[:50],
        }
        return gdf

    def _check_area_centroid_match(self, gdf: gpd.GeoDataFrame, report: dict) -> gpd.GeoDataFrame:
        logger.info("Check 2/2 - Area + centroid match...")
        gdf = gdf.copy()
        gdf["chk_area_centroid_match"] = False
        valid_mask = gdf.geometry.notna()
        valid_gdf = gdf.loc[valid_mask].copy()
        if len(valid_gdf) == 0:
            report["checks"]["area_centroid_match"] = {"status": "PASS", "count": 0}
            return gdf
        valid_gdf["_area_rounded"] = valid_gdf["area_ha"].round(4)
        valid_gdf["_cx"] = valid_gdf.geometry.centroid.x.round(1)
        valid_gdf["_cy"] = valid_gdf.geometry.centroid.y.round(1)
        dup_mask = valid_gdf.duplicated(subset=["_area_rounded", "_cx", "_cy"], keep=False)
        dup_plots = valid_gdf.loc[dup_mask]
        count = int(dup_mask.sum())
        if count > 0:
            for _, row in dup_plots.iterrows():
                match = gdf["plot_id"] == row["plot_id"]
                gdf.loc[match, "chk_area_centroid_match"] = True
            for pid in dup_plots["plot_id"].values:
                logger.warning(f"  AREA+CENTROID MATCH - plot_id: {pid}")
        logger.info(f"  Area+centroid match plots: {count}")
        report["checks"]["area_centroid_match"] = {
            "status": "PASS" if count == 0 else "FAIL",
            "count": count,
            "plot_ids": list(dup_plots["plot_id"].values[:50]) if count > 0 else [],
        }
        return gdf

    def _assign_phase_status(self, gdf: gpd.GeoDataFrame, report: dict) -> gpd.GeoDataFrame:
        gdf = gdf.copy()
        spatial_dup = gdf["chk_centroid_duplicate"] | gdf["chk_area_centroid_match"]
        gdf["phase_1d_status"] = "PASS"
        gdf.loc[spatial_dup, "phase_1d_status"] = "FAIL"
        status_counts = gdf["phase_1d_status"].value_counts().to_dict()
        report["phase_1d_status_summary"] = status_counts
        return gdf

    def _write_output(self, gdf: gpd.GeoDataFrame, run_id: str) -> None:
        self.interim_dir.mkdir(parents=True, exist_ok=True)
        output_path = self.interim_dir / f"1D_plots_spatial_dedup_{run_id}.gpkg"
        logger.info(f"Writing output: {output_path.name}")
        gdf.to_file(output_path, driver="GPKG", layer="plots_spatial_dedup")
        logger.info("Output written successfully")

    def _write_json_report(self, report: dict, run_id: str) -> None:
        self.outputs_dir.mkdir(parents=True, exist_ok=True)
        report_path = self.outputs_dir / f"1D_spatial_duplicate_report_{run_id}.json"
        with open(report_path, "w") as f:
            json.dump(report, f, indent=2, default=str)
        logger.info(f"JSON report written: {report_path.name}")

    def _write_csv_report(self, gdf: gpd.GeoDataFrame, run_id: str) -> None:
        self.outputs_dir.mkdir(parents=True, exist_ok=True)
        report_path = self.outputs_dir / f"1D_spatial_duplicate_report_{run_id}.csv"
        report_cols = [
            "plot_id", "area_ha", "phase_1d_status",
            "chk_centroid_duplicate", "chk_centroid_duplicate_pair",
            "chk_centroid_distance_m", "chk_area_centroid_match",
        ]
        available = [c for c in report_cols if c in gdf.columns]
        report_df = gdf[available].copy()
        sort_order = {"FAIL": 0, "PASS": 1}
        report_df["_sort"] = report_df["phase_1d_status"].map(sort_order)
        report_df = report_df.sort_values("_sort").drop(columns=["_sort"])
        report_df.to_csv(report_path, index=False, encoding="utf-8")
        fail_count = int((report_df["phase_1d_status"] == "FAIL").sum())
        pass_count = int((report_df["phase_1d_status"] == "PASS").sum())
        logger.info(f"CSV report written: {report_path.name}")
        logger.info(f"  CSV rows - FAIL: {fail_count:,} | PASS: {pass_count:,} | Total: {len(report_df):,}")

    def _log_summary(self, gdf: gpd.GeoDataFrame, report: dict) -> None:
        checks = report["checks"]
        status_summary = report.get("phase_1d_status_summary", {})
        logger.info("=" * 60)
        logger.info("PHASE 1D SUMMARY")
        logger.info("=" * 60)
        for check_name, result in checks.items():
            status = result.get("status", "UNKNOWN")
            logger.info(f"  {check_name:<35} {status}")
        logger.info("-" * 60)
        logger.info("  PLOT STATUS BREAKDOWN:")
        for status, count in sorted(status_summary.items()):
            logger.info(f"    {status:<15} {count:>8,} plots")
        logger.info("-" * 60)
        logger.info("  SPATIAL DUPLICATE BREAKDOWN:")
        centroid = int(gdf["chk_centroid_duplicate"].sum())
        area_cx = int(gdf["chk_area_centroid_match"].sum())
        logger.info(f"    Centroid within {self.centroid_distance_m}m:   {centroid:>8,} plots")
        logger.info(f"    Area+centroid match:         {area_cx:>8,} plots")
        logger.info("=" * 60)