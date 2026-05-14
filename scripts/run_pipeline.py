"""
AWD Validation Pipeline — Full Run (Phase 1A through 1F)
Usage: python scripts/run_pipeline.py
"""
import sys
import time
from datetime import datetime

sys.path.insert(0, "/app/src")

from awd_validation.utils.logger import setup_logger
from awd_validation.utils.config import Config
from awd_validation.ingestion.loader import KMLLoader
from awd_validation.checks.geometry.validity import GeometryValidator
from awd_validation.checks.attribute.unique_id import UniqueIDValidator
from awd_validation.checks.topology.spatial_duplicates import SpatialDuplicateDetector
from awd_validation.checks.topology.overlap import OverlapDetector
from awd_validation.pipeline.final_status import FinalStatusAssigner


def run_phase(name, func, logger):
    logger.info("")
    logger.info(f">>> STARTING {name}")
    start = time.time()
    result = func()
    elapsed = round(time.time() - start, 1)
    logger.info(f">>> {name} COMPLETE in {elapsed}s")
    return result


def main():
    run_id = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    logger = setup_logger(run_id=run_id)
    config = Config("/app/config/settings.yaml")

    logger.info("=" * 60)
    logger.info("AWD PLOT VALIDATION PIPELINE — FULL RUN")
    logger.info(f"Run ID: {run_id}")
    logger.info(f"Project: {config.project['name']}")
    logger.info(f"Standard: {config.project['standard']}")
    logger.info("=" * 60)

    pipeline_start = time.time()
    results = {}

    # Phase 1A
    gdf_1a, report_1a = run_phase(
        "PHASE 1A — INGESTION",
        lambda: KMLLoader(config).run(run_id),
        logger
    )
    results["1A"] = {
        "total_loaded": len(gdf_1a),
        "status": "COMPLETE"
    }

    # Phase 1B
    gdf_1b, report_1b = run_phase(
        "PHASE 1B — GEOMETRY VALIDITY",
        lambda: GeometryValidator(config).run(run_id),
        logger
    )
    s1b = report_1b.get("phase_1b_status_summary", {})
    results["1B"] = s1b

    # Phase 1C
    gdf_1c, report_1c = run_phase(
        "PHASE 1C — ID & DUPLICATE DETECTION",
        lambda: UniqueIDValidator(config).run(run_id),
        logger
    )
    s1c = report_1c.get("phase_1c_status_summary", {})
    results["1C"] = s1c

    # Phase 1D
    gdf_1d, report_1d = run_phase(
        "PHASE 1D — SPATIAL DUPLICATE DETECTION",
        lambda: SpatialDuplicateDetector(config).run(run_id),
        logger
    )
    s1d = report_1d.get("phase_1d_status_summary", {})
    results["1D"] = s1d

    # Phase 1E
    gdf_1e, report_1e = run_phase(
        "PHASE 1E — OVERLAP DETECTION",
        lambda: OverlapDetector(config).run(run_id),
        logger
    )
    s1e = report_1e.get("phase_1e_status_summary", {})
    results["1E"] = s1e

    # Phase 1F
    gdf_1f, report_1f = run_phase(
        "PHASE 1F — FINAL STATUS ASSIGNMENT",
        lambda: FinalStatusAssigner(config).run(run_id),
        logger
    )
    s1f = report_1f.get("final_status_summary", {})
    results["1F"] = s1f

    total_elapsed = round(time.time() - pipeline_start, 1)

    # Final pipeline summary
    logger.info("")
    logger.info("=" * 60)
    logger.info("PIPELINE COMPLETE — FULL SUMMARY")
    logger.info("=" * 60)
    logger.info(f"Run ID:              {run_id}")
    logger.info(f"Total runtime:       {total_elapsed}s ({total_elapsed/60:.1f} min)")
    logger.info(f"Total plots:         {len(gdf_1f):,}")
    logger.info("-" * 60)
    logger.info("PHASE RESULTS:")
    logger.info(f"  1A Ingestion       {results['1A']['total_loaded']:,} plots loaded")
    logger.info(f"  1B Geometry        PASS: {s1b.get('PASS',0):,}  FAIL: {s1b.get('FAIL',0):,}")
    logger.info(f"  1C ID & Dupes      PASS: {s1c.get('PASS',0):,}  FAIL: {s1c.get('FAIL',0):,}")
    logger.info(f"  1D Spatial Dupes   PASS: {s1d.get('PASS',0):,}  FAIL: {s1d.get('FAIL',0):,}")
    logger.info(f"  1E Overlaps        PASS: {s1e.get('PASS',0):,}  FAIL: {s1e.get('FAIL',0):,}  WARN: {s1e.get('WARNING',0):,}")
    logger.info("-" * 60)
    logger.info("FINAL ELIGIBILITY:")
    logger.info(f"  ELIGIBLE           {s1f.get('ELIGIBLE',{}).get('count',0):,} plots  ({s1f.get('ELIGIBLE',{}).get('pct',0):.2f}%)")
    logger.info(f"  PARTIALLY_ELIGIBLE {s1f.get('PARTIALLY_ELIGIBLE',{}).get('count',0):,} plots  ({s1f.get('PARTIALLY_ELIGIBLE',{}).get('pct',0):.2f}%)")
    logger.info(f"  NEEDS_REVIEW       {s1f.get('NEEDS_REVIEW',{}).get('count',0):,} plots  ({s1f.get('NEEDS_REVIEW',{}).get('pct',0):.2f}%)")
    logger.info(f"  INELIGIBLE         {s1f.get('INELIGIBLE',{}).get('count',0):,} plots  ({s1f.get('INELIGIBLE',{}).get('pct',0):.2f}%)")
    logger.info("-" * 60)
    logger.info("OUTPUT FILES:")
    logger.info("  data/outputs/valid_plots_*.gpkg")
    logger.info("  data/outputs/ineligible_plots_*.gpkg")
    logger.info("  data/outputs/partial_plots_*.gpkg")
    logger.info("  data/outputs/1F_master_validation_report_*.xlsx")
    logger.info("  data/outputs/1F_validation_map_*.html")
    logger.info("=" * 60)

    return 0


if __name__ == "__main__":
    sys.exit(main())
