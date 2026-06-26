"""
Phase 1F Runner — Final Status Assignment & Master Report
Usage: python scripts/run_phase_1f.py
"""
import sys
from datetime import datetime

sys.path.insert(0, "/app/src")

from awd_validation.utils.logger import setup_logger
from awd_validation.utils.config import Config
from awd_validation.pipeline.final_status import FinalStatusAssigner


def main():
    run_id = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    logger = setup_logger(run_id=run_id)
    logger.info(f"Starting Phase 1F — Run ID: {run_id}")

    config   = Config("/app/config/settings.yaml")
    assigner = FinalStatusAssigner(config)
    gdf, report = assigner.run(run_id=run_id)

    summary = report.get("final_status_summary", {})
    logger.info("Phase 1F complete.")
    logger.info(f"  ELIGIBLE:            {summary.get('ELIGIBLE', {}).get('count', 0):,}")
    logger.info(f"  PARTIALLY_ELIGIBLE:  {summary.get('PARTIALLY_ELIGIBLE', {}).get('count', 0):,}")
    logger.info(f"  NEEDS_REVIEW:        {summary.get('NEEDS_REVIEW', {}).get('count', 0):,}")
    logger.info(f"  INELIGIBLE:          {summary.get('INELIGIBLE', {}).get('count', 0):,}")
    logger.info(f"  plain-english Excel: /app/data/outputs/phase1_results.xlsx")
    return 0


if __name__ == "__main__":
    sys.exit(main())
