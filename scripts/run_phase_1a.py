"""
Phase 1A Runner — Ingestion & Schema Validation
Usage: python scripts/run_phase_1a.py
"""
import sys
from pathlib import Path
from datetime import datetime

# Add src to path
sys.path.insert(0, "/app/src")

from awd_validation.utils.logger import setup_logger
from awd_validation.utils.config import Config
from awd_validation.ingestion.loader import KMLLoader


def main():
    # Generate unique run ID
    run_id = datetime.utcnow().strftime("%Y%m%d_%H%M%S")

    # Setup logger
    logger = setup_logger(run_id=run_id)
    logger.info(f"Starting AWD Validation Pipeline — Run ID: {run_id}")

    # Load config
    config = Config("/app/config/settings.yaml")
    logger.info(
        f"Project: {config.project['name']} v{config.project['version']}"
    )

    # Run Phase 1A
    loader = KMLLoader(config)
    gdf, report = loader.run(run_id=run_id)

    # Final output
    logger.info(f"Phase 1A complete.")
    logger.info(f"Total plots processed: {len(gdf):,}")
    logger.info(
        f"Output: /app/data/interim/1A_plots_standardized_{run_id}.gpkg"
    )
    logger.info(
        f"Report: /app/data/outputs/1A_ingestion_report_{run_id}.json"
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())
