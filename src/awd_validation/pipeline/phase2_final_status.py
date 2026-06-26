"""
Phase 2E — Phase 2 Final Status Assignment

Assigns final Phase 2 eligibility status combining all Phase 2 checks.
Produces master Phase 2 report, updated GeoPackages, and HTML map.

Output classification:
  FULLY_ELIGIBLE      — passes all Phase 2 checks, adjustment_factor = 1.0
  PARTIALLY_ELIGIBLE  — has non-eligible sub-areas, adjustment_factor < 1.0
  INELIGIBLE          — entire plot is non-eligible, adjustment_factor = 0.0
  NEEDS_REVIEW        — losing >25% of area, manual verification required
"""
from __future__ import annotations

import glob
import json
from datetime import datetime
from pathlib import Path
from typing import Tuple

import geopandas as gpd
import pandas as pd
from loguru import logger

from awd_validation.utils.config import compute_working_epsg


class Phase2FinalStatusAssigner:
    """
    Phase 2E — Final Phase 2 eligibility status assignment.

    Input:  2D_plots_net_area_{run_id}.gpkg            (interim)
    Output: 2E_eligible_plots_{run_id}.gpkg            (outputs)
            2E_partial_plots_{run_id}.gpkg             (outputs)
            2E_ineligible_plots_{run_id}.gpkg          (outputs)
            2E_review_plots_{run_id}.gpkg              (outputs)
            2E_master_eligibility_report_{run_id}.xlsx (outputs)
            2E_master_eligibility_report_{run_id}.csv  (outputs)
            2E_eligibility_map_{run_id}.html           (outputs)
            2E_phase2_summary_{run_id}.json            (outputs)
    """

    FULLY_ELIGIBLE = "FULLY_ELIGIBLE"
    PARTIALLY_ELIGIBLE = "PARTIALLY_ELIGIBLE"
    INELIGIBLE = "INELIGIBLE"
    NEEDS_REVIEW = "NEEDS_REVIEW"

    def __init__(self, config) -> None:
        self.config = config
        self.interim_dir = Path(config.input["interim_dir"])
        self.outputs_dir = Path(config.input["outputs_dir"])
        self.output_epsg = config.crs.get("output_epsg", 4326)
        self.fallback_epsg = config.crs.get("fallback_working_epsg", 32644)
        self.working_epsg: int = self.fallback_epsg  # set dynamically in _load_input
        self.phase2_cfg = config.get("phase2", default={})
        self.eligibility_cfg = self.phase2_cfg.get("eligibility", {})
        self.review_threshold_pct = self.eligibility_cfg.get(
            "review_threshold_pct", 75
        )
        self.map_sample_size = self.eligibility_cfg.get("map_sample_size", 3000)

    def run(self, run_id: str) -> Tuple[gpd.GeoDataFrame, dict]:
        logger.info("=" * 60)
        logger.info("PHASE 2E — FINAL ELIGIBILITY STATUS ASSIGNMENT")
        logger.info("=" * 60)

        report: dict = {
            "run_id": run_id,
            "phase": "2E",
            "timestamp": datetime.utcnow().isoformat(),
            "standard": self.config.project.get("standard", "VCS v5.0"),
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
        pattern = str(self.interim_dir / "2D_plots_net_area_*.gpkg")
        files = sorted(glob.glob(pattern))
        if not files:
            raise FileNotFoundError(
                "No Phase 2D output found. Run Phase 2D first."
            )
        latest = files[-1]
        logger.info(f"Loading Phase 2D output: {Path(latest).name}")
        gdf = gpd.read_file(latest)
        self.working_epsg = compute_working_epsg(gdf, self.fallback_epsg)
        if gdf.crs is None or gdf.crs.to_epsg() != self.working_epsg:
            gdf = gdf.to_crs(epsg=self.working_epsg)
        logger.info(f"Loaded {len(gdf):,} plots | working CRS: EPSG:{self.working_epsg}")
        report["input_file"] = latest
        report["total_plots"] = len(gdf)
        report["working_epsg"] = self.working_epsg
        return gdf

    def _assign_final_status(
        self, gdf: gpd.GeoDataFrame, report: dict
    ) -> gpd.GeoDataFrame:
        logger.info("Assigning Phase 2 final eligibility status...")
        gdf = gdf.copy()

        def classify(row) -> Tuple[str, str]:
            factor = row.get("adjustment_factor", 1.0)
            needs_review = row.get("needs_review", False)
            if factor == 0.0:
                return self.INELIGIBLE, "Entire plot covered by non-eligible area"
            if needs_review:
                eligible_pct = row.get("eligible_pct", 100.0) or 0.0
                loss_pct = 100.0 - eligible_pct
                # Avoid "Losing 100.0%" for plots that retain a tiny sliver
                if eligible_pct > 0 and round(loss_pct, 1) >= 100.0:
                    retained_str = "<0.1% retained"
                else:
                    retained_str = f"{eligible_pct:.1f}% retained"
                return (
                    self.NEEDS_REVIEW,
                    f"Losing {loss_pct:.1f}% of area ({retained_str}) — manual review required",
                )
            if factor < 1.0:
                non_eligible_ha = row.get("total_non_eligible_ha", 0.0)
                return (
                    self.PARTIALLY_ELIGIBLE,
                    f"Non-eligible area: {non_eligible_ha:.4f} ha | "
                    f"Eligible: {row.get('eligible_pct', 0):.1f}%",
                )
            return self.FULLY_ELIGIBLE, "All Phase 2 checks passed"

        results = gdf.apply(classify, axis=1)
        gdf["phase2_eligibility"] = results.apply(lambda r: r[0])
        gdf["phase2_eligibility_reason"] = results.apply(lambda r: r[1])

        counts = gdf["phase2_eligibility"].value_counts().to_dict()
        total = len(gdf)
        report["phase2_status_summary"] = {
            k: {"count": v, "pct": round(v / total * 100, 2)}
            for k, v in counts.items()
        }

        # Carbon summary
        total_eligible = float(gdf["eligible_area_ha"].sum()) if "eligible_area_ha" in gdf.columns else 0.0
        total_area     = float(gdf["area_ha"].sum()) if "area_ha" in gdf.columns else 0.0
        plot_mean_adj  = float(gdf["adjustment_factor"].mean()) if "adjustment_factor" in gdf.columns else None
        area_wtd_adj   = round(total_eligible / total_area, 4) if total_area > 0 else None

        report["carbon_summary"] = {
            "total_eligible_area_ha": round(total_eligible, 4),
            # plot_mean: arithmetic mean across plots (each plot weighted equally)
            "plot_mean_adj_factor": round(plot_mean_adj, 4) if plot_mean_adj is not None else None,
            # area_weighted: correct portfolio-level factor = eligible_ha / total_ha
            # Use this for carbon calculations — required by VM0051 / VCS Standard 5
            "area_weighted_adj_factor": area_wtd_adj,
            "avg_adjustment_factor": round(plot_mean_adj, 4) if plot_mean_adj is not None else None,
            "plots_needing_review": int(
                (gdf["phase2_eligibility"] == self.NEEDS_REVIEW).sum()
            ),
            "note": (
                "Carbon stock must be calculated using eligible_area_ha × "
                "baseline_carbon_per_ha. Do NOT use total area when "
                "adjustment_factor < 1.0 (VCS Standard 5). "
                "Use area_weighted_adj_factor for portfolio-level carbon accounting."
            ),
        }

        logger.info("Phase 2 status assigned:")
        for status, count in sorted(counts.items()):
            logger.info(
                f"  {status:<25} {count:>8,} plots "
                f"({count / total * 100:.2f}%)"
            )
        return gdf

    def _write_split_outputs(
        self, gdf: gpd.GeoDataFrame, run_id: str, report: dict
    ) -> None:
        self.outputs_dir.mkdir(parents=True, exist_ok=True)
        gdf_out = gdf.to_crs(epsg=self.output_epsg)
        status_map = {
            self.FULLY_ELIGIBLE:   "2E_eligible_plots",
            self.PARTIALLY_ELIGIBLE: "2E_partial_plots",
            self.INELIGIBLE:       "2E_ineligible_plots",
            self.NEEDS_REVIEW:     "2E_review_plots",
        }
        for status, filename in status_map.items():
            subset = gdf_out.loc[
                gdf_out["phase2_eligibility"] == status
            ].copy()
            if len(subset) == 0:
                continue
            path = self.outputs_dir / f"{filename}_{run_id}.gpkg"
            subset.to_file(path, driver="GPKG", layer=filename)
            logger.info(f"  Written: {path.name} ({len(subset):,} plots)")

    def _write_master_csv(self, gdf: gpd.GeoDataFrame, run_id: str) -> None:
        cols = [
            "plot_id", "area_ha",
            "phase2_eligibility", "phase2_eligibility_reason",
            "forest_overlap_ha", "water_overlap_ha",
            "roads_overlap_ha", "railways_overlap_ha",
            "settlements_overlap_ha", "buildings_overlap_ha",
            "total_non_eligible_ha", "eligible_area_ha",
            "eligible_pct", "adjustment_factor", "needs_review",
            "phase_2a_status", "phase_2b_status",
            "phase_2c_status", "phase_2d_status",
        ]
        available = [c for c in cols if c in gdf.columns]
        sort_order = {
            self.INELIGIBLE: 0, self.NEEDS_REVIEW: 1,
            self.PARTIALLY_ELIGIBLE: 2, self.FULLY_ELIGIBLE: 3,
        }
        df = gdf[available].copy()
        df["_sort"] = df["phase2_eligibility"].map(sort_order)
        df = df.sort_values("_sort").drop(columns=["_sort"])
        path = self.outputs_dir / f"2E_master_eligibility_report_{run_id}.csv"
        df.to_csv(path, index=False, encoding="utf-8")
        logger.info(f"Master CSV written: {path.name}")

    def _write_master_excel(self, gdf: gpd.GeoDataFrame, run_id: str) -> None:
        try:
            from openpyxl.styles import PatternFill, Font
            cols = [
                "plot_id", "area_ha",
                "phase2_eligibility", "phase2_eligibility_reason",
                "total_non_eligible_ha", "eligible_area_ha",
                "eligible_pct", "adjustment_factor", "needs_review",
                "forest_overlap_ha", "water_overlap_ha",
                "roads_overlap_ha", "railways_overlap_ha",
                "settlements_overlap_ha", "buildings_overlap_ha",
            ]
            available = [c for c in cols if c in gdf.columns]
            df = gdf[available].copy()
            sort_order = {
                self.INELIGIBLE: 0, self.NEEDS_REVIEW: 1,
                self.PARTIALLY_ELIGIBLE: 2, self.FULLY_ELIGIBLE: 3,
            }
            df["_sort"] = df["phase2_eligibility"].map(sort_order)
            df = df.sort_values("_sort").drop(columns=["_sort"])
            path = (
                self.outputs_dir
                / f"2E_master_eligibility_report_{run_id}.xlsx"
            )
            colors = {
                self.FULLY_ELIGIBLE:     "C6EFCE",
                self.PARTIALLY_ELIGIBLE: "FFEB9C",
                self.NEEDS_REVIEW:       "D9D9D9",
                self.INELIGIBLE:         "FFC7CE",
            }
            with pd.ExcelWriter(path, engine="openpyxl") as writer:
                # Sheet 1: All plots
                df.to_excel(
                    writer, index=False, sheet_name="All Plots"
                )
                ws = writer.sheets["All Plots"]
                status_col = list(df.columns).index("phase2_eligibility") + 1
                for row_idx in range(2, len(df) + 2):
                    status = ws.cell(row=row_idx, column=status_col).value
                    fill = PatternFill(
                        start_color=colors.get(status, "FFFFFF"),
                        end_color=colors.get(status, "FFFFFF"),
                        fill_type="solid",
                    )
                    for col_idx in range(1, len(df.columns) + 1):
                        ws.cell(row=row_idx, column=col_idx).fill = fill
                for col_idx in range(1, len(df.columns) + 1):
                    ws.cell(row=1, column=col_idx).font = Font(bold=True)
                for col in ws.columns:
                    max_len = max(
                        len(str(cell.value or "")) for cell in col
                    )
                    ws.column_dimensions[
                        col[0].column_letter
                    ].width = min(max_len + 2, 40)

                # Sheet 2: Carbon adjustment summary
                carbon_df = df[
                    [c for c in [
                        "plot_id", "area_ha", "eligible_area_ha",
                        "eligible_pct", "adjustment_factor", "needs_review",
                    ] if c in df.columns]
                ].copy()
                carbon_df.to_excel(
                    writer, index=False, sheet_name="Carbon Adjustment"
                )

                # Sheet 3: Needs review
                review_df = df[
                    df["phase2_eligibility"] == self.NEEDS_REVIEW
                ] if "phase2_eligibility" in df.columns else pd.DataFrame()
                if len(review_df) > 0:
                    review_df.to_excel(
                        writer, index=False, sheet_name="Needs Review"
                    )

            logger.info(f"Master Excel written: {path.name}")
        except Exception as e:
            logger.warning(f"Excel report failed: {e}")

    def _write_html_map(self, gdf: gpd.GeoDataFrame, run_id: str) -> None:
        try:
            import folium
            logger.info("Generating Phase 2 eligibility map...")
            gdf_wgs = gdf.to_crs(epsg=self.output_epsg)
            bounds = gdf_wgs.total_bounds
            center = [
                (bounds[1] + bounds[3]) / 2,
                (bounds[0] + bounds[2]) / 2,
            ]
            m = folium.Map(
                location=center, zoom_start=11, tiles="OpenStreetMap"
            )
            colors = {
                self.FULLY_ELIGIBLE:     "#2ecc71",
                self.PARTIALLY_ELIGIBLE: "#f39c12",
                self.NEEDS_REVIEW:       "#e67e22",
                self.INELIGIBLE:         "#e74c3c",
            }
            sample = (
                gdf_wgs.sample(n=self.map_sample_size, random_state=42)
                if len(gdf_wgs) > self.map_sample_size else gdf_wgs
            )
            for _, row in sample.iterrows():
                if row.geometry is None:
                    continue
                color = colors.get(
                    row.get("phase2_eligibility", ""), "#95a5a6"
                )
                eligible_ha = row.get("eligible_area_ha", row["area_ha"])
                factor = row.get("adjustment_factor", 1.0)
                tooltip = (
                    f"ID: {row['plot_id']}<br>"
                    f"Status: {row.get('phase2_eligibility', 'N/A')}<br>"
                    f"Total area: {row['area_ha']:.4f} ha<br>"
                    f"Eligible: {eligible_ha:.4f} ha<br>"
                    f"Adjustment factor: {factor:.4f}"
                )
                folium.GeoJson(
                    row.geometry.__geo_interface__,
                    style_function=lambda x, c=color: {
                        "fillColor": c, "color": c,
                        "weight": 1, "fillOpacity": 0.6,
                    },
                    tooltip=folium.Tooltip(tooltip),
                ).add_to(m)
            path = (
                self.outputs_dir / f"2E_eligibility_map_{run_id}.html"
            )
            m.save(str(path))
            logger.info(f"HTML map written: {path.name}")
        except Exception as e:
            logger.warning(f"HTML map failed: {e}")

    def _write_json_report(self, report: dict, run_id: str) -> None:
        self.outputs_dir.mkdir(parents=True, exist_ok=True)
        path = self.outputs_dir / f"2E_phase2_summary_{run_id}.json"
        with open(path, "w") as f:
            json.dump(report, f, indent=2, default=str)
        logger.info(f"Phase 2 summary written: {path.name}")

    def _log_summary(self, gdf: gpd.GeoDataFrame, report: dict) -> None:
        summary = report.get("phase2_status_summary", {})
        carbon = report.get("carbon_summary", {})
        total = report.get("total_plots", len(gdf))
        logger.info("=" * 60)
        logger.info("PHASE 2E — FINAL PHASE 2 SUMMARY")
        logger.info("=" * 60)
        logger.info(f"  Total plots:           {total:,}")
        logger.info("-" * 60)
        for status in [
            self.FULLY_ELIGIBLE,
            self.PARTIALLY_ELIGIBLE,
            self.NEEDS_REVIEW,
            self.INELIGIBLE,
        ]:
            if status in summary:
                d = summary[status]
                logger.info(
                    f"  {status:<25} {d['count']:>8,} plots "
                    f"({d['pct']:.2f}%)"
                )
        logger.info("-" * 60)
        logger.info("  CARBON ACCOUNTING:")
        logger.info(
            f"  Total eligible area:       "
            f"{carbon.get('total_eligible_area_ha', 0):>10,.2f} ha"
        )
        logger.info(
            f"  Plot-mean adj factor:      "
            f"{carbon.get('plot_mean_adj_factor', 1.0):>10.4f}  (each plot weighted equally)"
        )
        logger.info(
            f"  Area-weighted adj factor:  "
            f"{carbon.get('area_weighted_adj_factor', 1.0):>10.4f}  (use for carbon accounting — VM0051)"
        )
        logger.info(
            f"  Plots for review:          "
            f"{carbon.get('plots_needing_review', 0):>10,}"
        )
        logger.info("=" * 60)
