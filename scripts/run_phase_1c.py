"""
Phase 1C Runner — Unique ID & Duplicate Detection
Usage: python scripts/run_phase_1c.py
"""
import sys
from datetime import datetime

sys.path.insert(0, "/app/src")

from awd_validation.utils.logger import setup_logger
from awd_validation.utils.config import Config
from awd_validation.checks.attribute.unique_id import UniqueIDValidator


def main():
    run_id = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    logger = setup_logger(run_id=run_id)
    logger.info(f"Starting Phase 1C — Run ID: {run_id}")

    config = Config("/app/config/settings.yaml")
    validator = UniqueIDValidator(config)
    gdf, report = validator.run(run_id=run_id)

    summary = report.get("phase_1c_status_summary", {})
    logger.info("Phase 1C complete.")
    logger.info(f"  PASS:          {summary.get('PASS', 0):,}")
    logger.info(f"  WARNING:       {summary.get('WARNING', 0):,}")
    logger.info(f"  NEEDS_REVIEW:  {summary.get('NEEDS_REVIEW', 0):,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
