"""Phase 2D — Net eligible area calculation. Usage: python scripts/run_phase_2d.py"""
import sys, time
from datetime import datetime
sys.path.insert(0, "/app/src")
from awd_validation.utils.logger import setup_logger
from awd_validation.utils.config import Config
from awd_validation.checks.eligibility.net_area import NetAreaCalculator

def main():
    run_id = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    logger = setup_logger(run_id=run_id)
    config = Config("/app/config/settings.yaml")
    logger.info(f"Phase 2D run: {run_id}")
    start = time.time()
    gdf, report = NetAreaCalculator(config).run(run_id)
    logger.info(f"Phase 2D complete in {round(time.time()-start, 1)}s")
    return 0

if __name__ == "__main__":
    sys.exit(main())
