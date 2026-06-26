from pathlib import Path
from datetime import datetime
from typing import Tuple
import geopandas as gpd
import pandas as pd
import numpy as np
from shapely.strtree import STRtree
from loguru import logger
import json


class OverlapDetector:

    def __init__(self, config):
        self.config = config
        self.interim_dir = Path(config.input["interim_dir"])
        self.outputs_dir = Path(config.input["outputs_dir"])
        self.output_epsg = config.crs.get("output_epsg", 4326)
        self.overlap_pct_threshold = config.thresholds["overlap_area_pct"]
        self.overlap_minor_fraction = config.thresholds.get(
            "overlap_minor_fraction", 0.10
        )
        self.overlap_significant_fraction = config.thresholds.get(
            "overlap_significant_fraction", 0.50
        )

    def run(self, run_id: str) -> Tuple[gpd.GeoDataFrame, dict]:
        logger.info("=" * 60)
        logger.info("PHASE 1E - TOPOLOGY & OVERLAP DETECTION")
        logger.info("=" * 60)

        report = {
            "run_id": run_id,
            "phase": "1E",
            "timestamp": datetime.utcnow().isoformat(),
            "overlap_pct_threshold": self.overlap_pct_threshold,
            "checks": {},
        }

        gdf = self._load_input(run_id, report)
        gdf = self._check_overlaps(gdf, report)
        gdf = self._assign_phase_status(gdf, report)
        self._write_output(gdf, run_id)
        self._write_error_geometries(gdf, run_id)
        self._write_json_report(report, run_id)
        self._write_csv_report(gdf, run_id)
        self._log_summary(gdf, report)

        return gdf, report

    def _load_input(self, run_id: str, report: dict) -> gpd.GeoDataFrame:
        import glob
        pattern = str(self.interim_dir / "1D_plots_spatial_dedup_*.gpkg")
        files = sorted(glob.glob(pattern))
        if not files:
            raise FileNotFoundError("No Phase 1D output found. Run Phase 1D first.")
        latest = files[-1]
        logger.info(f"Loading Phase 1D output: {Path(latest).name}")
        gdf = gpd.read_file(latest, layer="plots_spatial_dedup")
        logger.info(f"Loaded {len(gdf):,} plots")
        report["input_file"] = latest
        report["total_plots"] = len(gdf)
        return gdf

    def _check_overlaps(self, gdf: gpd.GeoDataFrame, report: dict) -> gpd.GeoDataFrame:
        logger.info(
            f"Check 1/1 - Spatial overlap detection "
            f"(threshold: >{self.overlap_pct_threshold*100}% of plot area)..."
        )

        gdf = gdf.copy()
        gdf["chk_overlap"]                = False
        gdf["chk_overlap_area_ha"]        = 0.0
        gdf["chk_overlap_area_sqm"]       = 0.0
        gdf["chk_overlap_total_area_ha"]  = 0.0
        gdf["chk_overlap_total_area_sqm"] = 0.0
        gdf["chk_overlap_pct"]            = 0.0
        gdf["chk_overlap_severity"]       = ""
        gdf["chk_overlap_partners"]       = ""
        gdf["chk_overlap_partner_count"]  = 0

        # Only process valid geometries
        valid_mask = gdf.geometry.notna() & gdf.geometry.is_valid
        valid_gdf  = gdf.loc[valid_mask].copy()
        invalid_count = int((~valid_mask).sum())

        logger.info(
            f"  Valid geometries for overlap check: {len(valid_gdf):,} "
            f"(skipping {invalid_count:,} invalid/null)"
        )

        if len(valid_gdf) < 2:
            report["checks"]["overlaps"] = {"status": "SKIPPED", "reason": "Insufficient valid geometries"}
            return gdf

        # Build STRtree spatial index
        geom_list = list(valid_gdf.geometry)
        tree = STRtree(geom_list)
        valid_index_list = list(valid_gdf.index)

        overlap_pairs  = []
        overlap_counts = {"minor": 0, "significant": 0, "major": 0}

        logger.info(f"  Building STRtree index on {len(valid_gdf):,} geometries...")
        logger.info("  Running pairwise overlap checks (this may take a few minutes)...")

        processed = 0
        for pos_i, idx in enumerate(valid_index_list):
            geom_i  = geom_list[pos_i]
            area_i  = valid_gdf.loc[idx, "area_ha"]
            pid_i   = valid_gdf.loc[idx, "plot_id"]

            # Query STRtree for candidate overlaps (bounding box intersection)
            candidate_positions = tree.query(geom_i)

            for pos_j in candidate_positions:
                if pos_j <= pos_i:
                    continue

                geom_j  = geom_list[pos_j]
                j_idx   = valid_index_list[pos_j]
                area_j  = valid_gdf.loc[j_idx, "area_ha"]
                pid_j   = valid_gdf.loc[j_idx, "plot_id"]

                # Check actual geometry intersection
                if not geom_i.intersects(geom_j):
                    continue

                try:
                    intersection = geom_i.intersection(geom_j)
                except Exception:
                    continue

                if intersection.is_empty:
                    continue

                overlap_area_ha = intersection.area / 10000
                if overlap_area_ha <= 0:
                    continue

                # Calculate overlap as percentage of smaller plot
                smaller_area = min(area_i, area_j)
                if smaller_area <= 0:
                    continue

                overlap_pct = overlap_area_ha / smaller_area

                # Only flag if above threshold (>1%)
                if overlap_pct <= self.overlap_pct_threshold:
                    continue

                # Classify severity — thresholds from settings.yaml
                if overlap_pct < self.overlap_minor_fraction:
                    severity = "MINOR"
                    overlap_counts["minor"] += 1
                elif overlap_pct < self.overlap_significant_fraction:
                    severity = "SIGNIFICANT"
                    overlap_counts["significant"] += 1
                else:
                    severity = "MAJOR"
                    overlap_counts["major"] += 1

                overlap_sqm = round(float(overlap_area_ha * 10000), 4)
                overlap_pairs.append({
                    "plot_id_1":       pid_i,
                    "plot_id_2":       pid_j,
                    "overlap_area_ha": round(float(overlap_area_ha), 6),
                    "overlap_area_sqm": overlap_sqm,
                    "overlap_pct":     round(float(overlap_pct), 4),
                    "severity":        severity,
                })

                logger.warning(
                    f"  OVERLAP [{severity}] - '{pid_i}' <-> '{pid_j}' | "
                    f"area: {overlap_area_ha:.4f} ha | "
                    f"pct: {overlap_pct*100:.2f}%"
                )

            processed += 1
            if processed % 5000 == 0:
                logger.info(f"  Progress: {processed:,}/{len(valid_gdf):,} plots checked...")

        # Apply flags to GeoDataFrame
        plot_overlap_data = {}
        for pair in overlap_pairs:
            for pid in [pair["plot_id_1"], pair["plot_id_2"]]:
                if pid not in plot_overlap_data:
                    plot_overlap_data[pid] = {
                        "max_overlap_ha":   0.0,
                        "max_overlap_pct":  0.0,
                        "max_severity":     "",
                        "total_overlap_ha": 0.0,
                        "partners":         [],
                    }
                d = plot_overlap_data[pid]
                partner = pair["plot_id_2"] if pid == pair["plot_id_1"] else pair["plot_id_1"]
                if pair["overlap_pct"] > d["max_overlap_pct"]:
                    d["max_overlap_ha"]  = pair["overlap_area_ha"]
                    d["max_overlap_pct"] = pair["overlap_pct"]
                    d["max_severity"]    = pair["severity"]
                d["total_overlap_ha"] += pair["overlap_area_ha"]
                d["partners"].append(partner)

        for pid, data in plot_overlap_data.items():
            match = gdf["plot_id"] == pid
            total_sqm = round(data["total_overlap_ha"] * 10000, 4)
            max_sqm   = round(data["max_overlap_ha"] * 10000, 4)
            gdf.loc[match, "chk_overlap"]                = True
            gdf.loc[match, "chk_overlap_area_ha"]        = data["max_overlap_ha"]
            gdf.loc[match, "chk_overlap_area_sqm"]       = max_sqm
            gdf.loc[match, "chk_overlap_total_area_ha"]  = data["total_overlap_ha"]
            gdf.loc[match, "chk_overlap_total_area_sqm"] = total_sqm
            gdf.loc[match, "chk_overlap_pct"]            = data["max_overlap_pct"]
            gdf.loc[match, "chk_overlap_severity"]       = data["max_severity"]
            gdf.loc[match, "chk_overlap_partners"]       = ", ".join(data["partners"])
            gdf.loc[match, "chk_overlap_partner_count"]  = len(data["partners"])

        total_pairs      = len(overlap_pairs)
        total_plots_flag = len(plot_overlap_data)

        logger.info(f"  Overlap pairs found:   {total_pairs:,}")
        logger.info(f"  Plots with overlap:    {total_plots_flag:,}")
        logger.info(f"  Minor  (1-10%):        {overlap_counts['minor']:,} pairs")
        logger.info(f"  Significant (10-50%):  {overlap_counts['significant']:,} pairs")
        logger.info(f"  Major  (>50%):         {overlap_counts['major']:,} pairs")

        report["checks"]["overlaps"] = {
            "status":              "PASS" if total_pairs == 0 else "FAIL",
            "threshold_pct":       self.overlap_pct_threshold,
            "total_overlap_pairs": total_pairs,
            "total_plots_flagged": total_plots_flag,
            "severity_counts":     overlap_counts,
            "sample_pairs":        overlap_pairs[:50],
        }
        return gdf

    def _assign_phase_status(self, gdf: gpd.GeoDataFrame, report: dict) -> gpd.GeoDataFrame:
        gdf = gdf.copy()

        major_sig  = gdf["chk_overlap_severity"].isin(["MAJOR", "SIGNIFICANT"])
        minor_only = gdf["chk_overlap"] & ~major_sig

        gdf["phase_1e_status"] = "PASS"
        gdf.loc[minor_only, "phase_1e_status"] = "WARNING"
        gdf.loc[major_sig,  "phase_1e_status"] = "NEEDS_REVIEW"

        status_counts = gdf["phase_1e_status"].value_counts().to_dict()
        report["phase_1e_status_summary"] = status_counts
        return gdf

    def _write_output(self, gdf: gpd.GeoDataFrame, run_id: str) -> None:
        self.interim_dir.mkdir(parents=True, exist_ok=True)
        output_path = self.interim_dir / f"1E_plots_topology_checked_{run_id}.gpkg"
        logger.info(f"Writing output: {output_path.name}")
        gdf.to_file(output_path, driver="GPKG", layer="plots_topology_checked")
        logger.info("Output written successfully")

    def _write_error_geometries(self, gdf: gpd.GeoDataFrame, run_id: str) -> None:
        error_mask = gdf["chk_overlap"]
        error_gdf  = gdf.loc[error_mask].copy()
        if len(error_gdf) == 0:
            logger.info("No overlap geometries to export")
            return
        error_gdf = error_gdf.to_crs(epsg=self.output_epsg)
        output_path = self.outputs_dir / f"1E_overlap_plots_{run_id}.gpkg"
        self.outputs_dir.mkdir(parents=True, exist_ok=True)
        error_gdf.to_file(output_path, driver="GPKG", layer="overlap_plots")
        logger.info(f"Overlap plots exported: {output_path.name} ({len(error_gdf)} plots)")

    def _write_json_report(self, report: dict, run_id: str) -> None:
        self.outputs_dir.mkdir(parents=True, exist_ok=True)
        report_path = self.outputs_dir / f"1E_topology_report_{run_id}.json"
        with open(report_path, "w") as f:
            json.dump(report, f, indent=2, default=str)
        logger.info(f"JSON report written: {report_path.name}")

    def _write_csv_report(self, gdf: gpd.GeoDataFrame, run_id: str) -> None:
        self.outputs_dir.mkdir(parents=True, exist_ok=True)
        report_path = self.outputs_dir / f"1E_topology_report_{run_id}.csv"
        report_cols = [
            "plot_id", "area_ha", "phase_1e_status",
            "chk_overlap", "chk_overlap_area_ha", "chk_overlap_area_sqm",
            "chk_overlap_total_area_ha", "chk_overlap_total_area_sqm",
            "chk_overlap_pct", "chk_overlap_severity",
            "chk_overlap_partner_count", "chk_overlap_partners",
        ]
        available = [c for c in report_cols if c in gdf.columns]
        report_df = gdf[available].copy()
        sort_order = {"FAIL": 0, "WARNING": 1, "PASS": 2}
        report_df["_sort"] = report_df["phase_1e_status"].map(sort_order)
        report_df = report_df.sort_values("_sort").drop(columns=["_sort"])
        report_df.to_csv(report_path, index=False, encoding="utf-8")
        fail_count    = int((report_df["phase_1e_status"] == "FAIL").sum())
        warning_count = int((report_df["phase_1e_status"] == "WARNING").sum())
        pass_count    = int((report_df["phase_1e_status"] == "PASS").sum())
        logger.info(f"CSV report written: {report_path.name}")
        logger.info(
            f"  CSV rows - FAIL: {fail_count:,} | "
            f"WARNING: {warning_count:,} | "
            f"PASS: {pass_count:,} | "
            f"Total: {len(report_df):,}"
        )

    def _log_summary(self, gdf: gpd.GeoDataFrame, report: dict) -> None:
        checks        = report["checks"]
        status_summary = report.get("phase_1e_status_summary", {})
        sev            = report["checks"].get("overlaps", {}).get("severity_counts", {})

        logger.info("=" * 60)
        logger.info("PHASE 1E SUMMARY")
        logger.info("=" * 60)
        for check_name, result in checks.items():
            status = result.get("status", "UNKNOWN")
            logger.info(f"  {check_name:<35} {status}")
        logger.info("-" * 60)
        logger.info("  PLOT STATUS BREAKDOWN:")
        for status, count in sorted(status_summary.items()):
            logger.info(f"    {status:<15} {count:>8,} plots")
        logger.info("-" * 60)
        logger.info("  OVERLAP SEVERITY BREAKDOWN (pairs):")
        logger.info(f"    Minor  overlap (1-10%):    {sev.get('minor', 0):>8,} pairs")
        logger.info(f"    Significant (10-50%):      {sev.get('significant', 0):>8,} pairs")
        logger.info(f"    Major  overlap (>50%):     {sev.get('major', 0):>8,} pairs")
        logger.info("=" * 60)