"""Phase 2E — Final Phase 2 status assignment. Usage: python scripts/run_phase_2e.py"""
import sys, time
from datetime import datetime
sys.path.insert(0, "/app/src")
from awd_validation.utils.logger import setup_logger
from awd_validation.utils.config import Config
from awd_validation.pipeline.phase2_final_status import Phase2FinalStatusAssigner

def main():
    run_id = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    logger = setup_logger(run_id=run_id)
    config = Config("/app/config/settings.yaml")
    logger.info(f"Phase 2E run: {run_id}")
    start = time.time()
    gdf, report = Phase2FinalStatusAssigner(config).run(run_id)
    logger.info(f"Phase 2E complete in {round(time.time()-start, 1)}s")
    return 0

if __name__ == "__main__":
    sys.exit(main())
