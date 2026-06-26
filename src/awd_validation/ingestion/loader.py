"""
Phase 1A - Data Ingestion & Schema Validation
Reads KML file, validates schema, strips Z coordinates,
reprojects to working CRS, writes standardized GeoPackage.
"""
from pathlib import Path
from datetime import datetime
import geopandas as gpd
import fiona
import pandas as pd
from shapely.geometry import shape, mapping
import pyproj
from loguru import logger
from typing import Tuple
import json

# Enable KML drivers
fiona.drvsupport.supported_drivers["KML"] = "rw"
fiona.drvsupport.supported_drivers["LIBKML"] = "rw"


def get_utm_epsg(lon: float, lat: float) -> str:
    """Return EPSG code for the UTM zone containing the given lon/lat."""
    zone = int((lon + 180) / 6) + 1
    if lat >= 0:
        return f"EPSG:{32600 + zone}"
    else:
        return f"EPSG:{32700 + zone}"


class KMLLoader:
    """
    Handles ingestion of KML plot files for AWD validation.
    """

    REQUIRED_FIELDS = ["Name"]
    OUTPUT_EPSG = 4326

    def __init__(self, config):
        self.config = config
        self.raw_dir = Path(config.input["raw_dir"])
        self.interim_dir = Path(config.input["interim_dir"])
        self.filename = config.input["filename"]
        self.layer = config.input["layer"]
        self.unique_id_field = config.schema["unique_id_field"]
        self.input_epsg = config.crs["input_epsg"]
        self.working_epsg = config.crs["working_epsg"]

    def run(self, run_id: str) -> Tuple[gpd.GeoDataFrame, dict]:
        logger.info("=" * 60)
        logger.info("PHASE 1A — INGESTION & SCHEMA VALIDATION")
        logger.info("=" * 60)

        filepath = self.raw_dir / self.filename
        report = {
            "run_id": run_id,
            "phase": "1A",
            "input_file": str(filepath),
            "timestamp": datetime.utcnow().isoformat(),
            "checks": {},
        }

        gdf = self._read_kml_safe(filepath, report)
        self._validate_schema(gdf, report)
        gdf = self._strip_z_coordinates(gdf, report)
        self._validate_crs(gdf, report)
        gdf = self._reproject(gdf, report)
        gdf = self._add_metadata_columns(gdf, run_id)
        gdf = self._calculate_area(gdf, report)
        output_path = self._write_output(gdf, run_id)
        report["output_file"] = str(output_path)
        self._write_report(report, run_id)
        self._log_summary(report)

        return gdf, report

    def _read_kml_safe(self, filepath: Path, report: dict) -> gpd.GeoDataFrame:
        """
        Read KML using Fiona directly — handles degenerate geometries
        gracefully instead of crashing. Each feature is parsed individually
        so one bad geometry does not kill the entire load.
        """
        logger.info(f"Reading KML: {filepath.name}")

        if not filepath.exists():
            raise FileNotFoundError(f"Input file not found: {filepath}")

        records = []
        degenerate_count = 0
        null_geom_count = 0
        total_raw = 0
        degenerate_ids = []

        with fiona.open(filepath, layer=self.layer) as src:
            total_raw = len(src)
            logger.info(f"Total raw features in file: {total_raw:,}")

            for feature in src:
                props = dict(feature["properties"])
                raw_geom = feature["geometry"]
                plot_id = props.get("Name", "UNKNOWN")

                if raw_geom is None:
                    null_geom_count += 1
                    logger.warning(f"Null geometry — plot_id: {plot_id}")
                    records.append({
                        **props,
                        "geometry": None,
                        "_geom_load_status": "NULL_GEOMETRY",
                    })
                    continue

                try:
                    geom = shape(raw_geom)
                    records.append({
                        **props,
                        "geometry": geom,
                        "_geom_load_status": "OK",
                    })
                except (ValueError, Exception) as e:
                    degenerate_count += 1
                    degenerate_ids.append(plot_id)
                    logger.warning(
                        f"Degenerate geometry skipped — "
                        f"plot_id: {plot_id} | error: {str(e)}"
                    )
                    records.append({
                        **props,
                        "geometry": None,
                        "_geom_load_status": f"DEGENERATE: {str(e)[:80]}",
                    })

        if not records:
            raise ValueError("No features loaded from KML file")

        gdf = gpd.GeoDataFrame(records, geometry="geometry", crs=f"EPSG:{self.input_epsg}")

        loaded_ok = total_raw - degenerate_count - null_geom_count

        logger.info(f"Load summary:")
        logger.info(f"  Total in file:        {total_raw:,}")
        logger.info(f"  Loaded OK:            {loaded_ok:,}")
        logger.info(f"  Degenerate geometry:  {degenerate_count:,}")
        logger.info(f"  Null geometry:        {null_geom_count:,}")

        if degenerate_count > 0:
            logger.warning(
                f"{degenerate_count} degenerate geometries found — "
                f"flagged with _geom_load_status, NOT dropped"
            )
            logger.warning(f"Degenerate plot IDs: {degenerate_ids[:20]}")

        report["checks"]["file_read"] = {
            "status": "PASS" if degenerate_count == 0 else "WARNING",
            "total_features_in_file": total_raw,
            "loaded_ok": loaded_ok,
            "degenerate_geometry_count": degenerate_count,
            "null_geometry_count": null_geom_count,
            "degenerate_plot_ids": degenerate_ids[:50],
            "file_size_mb": round(filepath.stat().st_size / 1024 / 1024, 2),
        }

        return gdf

    def _validate_schema(self, gdf: gpd.GeoDataFrame, report: dict) -> None:
        logger.info("Validating schema...")

        issues = []
        field_summary = {}

        for field in self.REQUIRED_FIELDS:
            if field not in gdf.columns:
                issues.append(f"Required field missing: '{field}'")
                field_summary[field] = {"present": False}
            else:
                null_count = int(gdf[field].isnull().sum())
                empty_count = int(
                    (gdf[field].astype(str).str.strip() == "").sum()
                )
                field_summary[field] = {
                    "present": True,
                    "null_count": null_count,
                    "empty_count": empty_count,
                }
                if null_count > 0:
                    issues.append(f"Field '{field}' has {null_count} null values")
                if empty_count > 0:
                    issues.append(f"Field '{field}' has {empty_count} empty values")

        status = "FAIL" if issues else "PASS"
        report["checks"]["schema_validation"] = {
            "status": status,
            "required_fields": field_summary,
            "all_fields": [c for c in gdf.columns if c != "geometry"],
            "issues": issues,
        }

        if issues:
            for issue in issues:
                logger.error(f"Schema issue: {issue}")
            raise ValueError(f"Schema validation failed: {issues}")

        logger.info("Schema validation: PASS")

    def _strip_z_coordinates(
        self, gdf: gpd.GeoDataFrame, report: dict
    ) -> gpd.GeoDataFrame:
        logger.info("Stripping Z coordinates...")

        has_z_count = sum(
            1 for geom in gdf.geometry
            if geom is not None and geom.has_z
        )

        if has_z_count == 0:
            logger.info("No Z coordinates found — skipping")
            report["checks"]["strip_z"] = {
                "status": "PASS",
                "geometries_with_z": 0,
                "action": "skipped",
            }
            return gdf

        def strip_z(geometry):
            if geometry is None:
                return geometry
            if geometry.has_z:
                return shape({
                    "type": geometry.geom_type,
                    "coordinates": _remove_z(mapping(geometry)["coordinates"]),
                })
            return geometry

        gdf = gdf.copy()
        gdf["geometry"] = gdf["geometry"].apply(strip_z)

        remaining_z = sum(
            1 for geom in gdf.geometry
            if geom is not None and geom.has_z
        )

        logger.info(
            f"Stripped Z from {has_z_count:,} geometries "
            f"— {remaining_z} remaining"
        )

        report["checks"]["strip_z"] = {
            "status": "PASS",
            "geometries_with_z_before": int(has_z_count),
            "geometries_with_z_after": int(remaining_z),
            "action": "stripped",
        }

        return gdf

    def _validate_crs(self, gdf: gpd.GeoDataFrame, report: dict) -> None:
        logger.info("Validating CRS...")

        if gdf.crs is None:
            logger.warning("No CRS detected — assuming EPSG:4326")
            report["checks"]["crs_validation"] = {
                "status": "WARNING",
                "detected_crs": None,
                "expected_epsg": self.input_epsg,
                "action": "assumed_4326",
            }
            return

        detected_epsg = gdf.crs.to_epsg()
        status = "PASS" if detected_epsg == self.input_epsg else "WARNING"
        logger.info(f"CRS detected: EPSG:{detected_epsg}")

        report["checks"]["crs_validation"] = {
            "status": status,
            "detected_crs": str(gdf.crs),
            "detected_epsg": detected_epsg,
            "expected_epsg": self.input_epsg,
        }

    def _reproject(self, gdf: gpd.GeoDataFrame, report: dict) -> gpd.GeoDataFrame:
        if gdf.crs is None:
            gdf = gdf.set_crs(epsg=self.input_epsg)

        valid_geoms = gdf[gdf.geometry.notna()]
        if len(valid_geoms) > 0:
            all_centroids = valid_geoms.geometry.centroid
            mean_lon = float(all_centroids.x.mean())
            mean_lat = float(all_centroids.y.mean())
            target_epsg = get_utm_epsg(mean_lon, mean_lat)
        else:
            target_epsg = f"EPSG:{self.working_epsg}"

        logger.info(f"Reprojecting EPSG:{self.input_epsg} → {target_epsg} (dynamic UTM)...")
        gdf_projected = gdf.to_crs(target_epsg)
        epsg_num = int(target_epsg.split(":")[1])
        gdf_projected["working_crs_epsg"] = epsg_num

        report["checks"]["reprojection"] = {
            "status": "PASS",
            "from_epsg": self.input_epsg,
            "to_epsg": epsg_num,
            "crs_detected": target_epsg,
        }

        logger.info(f"Reprojection complete — working CRS: {target_epsg}")
        return gdf_projected

    def _add_metadata_columns(
        self, gdf: gpd.GeoDataFrame, run_id: str
    ) -> gpd.GeoDataFrame:
        gdf = gdf.copy()
        gdf["plot_id"] = gdf[self.unique_id_field].astype(str).str.strip()
        gdf["run_id"] = run_id
        gdf["ingestion_timestamp"] = datetime.utcnow().isoformat()
        gdf["source_file"] = self.filename
        gdf["phase_1a_status"] = gdf["_geom_load_status"].apply(
            lambda s: "FAIL" if s != "OK" else "PASS"
        )
        gdf["phase_1b_status"] = "PENDING"
        gdf["phase_1c_status"] = "PENDING"
        gdf["phase_1d_status"] = "PENDING"
        gdf["phase_1e_status"] = "PENDING"
        gdf["phase_1f_status"] = "PENDING"
        gdf["final_eligibility"] = "PENDING"
        logger.debug("Metadata columns added")
        return gdf

    def _calculate_area(
        self, gdf: gpd.GeoDataFrame, report: dict
    ) -> gpd.GeoDataFrame:
        logger.info("Calculating plot areas in hectares...")

        gdf = gdf.copy()
        gdf["area_ha"] = gdf.geometry.area / 10000

        valid_areas = gdf.loc[gdf.geometry.notna(), "area_ha"]

        area_stats = {
            "min_area_ha": round(float(valid_areas.min()), 4),
            "max_area_ha": round(float(valid_areas.max()), 4),
            "mean_area_ha": round(float(valid_areas.mean()), 4),
            "median_area_ha": round(float(valid_areas.median()), 4),
            "total_area_ha": round(float(valid_areas.sum()), 2),
            "zero_area_count": int((valid_areas == 0).sum()),
            "plots_with_null_geom": int(gdf.geometry.isna().sum()),
        }

        report["checks"]["area_calculation"] = {
            "status": "PASS",
            "stats": area_stats,
        }

        logger.info(
            f"Area stats — Min: {area_stats['min_area_ha']} ha | "
            f"Max: {area_stats['max_area_ha']} ha | "
            f"Mean: {area_stats['mean_area_ha']} ha | "
            f"Total: {area_stats['total_area_ha']:,} ha"
        )

        return gdf

    def _write_output(self, gdf: gpd.GeoDataFrame, run_id: str) -> Path:
        self.interim_dir.mkdir(parents=True, exist_ok=True)
        output_path = self.interim_dir / f"1A_plots_standardized_{run_id}.gpkg"
        logger.info(f"Writing output: {output_path.name}")
        gdf.to_file(output_path, driver="GPKG", layer="plots_standardized")
        logger.info(f"Output written successfully")
        return output_path

    def _write_report(self, report: dict, run_id: str) -> None:
        reports_dir = Path("/app/data/outputs")
        reports_dir.mkdir(parents=True, exist_ok=True)
        report_path = reports_dir / f"1A_ingestion_report_{run_id}.json"
        with open(report_path, "w") as f:
            json.dump(report, f, indent=2, default=str)
        logger.info(f"Report written: {report_path.name}")

    def _log_summary(self, report: dict) -> None:
        checks = report["checks"]
        logger.info("=" * 60)
        logger.info("PHASE 1A SUMMARY")
        logger.info("=" * 60)
        for check_name, result in checks.items():
            status = result.get("status", "UNKNOWN")
            logger.info(f"  {check_name:<35} {status}")
        logger.info("=" * 60)


def _remove_z(coordinates):
    """Recursively remove Z coordinate from nested coordinate lists."""
    if not coordinates:
        return coordinates
    if isinstance(coordinates[0], (int, float)):
        return coordinates[:2]
    return [_remove_z(c) for c in coordinates]
