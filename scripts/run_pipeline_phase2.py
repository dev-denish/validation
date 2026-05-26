"""
AWD Validation Pipeline — Phase 2 Full Run (2A through 2E)
Usage: python scripts/run_pipeline_phase2.py

Runs all Phase 2 eligibility checks in sequence:
  2A → Forest overlap check
  2B → Water body overlap check
  2C → Infrastructure overlap check
  2D → Net eligible area calculation
  2E → Final Phase 2 status assignment

All checks have enabled: false by default until GIS layers
are received from Jibotosh. The pipeline runs through all phases
regardless — SKIPPED phases pass all plots through with full
traceability columns for audit purposes.
"""
import sys
import time
from datetime import datetime

sys.path.insert(0, "/app/src")

from awd_validation.utils.logger import setup_logger
from awd_validation.utils.config import Config
from awd_validation.checks.eligibility.forest import ForestOverlapChecker
from awd_validation.checks.eligibility.water_bodies import WaterBodyChecker
from awd_validation.checks.eligibility.infrastructure import InfrastructureChecker
from awd_validation.checks.eligibility.net_area import NetAreaCalculator
from awd_validation.pipeline.phase2_final_status import Phase2FinalStatusAssigner


def run_phase(name: str, func, logger) -> tuple:
    logger.info("")
    logger.info(f">>> STARTING {name}")
    start = time.time()
    result = func()
    elapsed = round(time.time() - start, 1)
    logger.info(f">>> {name} COMPLETE in {elapsed}s")
    return result


def main() -> int:
    run_id = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    logger = setup_logger(run_id=run_id)
    config = Config("/app/config/settings.yaml")

    logger.info("=" * 60)
    logger.info("AWD PLOT VALIDATION PIPELINE — PHASE 2 RUN")
    logger.info(f"Run ID:   {run_id}")
    logger.info(f"Project:  {config.project['name']}")
    logger.info(f"Standard: {config.project['standard']}")
    logger.info("=" * 60)

    # Check Phase 2 enabled status before running
    phase2_cfg = config.get("phase2", default={})
    forest_enabled = phase2_cfg.get("forest", {}).get("enabled", False)
    water_enabled = phase2_cfg.get("water_bodies", {}).get("enabled", False)
    infra_enabled = phase2_cfg.get("infrastructure", {}).get("enabled", False)

    logger.info("PHASE 2 CHECK STATUS:")
    logger.info(f"  2A Forest:         {'ENABLED' if forest_enabled else 'BLOCKED — awaiting data'}")
    logger.info(f"  2B Water bodies:   {'ENABLED' if water_enabled else 'BLOCKED — awaiting data'}")
    logger.info(f"  2C Infrastructure: {'ENABLED' if infra_enabled else 'BLOCKED — awaiting data'}")
    logger.info(f"  2D Net area:       ALWAYS RUN")
    logger.info(f"  2E Final status:   ALWAYS RUN")
    logger.info("")

    pipeline_start = time.time()
    results = {}

    # Phase 2A
    gdf_2a, report_2a = run_phase(
        "PHASE 2A — FOREST OVERLAP",
        lambda: ForestOverlapChecker(config).run(run_id),
        logger,
    )
    results["2A"] = report_2a.get("checks", {}).get("forest_overlap", {})

    # Phase 2B
    gdf_2b, report_2b = run_phase(
        "PHASE 2B — WATER BODY OVERLAP",
        lambda: WaterBodyChecker(config).run(run_id),
        logger,
    )
    results["2B"] = report_2b.get("checks", {}).get("water_overlap", {})

    # Phase 2C
    gdf_2c, report_2c = run_phase(
        "PHASE 2C — INFRASTRUCTURE OVERLAP",
        lambda: InfrastructureChecker(config).run(run_id),
        logger,
    )
    results["2C"] = report_2c.get("checks", {}).get("infrastructure_overlap", {})

    # Phase 2D
    gdf_2d, report_2d = run_phase(
        "PHASE 2D — NET ELIGIBLE AREA",
        lambda: NetAreaCalculator(config).run(run_id),
        logger,
    )
    results["2D"] = report_2d.get("phase_2d_status_summary", {})

    # Phase 2E
    gdf_2e, report_2e = run_phase(
        "PHASE 2E — FINAL STATUS ASSIGNMENT",
        lambda: Phase2FinalStatusAssigner(config).run(run_id),
        logger,
    )
    results["2E"] = report_2e.get("phase2_status_summary", {})

    total_elapsed = round(time.time() - pipeline_start, 1)
    total_plots = len(gdf_2e)

    logger.info("")
    logger.info("=" * 60)
    logger.info("PHASE 2 PIPELINE COMPLETE — FULL SUMMARY")
    logger.info("=" * 60)
    logger.info(f"Run ID:              {run_id}")
    logger.info(f"Total runtime:       {total_elapsed}s ({total_elapsed/60:.1f} min)")
    logger.info(f"Total plots:         {total_plots:,}")
    logger.info("-" * 60)
    logger.info("PHASE RESULTS:")
    logger.info(f"  2A Forest:         {results['2A'].get('status', 'N/A')}")
    logger.info(f"  2B Water bodies:   {results['2B'].get('status', 'N/A')}")
    logger.info(f"  2C Infrastructure: {results['2C'].get('status', 'N/A')}")
    logger.info("-" * 60)
    logger.info("FINAL PHASE 2 ELIGIBILITY:")
    for status, data in sorted(results["2E"].items()):
        logger.info(
            f"  {status:<25} {data.get('count', 0):>8,} plots "
            f"({data.get('pct', 0):.2f}%)"
        )
    logger.info("-" * 60)
    carbon = report_2e.get("carbon_summary", {})
    logger.info("CARBON ACCOUNTING:")
    logger.info(
        f"  Total eligible area: "
        f"{carbon.get('total_eligible_area_ha', 0):>10,.2f} ha"
    )
    logger.info(
        f"  Avg adjustment:      "
        f"{carbon.get('avg_adjustment_factor', 1.0):>10.4f}"
    )
    logger.info(
        f"  Plots for review:    "
        f"{carbon.get('plots_needing_review', 0):>10,}"
    )
    logger.info("-" * 60)
    logger.info("OUTPUT FILES:")
    logger.info("  data/outputs/2E_eligible_plots_*.gpkg")
    logger.info("  data/outputs/2E_partial_plots_*.gpkg")
    logger.info("  data/outputs/2E_ineligible_plots_*.gpkg")
    logger.info("  data/outputs/2E_review_plots_*.gpkg")
    logger.info("  data/outputs/2E_master_eligibility_report_*.xlsx")
    logger.info("  data/outputs/2E_eligibility_map_*.html")
    logger.info("  data/outputs/2E_phase2_summary_*.json")
    logger.info("=" * 60)

    return 0


if __name__ == "__main__":
    sys.exit(main())
