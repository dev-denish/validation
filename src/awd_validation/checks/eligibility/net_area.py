"""
Phase 2D — Net Eligible Area Calculation

Aggregates all non-eligible areas from Phases 2A/2B/2C and calculates:
  - total_non_eligible_ha  = forest + water + roads + railways + settlements + buildings
  - eligible_area_ha       = total_area_ha - total_non_eligible_ha
  - eligible_pct           = (eligible_area_ha / total_area_ha) * 100
  - adjustment_factor      = eligible_area_ha / total_area_ha  [0.0–1.0]
  - needs_review           = adjustment_factor < 0.75 (losing >25% area)

CRITICAL (VCS Standard 5):
  Carbon stock MUST be calculated on eligible_area_ha only.
  NEVER use total_area_ha when non-eligible areas exist.
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


class NetAreaCalculator:
    """
    Phase 2D — Net eligible area and carbon adjustment factor calculation.

    Input:  2C_plots_infra_checked_{run_id}.gpkg  (interim)
    Output: 2D_plots_net_area_{run_id}.gpkg        (interim)
            2D_net_area_report_{run_id}.json        (outputs)
            2D_net_area_report_{run_id}.csv         (outputs)
    """

    WORKING_EPSG = 32643
    OUTPUT_EPSG = 4326

    # Non-eligible area columns from Phases 2A/2B/2C
    NON_ELIGIBLE_COLS = [
        "forest_overlap_ha",
        "water_overlap_ha",
        "roads_overlap_ha",
        "railways_overlap_ha",
        "settlements_overlap_ha",
        "buildings_overlap_ha",
    ]

    def __init__(self, config) -> None:
        self.config = config
        self.interim_dir = Path(config.input["interim_dir"])
        self.outputs_dir = Path(config.input["outputs_dir"])
        self.phase2_cfg = config.get("phase2", default={})
        self.eligibility_cfg = self.phase2_cfg.get("eligibility", {})
        self.review_threshold_pct = self.eligibility_cfg.get(
            "review_threshold_pct", 75
        )
        self.adjustment_decimals = self.eligibility_cfg.get(
            "adjustment_factor_decimals", 4
        )

    def run(self, run_id: str) -> Tuple[gpd.GeoDataFrame, dict]:
        logger.info("=" * 60)
        logger.info("PHASE 2D — NET ELIGIBLE AREA CALCULATION")
        logger.info("=" * 60)

        report: dict = {
            "run_id": run_id,
            "phase": "2D",
            "timestamp": datetime.utcnow().isoformat(),
            "review_threshold_pct": self.review_threshold_pct,
            "checks": {},
        }

        gdf = self._load_input(run_id, report)
        gdf = self._calculate_net_area(gdf, report)
        gdf = self._calculate_adjustment_factor(gdf, report)
        gdf = self._flag_for_review(gdf, report)
        gdf = self._assign_phase_status(gdf, report)
        self._write_output(gdf, run_id)
        self._write_json_report(report, run_id)
        self._write_csv_report(gdf, run_id)
        self._log_summary(gdf, report)

        return gdf, report

    def _load_input(self, run_id: str, report: dict) -> gpd.GeoDataFrame:
        pattern = str(self.interim_dir / "2C_plots_infra_checked_*.gpkg")
        files = sorted(glob.glob(pattern))
        if not files:
            raise FileNotFoundError(
                "No Phase 2C output found. Run Phase 2C first."
            )
        latest = files[-1]
        logger.info(f"Loading Phase 2C output: {Path(latest).name}")
        gdf = gpd.read_file(latest)
        if gdf.crs.to_epsg() != self.WORKING_EPSG:
            gdf = gdf.to_crs(epsg=self.WORKING_EPSG)
        logger.info(f"Loaded {len(gdf):,} plots")
        report["input_file"] = latest
        report["total_plots"] = len(gdf)
        return gdf

    def _calculate_net_area(
        self, gdf: gpd.GeoDataFrame, report: dict
    ) -> gpd.GeoDataFrame:
        """
        Sum all non-eligible overlap areas per plot.
        Only uses columns that actually exist — graceful when phases were SKIPPED.
        """
        logger.info("Summing non-eligible areas from all Phase 2 checks...")
        gdf = gdf.copy()
        present_cols = [
            c for c in self.NON_ELIGIBLE_COLS if c in gdf.columns
        ]
        missing_cols = [
            c for c in self.NON_ELIGIBLE_COLS if c not in gdf.columns
        ]
        if missing_cols:
            logger.warning(
                f"Missing non-eligible columns (phases SKIPPED): {missing_cols}"
            )
            for col in missing_cols:
                gdf[col] = 0.0

        gdf["total_non_eligible_ha"] = gdf[self.NON_ELIGIBLE_COLS].sum(axis=1)
        # Eligible area cannot be negative
        gdf["eligible_area_ha"] = (
            gdf["area_ha"] - gdf["total_non_eligible_ha"]
        ).clip(lower=0.0)

        area_stats = {
            "total_plots_area_ha": round(float(gdf["area_ha"].sum()), 4),
            "total_non_eligible_ha": round(
                float(gdf["total_non_eligible_ha"].sum()), 4
            ),
            "total_eligible_ha": round(
                float(gdf["eligible_area_ha"].sum()), 4
            ),
            "breakdown": {
                col: round(float(gdf[col].sum()), 4)
                for col in self.NON_ELIGIBLE_COLS
            },
        }
        logger.info(
            f"  Total area:        {area_stats['total_plots_area_ha']:,} ha"
        )
        logger.info(
            f"  Non-eligible:      {area_stats['total_non_eligible_ha']:,} ha"
        )
        logger.info(
            f"  Eligible:          {area_stats['total_eligible_ha']:,} ha"
        )
        report["area_summary"] = area_stats
        return gdf

    def _calculate_adjustment_factor(
        self, gdf: gpd.GeoDataFrame, report: dict
    ) -> gpd.GeoDataFrame:
        """
        Carbon adjustment factor = eligible_area_ha / total_area_ha.
        MUST be between 0.0 and 1.0.
        This is the multiplier applied to baseline carbon stock per hectare.
        """
        logger.info("Calculating carbon adjustment factors...")
        gdf = gdf.copy()
        gdf["eligible_pct"] = (
            gdf["eligible_area_ha"] / gdf["area_ha"] * 100
        ).where(gdf["area_ha"] > 0, other=0.0).round(2)
        gdf["adjustment_factor"] = (
            gdf["eligible_area_ha"] / gdf["area_ha"]
        ).where(gdf["area_ha"] > 0, other=0.0).round(self.adjustment_decimals)
        # Enforce bounds — adjustment_factor must be [0.0, 1.0]
        assert (
            gdf["adjustment_factor"].between(0.0, 1.0).all()
        ), "CRITICAL: adjustment_factor out of [0, 1] range — check area calculations"

        logger.info(
            f"  Adjustment factor — "
            f"min: {gdf['adjustment_factor'].min():.4f} | "
            f"mean: {gdf['adjustment_factor'].mean():.4f} | "
            f"max: {gdf['adjustment_factor'].max():.4f}"
        )
        report["adjustment_factor_stats"] = {
            "min": round(float(gdf["adjustment_factor"].min()), 4),
            "mean": round(float(gdf["adjustment_factor"].mean()), 4),
            "max": round(float(gdf["adjustment_factor"].max()), 4),
            "plots_fully_eligible": int(
                (gdf["adjustment_factor"] == 1.0).sum()
            ),
            "plots_partially_eligible": int(
                gdf["adjustment_factor"].between(0.0, 1.0, inclusive="neither").sum()
            ),
            "plots_fully_ineligible": int(
                (gdf["adjustment_factor"] == 0.0).sum()
            ),
        }
        return gdf

    def _flag_for_review(
        self, gdf: gpd.GeoDataFrame, report: dict
    ) -> gpd.GeoDataFrame:
        """
        Flag plots losing >25% of their area for manual verification.
        Jibotosh must review these before final carbon calculations.
        """
        gdf = gdf.copy()
        gdf["needs_review"] = (
            gdf["adjustment_factor"] < (self.review_threshold_pct / 100)
        )
        review_count = int(gdf["needs_review"].sum())
        if review_count > 0:
            logger.warning(
                f"  {review_count:,} plots losing >{100 - self.review_threshold_pct}% "
                f"of area — flagged for Jibotosh review"
            )
            for pid in gdf.loc[gdf["needs_review"], "plot_id"].values[:5]:
                row = gdf.loc[gdf["plot_id"] == pid].iloc[0]
                logger.warning(
                    f"    REVIEW — {pid} | "
                    f"eligible: {row['eligible_pct']:.1f}% | "
                    f"factor: {row['adjustment_factor']:.4f}"
                )
        report["review_flags"] = {
            "threshold_pct": self.review_threshold_pct,
            "plots_needing_review": review_count,
            "plot_ids": list(
                gdf.loc[gdf["needs_review"], "plot_id"].values[:50]
            ),
        }
        return gdf

    def _assign_phase_status(
        self, gdf: gpd.GeoDataFrame, report: dict
    ) -> gpd.GeoDataFrame:
        gdf = gdf.copy()

        def status(row) -> str:
            if row["adjustment_factor"] == 0.0:
                return "INELIGIBLE"
            if row["adjustment_factor"] == 1.0:
                return "FULLY_ELIGIBLE"
            return "PARTIALLY_ELIGIBLE"

        gdf["phase_2d_status"] = gdf.apply(status, axis=1)
        counts = gdf["phase_2d_status"].value_counts().to_dict()
        total = len(gdf)
        report["phase_2d_status_summary"] = {
            k: {"count": v, "pct": round(v / total * 100, 2)}
            for k, v in counts.items()
        }
        return gdf

    def _write_output(self, gdf: gpd.GeoDataFrame, run_id: str) -> None:
        self.interim_dir.mkdir(parents=True, exist_ok=True)
        path = self.interim_dir / f"2D_plots_net_area_{run_id}.gpkg"
        gdf.to_crs(epsg=self.OUTPUT_EPSG).to_file(
            path, driver="GPKG", layer="plots_net_area"
        )
        logger.info(f"Output written: {path.name}")

    def _write_json_report(self, report: dict, run_id: str) -> None:
        self.outputs_dir.mkdir(parents=True, exist_ok=True)
        path = self.outputs_dir / f"2D_net_area_report_{run_id}.json"
        with open(path, "w") as f:
            json.dump(report, f, indent=2, default=str)
        logger.info(f"JSON report written: {path.name}")

    def _write_csv_report(self, gdf: gpd.GeoDataFrame, run_id: str) -> None:
        cols = [
            "plot_id", "area_ha",
            "forest_overlap_ha", "water_overlap_ha",
            "roads_overlap_ha", "railways_overlap_ha",
            "settlements_overlap_ha", "buildings_overlap_ha",
            "total_non_eligible_ha", "eligible_area_ha",
            "eligible_pct", "adjustment_factor",
            "needs_review", "phase_2d_status",
        ]
        available = [c for c in cols if c in gdf.columns]
        path = self.outputs_dir / f"2D_net_area_report_{run_id}.csv"
        gdf[available].to_csv(path, index=False, encoding="utf-8")
        logger.info(f"CSV report written: {path.name}")

    def _log_summary(self, gdf: gpd.GeoDataFrame, report: dict) -> None:
        summary = report.get("phase_2d_status_summary", {})
        area = report.get("area_summary", {})
        logger.info("=" * 60)
        logger.info("PHASE 2D SUMMARY")
        logger.info("=" * 60)
        logger.info(f"  Total plots area:   {area.get('total_plots_area_ha', 0):>12,.2f} ha")
        logger.info(f"  Non-eligible area:  {area.get('total_non_eligible_ha', 0):>12,.2f} ha")
        logger.info(f"  Eligible area:      {area.get('total_eligible_ha', 0):>12,.2f} ha")
        logger.info("-" * 60)
        for status, data in sorted(summary.items()):
            logger.info(
                f"  {status:<25} {data['count']:>8,} plots "
                f"({data['pct']:.2f}%)"
            )
        logger.info(
            f"  Flagged for review: {report.get('review_flags', {}).get('plots_needing_review', 0):>8,} plots"
        )
        logger.info("=" * 60)
