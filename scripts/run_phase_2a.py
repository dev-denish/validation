"""Phase 2A — Forest overlap check. Usage: python scripts/run_phase_2a.py"""
import sys, time
from datetime import datetime
sys.path.insert(0, "/app/src")
from awd_validation.utils.logger import setup_logger
from awd_validation.utils.config import Config
from awd_validation.checks.eligibility.forest import ForestOverlapChecker

def main():
    run_id = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    logger = setup_logger(run_id=run_id)
    config = Config("/app/config/settings.yaml")
    logger.info(f"Phase 2A run: {run_id}")
    start = time.time()
    gdf, report = ForestOverlapChecker(config).run(run_id)
    logger.info(f"Phase 2A complete in {round(time.time()-start, 1)}s")
    return 0

if __name__ == "__main__":
    sys.exit(main())
