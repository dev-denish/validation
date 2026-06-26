"""
Phase 1B Runner — Geometry Validity Checks
Usage: python scripts/run_phase_1b.py
"""
import sys
from datetime import datetime

sys.path.insert(0, "/app/src")

from awd_validation.utils.logger import setup_logger
from awd_validation.utils.config import Config
from awd_validation.checks.geometry.validity import GeometryValidator


def main():
    run_id = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    logger = setup_logger(run_id=run_id)
    logger.info(f"Starting Phase 1B — Run ID: {run_id}")

    config = Config("/app/config/settings.yaml")
    validator = GeometryValidator(config)
    gdf, report = validator.run(run_id=run_id)

    summary = report.get("phase_1b_status_summary", {})
    logger.info("Phase 1B complete.")
    logger.info(f"  PASS:          {summary.get('PASS', 0):,}")
    logger.info(f"  NEEDS_REVIEW:  {summary.get('NEEDS_REVIEW', 0):,}")
    logger.info(
        f"Output: /app/data/interim/1B_plots_geometry_checked_{run_id}.gpkg"
    )
    logger.info(
        f"Errors: /app/data/outputs/1B_error_geometries_{run_id}.gpkg"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
