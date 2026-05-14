"""
Phase 1C - Unique ID & Attribute Duplicate Detection

Checks performed:
  1. ID format validation  — matches expected pattern
  2. Special character check — invalid chars in ID
  3. Exact ID duplicates   — same ID on two different plots
  4. Attribute duplicates  — all non-geometry fields identical
"""
from pathlib import Path
from datetime import datetime
from typing import Tuple
import geopandas as gpd
import pandas as pd
import re
from loguru import logger
import json


class UniqueIDValidator:

    def __init__(self, config):
        self.config = config
        self.interim_dir = Path(config.input["interim_dir"])
        self.outputs_dir = Path(config.input["outputs_dir"])
        self.id_field = config.schema["unique_id_field"]
        self.primary_pattern = config.schema["id_pattern"]
        self.alternate_pattern = config.schema["alternate_id_pattern"]

    def run(self, run_id: str) -> Tuple[gpd.GeoDataFrame, dict]:
        logger.info("=" * 60)
        logger.info("PHASE 1C — UNIQUE ID & DUPLICATE DETECTION")
        logger.info("=" * 60)

        report = {
            "run_id": run_id,
            "phase": "1C",
            "timestamp": datetime.utcnow().isoformat(),
            "checks": {},
        }

        gdf = self._load_input(run_id, report)
        gdf = self._check_id_format(gdf, report)
        gdf = self._check_special_characters(gdf, report)
        gdf = self._check_exact_id_duplicates(gdf, report)
        gdf = self._check_attribute_duplicates(gdf, report)
        gdf = self._assign_phase_status(gdf, report)
        self._write_output(gdf, run_id)
        self._write_json_report(report, run_id)
        self._write_csv_report(gdf, run_id)
        self._log_summary(gdf, report)

        return gdf, report

    def _load_input(self, run_id: str, report: dict) -> gpd.GeoDataFrame:
        import glob
        pattern = str(self.interim_dir / "1B_plots_geometry_checked_*.gpkg")
        files = sorted(glob.glob(pattern))
        if not files:
            raise FileNotFoundError(
                "No Phase 1B output found. Run Phase 1B first."
            )
        latest = files[-1]
        logger.info(f"Loading Phase 1B output: {Path(latest).name}")
        gdf = gpd.read_file(latest, layer="plots_geometry_checked")
        logger.info(f"Loaded {len(gdf):,} plots")
        report["input_file"] = latest
        report["total_plots"] = len(gdf)
        return gdf

    def _check_id_format(
        self, gdf: gpd.GeoDataFrame, report: dict
    ) -> gpd.GeoDataFrame:
        """
        Validate ID against two known patterns:
          Primary:   N-N-A-N  e.g. 63-03-BB-27064
          Alternate: FAMH...  e.g. FAMHDB2023887213
        """
        logger.info("Check 1/4 — ID format validation...")

        gdf = gdf.copy()
        primary_re   = re.compile(self.primary_pattern)
        alternate_re = re.compile(self.alternate_pattern)

        def validate_id(plot_id):
            if pd.isna(plot_id) or str(plot_id).strip() == "":
                return False, "Empty or null ID"
            s = str(plot_id).strip()
            if primary_re.match(s):
                return True, ""
            if alternate_re.match(s):
                return True, ""
            return False, f"ID '{s}' does not match any known pattern"

        results = gdf["plot_id"].apply(validate_id)
        gdf["chk_id_format"]        = results.apply(lambda r: r[0])
        gdf["chk_id_format_reason"] = results.apply(lambda r: r[1])

        fail_mask       = ~gdf["chk_id_format"]
        count           = int(fail_mask.sum())
        primary_count   = int(gdf["plot_id"].astype(str)
                              .str.match(self.primary_pattern).sum())
        alternate_count = int(gdf["plot_id"].astype(str)
                              .str.match(self.alternate_pattern).sum())

        for _, row in gdf.loc[fail_mask].iterrows():
            logger.warning(
                f"  BAD FORMAT — plot_id: {row['plot_id']} | "
                f"{row['chk_id_format_reason']}"
            )

        logger.info(f"  Primary format   (N-N-A-N): {primary_count:,} plots")
        logger.info(f"  Alternate format (FAMH..):  {alternate_count:,} plots")
        logger.info(f"  Unknown format:             {count:,} plots")

        report["checks"]["id_format"] = {
            "status": "PASS" if count == 0 else "FAIL",
            "primary_pattern_count": primary_count,
            "alternate_pattern_count": alternate_count,
            "unknown_format_count": count,
            "unknown_format_ids": list(gdf.loc[fail_mask, "plot_id"].values),
        }
        return gdf

    def _check_special_characters(
        self, gdf: gpd.GeoDataFrame, report: dict
    ) -> gpd.GeoDataFrame:
        """Detect IDs with special characters e.g. FAMH(P2024767205"""
        logger.info("Check 2/4 — Special character detection...")

        gdf = gdf.copy()
        special_re = re.compile(r'[^A-Za-z0-9\-]')

        def has_special(plot_id):
            s = str(plot_id).strip()
            found = special_re.findall(s)
            if found:
                return True, f"Special chars: {list(set(found))}"
            return False, ""

        results = gdf["plot_id"].apply(has_special)
        gdf["chk_special_chars"]        = results.apply(lambda r: r[0])
        gdf["chk_special_chars_reason"] = results.apply(lambda r: r[1])

        bad_mask = gdf["chk_special_chars"]
        count    = int(bad_mask.sum())

        for _, row in gdf.loc[bad_mask].iterrows():
            logger.warning(
                f"  SPECIAL CHAR — plot_id: {row['plot_id']} | "
                f"{row['chk_special_chars_reason']}"
            )

        logger.info(f"  IDs with special characters: {count}")
        report["checks"]["special_characters"] = {
            "status": "PASS" if count == 0 else "FAIL",
            "count": count,
            "affected_ids": list(gdf.loc[bad_mask, "plot_id"].values),
        }
        return gdf

    def _check_exact_id_duplicates(
        self, gdf: gpd.GeoDataFrame, report: dict
    ) -> gpd.GeoDataFrame:
        """Detect plots sharing the same ID."""
        logger.info("Check 3/4 — Exact ID duplicate detection...")

        gdf = gdf.copy()
        gdf["chk_id_duplicate"]       = False
        gdf["chk_id_duplicate_group"] = ""

        dup_mask    = gdf["plot_id"].duplicated(keep=False)
        count_plots = int(dup_mask.sum())
        dup_groups  = []

        if count_plots > 0:
            gdf.loc[dup_mask, "chk_id_duplicate"] = True
            dup_ids = gdf.loc[dup_mask, "plot_id"].value_counts()
            for dup_id, freq in dup_ids.items():
                gdf.loc[
                    gdf["plot_id"] == dup_id,
                    "chk_id_duplicate_group"
                ] = f"Duplicate — appears {freq} times"
                dup_groups.append({
                    "plot_id": dup_id,
                    "occurrences": int(freq),
                })
                logger.warning(
                    f"  DUPLICATE ID — '{dup_id}' appears {freq} times"
                )

        logger.info(f"  Total plots:      {len(gdf):,}")
        logger.info(f"  Unique IDs:       {gdf['plot_id'].nunique():,}")
        logger.info(f"  Duplicate plots:  {count_plots}")

        report["checks"]["id_duplicates"] = {
            "status": "PASS" if count_plots == 0 else "FAIL",
            "total_plots": len(gdf),
            "unique_ids": int(gdf["plot_id"].nunique()),
            "duplicate_plot_count": count_plots,
            "duplicate_groups": dup_groups,
        }
        return gdf

    def _check_attribute_duplicates(
        self, gdf: gpd.GeoDataFrame, report: dict
    ) -> gpd.GeoDataFrame:
        """Detect rows where all source fields are identical."""
        logger.info("Check 4/4 — Attribute duplicate detection...")

        gdf = gdf.copy()
        gdf["chk_attr_duplicate"] = False

        compare_cols = [c for c in ["Name", "Description", "plot_id"]
                        if c in gdf.columns]

        attr_dup_mask = gdf.duplicated(subset=compare_cols, keep=False)
        count         = int(attr_dup_mask.sum())

        if count > 0:
            gdf.loc[attr_dup_mask, "chk_attr_duplicate"] = True
            for _, row in gdf.loc[attr_dup_mask].head(10).iterrows():
                logger.warning(
                    f"  ATTR DUPLICATE — plot_id: {row['plot_id']}"
                )

        logger.info(f"  Attribute duplicate plots: {count}")
        report["checks"]["attribute_duplicates"] = {
            "status": "PASS" if count == 0 else "WARNING",
            "count": count,
            "compared_fields": compare_cols,
        }
        return gdf

    def _assign_phase_status(
        self, gdf: gpd.GeoDataFrame, report: dict
    ) -> gpd.GeoDataFrame:
        gdf = gdf.copy()

        critical = (
            ~gdf["chk_id_format"] |
            gdf["chk_special_chars"] |
            gdf["chk_id_duplicate"]
        )
        warning = gdf["chk_attr_duplicate"]

        gdf["phase_1c_status"] = "PASS"
        gdf.loc[warning,  "phase_1c_status"] = "WARNING"
        gdf.loc[critical, "phase_1c_status"] = "FAIL"

        status_counts = gdf["phase_1c_status"].value_counts().to_dict()
        report["phase_1c_status_summary"] = status_counts
        return gdf

    def _write_output(self, gdf: gpd.GeoDataFrame, run_id: str) -> None:
        self.interim_dir.mkdir(parents=True, exist_ok=True)
        output_path = self.interim_dir / f"1C_plots_id_checked_{run_id}.gpkg"
        logger.info(f"Writing output: {output_path.name}")
        gdf.to_file(output_path, driver="GPKG", layer="plots_id_checked")
        logger.info("Output written successfully")

    def _write_json_report(self, report: dict, run_id: str) -> None:
        self.outputs_dir.mkdir(parents=True, exist_ok=True)
        report_path = self.outputs_dir / f"1C_id_report_{run_id}.json"
        with open(report_path, "w") as f:
            json.dump(report, f, indent=2, default=str)
        logger.info(f"JSON report written: {report_path.name}")

    def _write_csv_report(self, gdf: gpd.GeoDataFrame, run_id: str) -> None:
        self.outputs_dir.mkdir(parents=True, exist_ok=True)
        report_path = self.outputs_dir / f"1C_id_report_{run_id}.csv"

        report_cols = [
            "plot_id",
            "phase_1c_status",
            "chk_id_format",
            "chk_id_format_reason",
            "chk_special_chars",
            "chk_special_chars_reason",
            "chk_id_duplicate",
            "chk_id_duplicate_group",
            "chk_attr_duplicate",
        ]
        available = [c for c in report_cols if c in gdf.columns]
        report_df = gdf[available].copy()

        sort_order = {"FAIL": 0, "WARNING": 1, "PASS": 2}
        report_df["_sort"] = report_df["phase_1c_status"].map(sort_order)
        report_df = report_df.sort_values("_sort").drop(columns=["_sort"])

        report_df.to_csv(report_path, index=False, encoding="utf-8")

        fail_count = int((report_df["phase_1c_status"] == "FAIL").sum())
        pass_count = int((report_df["phase_1c_status"] == "PASS").sum())
        logger.info(f"CSV report written: {report_path.name}")
        logger.info(
            f"  CSV rows — FAIL: {fail_count:,} | "
            f"PASS: {pass_count:,} | "
            f"Total: {len(report_df):,}"
        )

    def _log_summary(
        self, gdf: gpd.GeoDataFrame, report: dict
    ) -> None:
        checks        = report["checks"]
        status_summary = report.get("phase_1c_status_summary", {})

        logger.info("=" * 60)
        logger.info("PHASE 1C SUMMARY")
        logger.info("=" * 60)
        for check_name, result in checks.items():
            status = result.get("status", "UNKNOWN")
            logger.info(f"  {check_name:<35} {status}")
        logger.info("-" * 60)
        logger.info("  PLOT STATUS BREAKDOWN:")
        for status, count in sorted(status_summary.items()):
            logger.info(f"    {status:<15} {count:>8,} plots")
        logger.info("-" * 60)
        logger.info("  FAIL BREAKDOWN BY CAUSE:")
        bad_format  = int((~gdf["chk_id_format"]).sum())
        special     = int(gdf["chk_special_chars"].sum())
        dup_id      = int(gdf["chk_id_duplicate"].sum())
        attr_dup    = int(gdf["chk_attr_duplicate"].sum())
        logger.info(f"    Bad ID format:           {bad_format:>8,} plots")
        logger.info(f"    Special characters:      {special:>8,} plots")
        logger.info(f"    Duplicate IDs:           {dup_id:>8,} plots")
        logger.info(f"    Attribute duplicates:    {attr_dup:>8,} plots (WARNING)")
        logger.info("=" * 60)
