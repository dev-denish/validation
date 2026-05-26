"""Phase 2B — Water body overlap check. Usage: python scripts/run_phase_2b.py"""
import sys, time
from datetime import datetime
sys.path.insert(0, "/app/src")
from awd_validation.utils.logger import setup_logger
from awd_validation.utils.config import Config
from awd_validation.checks.eligibility.water_bodies import WaterBodyChecker

def main():
    run_id = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    logger = setup_logger(run_id=run_id)
    config = Config("/app/config/settings.yaml")
    logger.info(f"Phase 2B run: {run_id}")
    start = time.time()
    gdf, report = WaterBodyChecker(config).run(run_id)
    logger.info(f"Phase 2B complete in {round(time.time()-start, 1)}s")
    return 0

if __name__ == "__main__":
    sys.exit(main())
