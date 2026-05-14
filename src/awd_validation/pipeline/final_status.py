from pathlib import Path
from datetime import datetime
from typing import Tuple
import geopandas as gpd
import pandas as pd
import numpy as np
from loguru import logger
import json


class FinalStatusAssigner:

    ELIGIBLE           = "ELIGIBLE"
    INELIGIBLE         = "INELIGIBLE"
    PARTIALLY_ELIGIBLE = "PARTIALLY_ELIGIBLE"
    NEEDS_REVIEW       = "NEEDS_REVIEW"

    def __init__(self, config):
        self.config      = config
        self.interim_dir = Path(config.input["interim_dir"])
        self.outputs_dir = Path(config.input["outputs_dir"])

    def run(self, run_id: str) -> Tuple[gpd.GeoDataFrame, dict]:
        logger.info("=" * 60)
        logger.info("PHASE 1F - FINAL STATUS ASSIGNMENT")
        logger.info("=" * 60)
        report = {
            "run_id":    run_id,
            "phase":     "1F",
            "timestamp": datetime.utcnow().isoformat(),
        }
        gdf = self._load_input(run_id, report)
        gdf = self._assign_final_status(gdf, report)
        self._write_split_outputs(gdf, run_id, report)
        self._write_master_csv(gdf, run_id)
        self._write_master_excel(gdf, run_id)
        self._write_html_map(gdf, run_id)
        self._write_json_report(report, run_id)
        self._log_summary(gdf, report)
        return gdf, report

    def _load_input(self, run_id: str, report: dict) -> gpd.GeoDataFrame:
        import glob
        pattern = str(self.interim_dir / "1E_plots_topology_checked_*.gpkg")
        files   = sorted(glob.glob(pattern))
        if not files:
            raise FileNotFoundError("No Phase 1E output found. Run Phase 1E first.")
        latest = files[-1]
        logger.info(f"Loading Phase 1E output: {Path(latest).name}")
        gdf = gpd.read_file(latest, layer="plots_topology_checked")
        logger.info(f"Loaded {len(gdf):,} plots")
        report["input_file"]  = latest
        report["total_plots"] = len(gdf)
        return gdf

    def _assign_final_status(self, gdf: gpd.GeoDataFrame, report: dict) -> gpd.GeoDataFrame:
        logger.info("Assigning final eligibility status to all plots...")
        gdf = gdf.copy()

        def get_status(row):
            if row.get("phase_1b_status") == "FAIL":
                return self.INELIGIBLE, "Geometry invalid or null"
            if row.get("phase_1c_status") == "FAIL":
                return self.INELIGIBLE, "Invalid or duplicate ID"
            if row.get("phase_1d_status") == "FAIL":
                return self.INELIGIBLE, "Spatial duplicate"
            if row.get("phase_1e_status") == "FAIL":
                pct = row.get("chk_overlap_pct", 0) * 100
                return self.INELIGIBLE, f"Major or significant overlap ({pct:.1f}%)"
            if row.get("phase_1e_status") == "WARNING":
                pct = row.get("chk_overlap_pct", 0) * 100
                return self.PARTIALLY_ELIGIBLE, f"Minor overlap ({pct:.1f}%) - review boundary"
            if row.get("phase_1b_status") == "WARNING" or row.get("phase_1c_status") == "WARNING":
                return self.NEEDS_REVIEW, "Minor issue flagged - human review required"
            return self.ELIGIBLE, "All checks passed"

        results = gdf.apply(get_status, axis=1)
        gdf["final_eligibility"]        = results.apply(lambda r: r[0])
        gdf["final_eligibility_reason"] = results.apply(lambda r: r[1])

        counts = gdf["final_eligibility"].value_counts().to_dict()
        total  = len(gdf)
        report["final_status_summary"] = {
            k: {"count": v, "pct": round(v / total * 100, 2)}
            for k, v in counts.items()
        }
        logger.info("Final status assigned:")
        for status, count in sorted(counts.items()):
            logger.info(f"  {status:<25} {count:>8,} plots ({count/total*100:.2f}%)")
        return gdf

    def _write_split_outputs(self, gdf: gpd.GeoDataFrame, run_id: str, report: dict) -> None:
        self.outputs_dir.mkdir(parents=True, exist_ok=True)
        gdf_out = gdf.to_crs(epsg=4326)
        status_map = {
            self.ELIGIBLE:           "valid_plots",
            self.INELIGIBLE:         "ineligible_plots",
            self.PARTIALLY_ELIGIBLE: "partial_plots",
            self.NEEDS_REVIEW:       "needs_review_plots",
        }
        output_paths = {}
        for status, filename in status_map.items():
            subset = gdf_out.loc[gdf_out["final_eligibility"] == status].copy()
            if len(subset) == 0:
                logger.info(f"  No plots with status {status} - skipping")
                continue
            path = self.outputs_dir / f"{filename}_{run_id}.gpkg"
            subset.to_file(path, driver="GPKG", layer=filename)
            output_paths[status] = str(path)
            logger.info(f"  Written: {path.name} ({len(subset):,} plots)")
        report["output_files"] = output_paths

    def _write_master_csv(self, gdf: gpd.GeoDataFrame, run_id: str) -> None:
        report_cols = [
            "plot_id", "area_ha",
            "final_eligibility", "final_eligibility_reason",
            "phase_1b_status", "phase_1c_status",
            "phase_1d_status", "phase_1e_status",
            "chk_null_geom", "chk_is_valid", "chk_is_simple",
            "chk_id_format", "chk_special_chars", "chk_id_duplicate",
            "chk_centroid_duplicate", "chk_centroid_distance_m",
            "chk_area_centroid_match",
            "chk_overlap", "chk_overlap_area_ha",
            "chk_overlap_pct", "chk_overlap_severity", "chk_overlap_partners",
            "source_file", "ingestion_timestamp",
        ]
        available = [c for c in report_cols if c in gdf.columns]
        report_df = gdf[available].copy()
        sort_order = {
            self.INELIGIBLE: 0, self.PARTIALLY_ELIGIBLE: 1,
            self.NEEDS_REVIEW: 2, self.ELIGIBLE: 3,
        }
        report_df["_sort"] = report_df["final_eligibility"].map(sort_order)
        report_df = report_df.sort_values("_sort").drop(columns=["_sort"])
        path = self.outputs_dir / f"1F_master_validation_report_{run_id}.csv"
        report_df.to_csv(path, index=False, encoding="utf-8")
        logger.info(f"Master CSV written: {path.name} ({len(report_df):,} rows)")

    def _write_master_excel(self, gdf: gpd.GeoDataFrame, run_id: str) -> None:
        try:
            import openpyxl
            from openpyxl.styles import PatternFill, Font
            report_cols = [
                "plot_id", "area_ha",
                "final_eligibility", "final_eligibility_reason",
                "phase_1b_status", "phase_1c_status",
                "phase_1d_status", "phase_1e_status",
                "chk_overlap_severity", "chk_overlap_pct",
            ]
            available = [c for c in report_cols if c in gdf.columns]
            df = gdf[available].copy()
            sort_order = {
                self.INELIGIBLE: 0, self.PARTIALLY_ELIGIBLE: 1,
                self.NEEDS_REVIEW: 2, self.ELIGIBLE: 3,
            }
            df["_sort"] = df["final_eligibility"].map(sort_order)
            df = df.sort_values("_sort").drop(columns=["_sort"])
            path = self.outputs_dir / f"1F_master_validation_report_{run_id}.xlsx"
            with pd.ExcelWriter(path, engine="openpyxl") as writer:
                df.to_excel(writer, index=False, sheet_name="Validation Results")
                ws = writer.sheets["Validation Results"]
                colors = {
                    self.ELIGIBLE:           "C6EFCE",
                    self.INELIGIBLE:         "FFC7CE",
                    self.PARTIALLY_ELIGIBLE: "FFEB9C",
                    self.NEEDS_REVIEW:       "D9D9D9",
                }
                status_col_idx = list(df.columns).index("final_eligibility") + 1
                for row_idx in range(2, len(df) + 2):
                    status = ws.cell(row=row_idx, column=status_col_idx).value
                    fill_color = colors.get(status, "FFFFFF")
                    fill = PatternFill(
                        start_color=fill_color,
                        end_color=fill_color,
                        fill_type="solid"
                    )
                    for col_idx in range(1, len(df.columns) + 1):
                        ws.cell(row=row_idx, column=col_idx).fill = fill
                for col_idx in range(1, len(df.columns) + 1):
                    ws.cell(row=1, column=col_idx).font = Font(bold=True)
                for col in ws.columns:
                    max_len = max(len(str(cell.value or "")) for cell in col)
                    ws.column_dimensions[col[0].column_letter].width = min(max_len + 2, 40)
            logger.info(f"Master Excel written: {path.name}")
        except Exception as e:
            logger.warning(f"Excel report failed: {e}")

    def _write_html_map(self, gdf: gpd.GeoDataFrame, run_id: str) -> None:
        try:
            import folium
            logger.info("Generating interactive HTML map...")
            gdf_wgs = gdf.to_crs(epsg=4326)
            bounds  = gdf_wgs.total_bounds
            center  = [(bounds[1] + bounds[3]) / 2, (bounds[0] + bounds[2]) / 2]
            m       = folium.Map(location=center, zoom_start=11, tiles="OpenStreetMap")
            colors  = {
                self.ELIGIBLE:           "#2ecc71",
                self.INELIGIBLE:         "#e74c3c",
                self.PARTIALLY_ELIGIBLE: "#f39c12",
                self.NEEDS_REVIEW:       "#95a5a6",
            }
            sample_size = min(3000, len(gdf_wgs))
            sample_gdf  = gdf_wgs.sample(n=sample_size, random_state=42) if len(gdf_wgs) > sample_size else gdf_wgs
            for _, row in sample_gdf.iterrows():
                if row.geometry is None:
                    continue
                color   = colors.get(row["final_eligibility"], "#95a5a6")
                tooltip = (
                    "ID: " + str(row["plot_id"]) + "<br>" +
                    "Status: " + str(row["final_eligibility"]) + "<br>" +
                    "Area: " + str(round(row["area_ha"], 4)) + " ha<br>" +
                    "Reason: " + str(row.get("final_eligibility_reason", ""))
                )
                folium.GeoJson(
                    row.geometry.__geo_interface__,
                    style_function=lambda x, c=color: {
                        "fillColor": c, "color": c,
                        "weight": 1, "fillOpacity": 0.6,
                    },
                    tooltip=folium.Tooltip(tooltip),
                ).add_to(m)
            path = self.outputs_dir / f"1F_validation_map_{run_id}.html"
            m.save(str(path))
            logger.info(f"HTML map written: {path.name} ({sample_size:,} plots shown)")
        except Exception as e:
            logger.warning(f"HTML map failed: {e}")

    def _write_json_report(self, report: dict, run_id: str) -> None:
        self.outputs_dir.mkdir(parents=True, exist_ok=True)
        path = self.outputs_dir / f"1F_run_summary_{run_id}.json"
        with open(path, "w") as f:
            json.dump(report, f, indent=2, default=str)
        logger.info(f"Run summary written: {path.name}")

    def _log_summary(self, gdf: gpd.GeoDataFrame, report: dict) -> None:
        summary = report.get("final_status_summary", {})
        total   = report.get("total_plots", len(gdf))
        logger.info("=" * 60)
        logger.info("PHASE 1F - FINAL SUMMARY")
        logger.info("=" * 60)
        logger.info(f"  Total plots processed:    {total:,}")
        logger.info("-" * 60)
        for status in [self.ELIGIBLE, self.PARTIALLY_ELIGIBLE, self.NEEDS_REVIEW, self.INELIGIBLE]:
            if status in summary:
                count = summary[status]["count"]
                pct   = summary[status]["pct"]
                logger.info(f"  {status:<25} {count:>8,} plots ({pct:.2f}%)")
        logger.info("-" * 60)
        logger.info("  OUTPUT FILES:")
        logger.info("    valid_plots.gpkg              ELIGIBLE plots")
        logger.info("    ineligible_plots.gpkg         INELIGIBLE plots")
        logger.info("    partial_plots.gpkg            PARTIALLY ELIGIBLE")
        logger.info("    needs_review_plots.gpkg       NEEDS REVIEW")
        logger.info("    1F_master_validation_report   CSV + Excel")
        logger.info("    1F_validation_map.html        Interactive map")
        logger.info("    1F_run_summary.json           Audit trail")
        logger.info("=" * 60)
