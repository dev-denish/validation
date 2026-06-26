from pathlib import Path
from datetime import datetime
from typing import Optional, Tuple
import re
import geopandas as gpd
import pandas as pd
import numpy as np
from shapely.geometry import Point, mapping
from shapely.validation import explain_validity
from loguru import logger
import json


class FinalStatusAssigner:

    ELIGIBLE           = "ELIGIBLE"
    INELIGIBLE         = "INELIGIBLE"
    PARTIALLY_ELIGIBLE = "PARTIALLY_ELIGIBLE"
    NEEDS_REVIEW       = "NEEDS_REVIEW"

    def __init__(self, config):
        self.config      = config
        self.interim_dir = Path(config.input["interim_dir"])
        self.outputs_dir = Path(config.input["outputs_dir"])

    def run(self, run_id: str) -> Tuple[gpd.GeoDataFrame, dict]:
        logger.info("=" * 60)
        logger.info("PHASE 1F - FINAL STATUS ASSIGNMENT")
        logger.info("=" * 60)
        report = {
            "run_id":    run_id,
            "phase":     "1F",
            "timestamp": datetime.utcnow().isoformat(),
        }
        gdf = self._load_input(run_id, report)
        gdf = self._assign_final_status(gdf, report)
        self._write_split_outputs(gdf, run_id, report)
        self._write_master_csv(gdf, run_id)
        self._write_plain_excel(gdf, run_id)
        self._write_geojson(gdf, run_id)
        self._write_html_map(gdf, run_id)
        self._write_json_report(report, run_id)
        self._log_summary(gdf, report)
        return gdf, report

    def _load_input(self, run_id: str, report: dict) -> gpd.GeoDataFrame:
        import glob
        pattern = str(self.interim_dir / "1E_plots_topology_checked_*.gpkg")
        files   = sorted(glob.glob(pattern))
        if not files:
            raise FileNotFoundError("No Phase 1E output found. Run Phase 1E first.")
        latest = files[-1]
        logger.info(f"Loading Phase 1E output: {Path(latest).name}")
        gdf = gpd.read_file(latest, layer="plots_topology_checked")
        logger.info(f"Loaded {len(gdf):,} plots")
        report["input_file"]  = latest
        report["total_plots"] = len(gdf)
        return gdf

    def _assign_final_status(self, gdf: gpd.GeoDataFrame, report: dict) -> gpd.GeoDataFrame:
        logger.info("Assigning final eligibility status to all plots...")
        gdf = gdf.copy()

        def get_status(row):
            # Phase 1 failures → NEEDS_REVIEW (INELIGIBLE is Phase 2 only)
            if row.get("phase_1b_status") in ("FAIL", "NEEDS_REVIEW"):
                return self.NEEDS_REVIEW, "Geometry invalid or null — field survey required"
            if row.get("phase_1c_status") in ("FAIL", "NEEDS_REVIEW"):
                return self.NEEDS_REVIEW, "Invalid or duplicate ID — office correction required"
            if row.get("phase_1d_status") in ("FAIL", "NEEDS_REVIEW"):
                return self.NEEDS_REVIEW, "Spatial duplicate — office verification required"
            if row.get("phase_1e_status") in ("FAIL", "NEEDS_REVIEW"):
                pct = row.get("chk_overlap_pct", 0) * 100
                n = int(row.get("chk_overlap_partner_count", 0) or 0)
                return self.NEEDS_REVIEW, f"Major or significant overlap ({pct:.1f}%) with {n} neighbour farm(s)"
            if row.get("phase_1e_status") == "WARNING":
                pct = row.get("chk_overlap_pct", 0) * 100
                return self.PARTIALLY_ELIGIBLE, f"Minor overlap ({pct:.1f}%) — review boundary"
            if row.get("phase_1b_status") == "WARNING" or row.get("phase_1c_status") == "WARNING":
                return self.NEEDS_REVIEW, "Minor issue flagged — human review required"
            return self.ELIGIBLE, "All checks passed"

        results = gdf.apply(get_status, axis=1)
        gdf["final_eligibility"]        = results.apply(lambda r: r[0])
        gdf["final_eligibility_reason"] = results.apply(lambda r: r[1])

        counts = gdf["final_eligibility"].value_counts().to_dict()
        total  = len(gdf)
        report["final_status_summary"] = {
            k: {"count": v, "pct": round(v / total * 100, 2)}
            for k, v in counts.items()
        }
        logger.info("Final status assigned:")
        for status, count in sorted(counts.items()):
            logger.info(f"  {status:<25} {count:>8,} plots ({count/total*100:.2f}%)")
        return gdf

    def _write_split_outputs(self, gdf: gpd.GeoDataFrame, run_id: str, report: dict) -> None:
        self.outputs_dir.mkdir(parents=True, exist_ok=True)
        gdf_out = gdf.to_crs(epsg=4326)
        status_map = {
            self.ELIGIBLE:           "valid_plots",
            self.INELIGIBLE:         "ineligible_plots",
            self.PARTIALLY_ELIGIBLE: "partial_plots",
            self.NEEDS_REVIEW:       "needs_review_plots",
        }
        output_paths = {}
        for status, filename in status_map.items():
            subset = gdf_out.loc[gdf_out["final_eligibility"] == status].copy()
            if len(subset) == 0:
                logger.info(f"  No plots with status {status} - skipping")
                continue
            path = self.outputs_dir / f"{filename}_{run_id}.gpkg"
            subset.to_file(path, driver="GPKG", layer=filename)
            output_paths[status] = str(path)
            logger.info(f"  Written: {path.name} ({len(subset):,} plots)")
        report["output_files"] = output_paths

    def _write_master_csv(self, gdf: gpd.GeoDataFrame, run_id: str) -> None:
        report_cols = [
            "plot_id", "area_ha",
            "final_eligibility", "final_eligibility_reason",
            "id_format",
            "phase_1b_status", "phase_1c_status",
            "phase_1d_status", "phase_1e_status",
            "chk_null_geom", "chk_is_valid", "chk_is_simple",
            "chk_multipolygon", "chk_multipolygon_reason",
            "chk_id_format", "chk_special_chars", "chk_id_duplicate",
            "chk_centroid_duplicate", "chk_centroid_distance_m",
            "chk_area_centroid_match",
            "chk_overlap", "chk_overlap_area_ha", "chk_overlap_area_sqm",
            "chk_overlap_total_area_ha", "chk_overlap_total_area_sqm",
            "chk_overlap_pct", "chk_overlap_severity",
            "chk_overlap_partner_count", "chk_overlap_partners",
            "source_file", "ingestion_timestamp",
        ]
        available = [c for c in report_cols if c in gdf.columns]
        report_df = gdf[available].copy()
        sort_order = {
            self.NEEDS_REVIEW: 0, self.PARTIALLY_ELIGIBLE: 1,
            self.ELIGIBLE: 2, self.INELIGIBLE: 3,
        }
        report_df["_sort"] = report_df["final_eligibility"].map(sort_order)
        report_df = report_df.sort_values("_sort").drop(columns=["_sort"])
        path = self.outputs_dir / f"1F_master_validation_report_{run_id}.csv"
        report_df.to_csv(path, index=False, encoding="utf-8")
        logger.info(f"Master CSV written: {path.name} ({len(report_df):,} rows)")

    # ── COORDINATE HELPERS (Correction 8 — Where column coordinates) ──────────

    @staticmethod
    def _compass_direction(centroid, point) -> str:
        """Return the compass side (north/south/east/west) of point from centroid.

        Coordinates are lon/lat (x=lon, y=lat). dy drives north/south, dx drives
        east/west — the larger component wins.
        """
        dx = point.x - centroid.x
        dy = point.y - centroid.y
        if dx == 0 and dy == 0:
            return "centre"
        if abs(dy) > abs(dx):
            return "north" if dy > 0 else "south"
        return "east" if dx > 0 else "west"

    @staticmethod
    def _fmt_lonlat(point) -> str:
        """Format a WGS84 point as '<lon>°E, <lat>°N', rounded to 4 dp (~11 m)."""
        return f"{point.x:.4f}°E, {point.y:.4f}°N"

    @staticmethod
    def _self_intersection_point(geom) -> Optional[Point]:
        """Extract the self-intersection coordinate from GEOS explain_validity.

        Works on a WGS84 geometry so the returned point is already lon/lat.
        Returns None for geometries GEOS reports as valid (e.g. self-touching
        but technically valid rings).
        """
        try:
            reason = explain_validity(geom)
            m = re.search(r"\[([^\]]+)\]", reason)
            if m:
                parts = m.group(1).replace(",", " ").split()
                if len(parts) >= 2:
                    return Point(float(parts[0]), float(parts[1]))
        except Exception:
            pass
        return None

    @staticmethod
    def _special_chars_in(plot_id) -> str:
        """Return the unique invalid characters (not letter/number/hyphen) in an ID."""
        seen = []
        for c in str(plot_id):
            if not (c.isalnum() or c == "-") and c not in seen:
                seen.append(c)
        return "".join(seen)

    # ── PLAIN-ENGLISH EXCEL (Correction 7) ────────────────────────────────────

    def _plain_problem(self, row: pd.Series) -> str:
        if row.get("chk_null_geom"):
            return "No farm boundary was recorded. The survey app did not save a location."
        if row.get("chk_multipolygon"):
            n = int(row.get("chk_multipolygon_part_count", 2) or 2)
            return f"This farm ID has {n} separate shapes recorded. One farm cannot be in {n} different places."
        if not row.get("chk_is_valid", True):
            return "Farm boundary crosses itself like a figure 8. The line goes over itself."
        if not row.get("chk_is_simple", True):
            return "Farm boundary touches itself at a point. The boundary folds back."
        if row.get("chk_special_chars"):
            ch = self._special_chars_in(row.get("plot_id", ""))
            return (
                f"Farm ID contains an invalid character ( {ch} ). "
                "IDs must use only letters, numbers and hyphens."
            )
        if not row.get("chk_id_format", True):
            reason = row.get("chk_id_format_reason", "")
            return f"The farm ID does not match the expected format. {reason}"
        if row.get("chk_id_duplicate"):
            group = row.get("chk_id_duplicate_group", "")
            return f"The same ID number is given to more than one farm. {group}"
        if row.get("chk_attr_duplicate"):
            return "All the details for this farm are identical to another farm record. Possible data entry copy."
        if row.get("chk_centroid_duplicate"):
            dist = row.get("chk_centroid_distance_m", 0)
            return f"The center of this farm is only {dist:.1f} metres away from another farm. Likely the same farm recorded twice."
        if row.get("chk_area_centroid_match"):
            return "This farm has the same area and location as another farm. Likely a duplicate entry."
        if row.get("chk_overlap"):
            n = int(row.get("chk_overlap_partner_count", 1) or 1)
            sqm = row.get("chk_overlap_total_area_sqm", 0)
            pct = row.get("chk_overlap_pct", 0) * 100
            return (
                f"Farm boundary goes inside {n} neighbour farm(s). "
                f"Total overlapping area is {sqm:.1f} square metres ({pct:.1f}%)."
            )
        return "No problems found"

    def _plain_where(self, row: pd.Series, geom_lookup: Optional[dict] = None) -> str:
        """Plain-English location of the problem, including WGS84 coordinates.

        ``row`` must carry a WGS84 geometry (the caller iterates a 4326 frame) so
        any coordinate reported here is directly usable in the field. ``geom_lookup``
        maps plot_id → WGS84 geometry, used to locate the centre of an overlap.
        """
        geom = row.geometry if "geometry" in row.index else None
        centroid = None
        if geom is not None and not (hasattr(geom, "is_empty") and geom.is_empty):
            try:
                centroid = geom.centroid
            except Exception:
                centroid = None

        if row.get("chk_null_geom"):
            return "No location available — farm has no boundary in the system"
        if row.get("chk_multipolygon"):
            centroids = row.get("chk_multipolygon_centroids", "")
            return f"Multiple farm shapes found at these locations: {centroids}" if centroids else "Multiple shapes recorded for this farm ID"
        if not row.get("chk_is_valid", True) or not row.get("chk_is_simple", True):
            pt = self._self_intersection_point(geom) if geom is not None else None
            if pt is not None and centroid is not None:
                d = self._compass_direction(centroid, pt)
                return (
                    f"At the point where the boundary line crosses itself, near {d} "
                    f"corner of the farm at {self._fmt_lonlat(pt)}"
                )
            if centroid is not None:
                return (
                    "At the point where the boundary line crosses itself, near the "
                    f"centre of the farm at {self._fmt_lonlat(centroid)}"
                )
            return "At the point on the farm boundary where the line crosses itself"
        if row.get("chk_special_chars") or row.get("chk_id_duplicate") or not row.get("chk_id_format", True):
            if centroid is not None:
                return f"In the farm ID field. Farm centroid location: {self._fmt_lonlat(centroid)}"
            return "In the farm ID field — the name or number assigned to this farm"
        if row.get("chk_centroid_duplicate"):
            pair = row.get("chk_centroid_duplicate_pair", "")
            base = f"At the farm center point — {pair}" if pair else "At the farm center point"
            if centroid is not None:
                return f"{base} (near {self._fmt_lonlat(centroid)})"
            return base
        if row.get("chk_overlap"):
            partners = row.get("chk_overlap_partners", "")
            first = partners.split(",")[0].strip() if partners else ""
            overlap_pt = None
            if geom is not None and geom_lookup and first in geom_lookup:
                try:
                    inter = geom.intersection(geom_lookup[first])
                    if not inter.is_empty:
                        overlap_pt = inter.centroid
                except Exception:
                    overlap_pt = None
            ref = overlap_pt or centroid
            if first and ref is not None and centroid is not None:
                d = self._compass_direction(centroid, ref)
                return (
                    f"On the {d} boundary edge where it meets farm {first}, "
                    f"overlap area centred near {self._fmt_lonlat(ref)}"
                )
            if first:
                return f"On the farm boundary edge where it meets farm {first}"
            return "On the farm boundary where it overlaps a neighbour farm"
        return "All boundaries clean"

    def _plain_why(self, row: pd.Series) -> str:
        if row.get("chk_null_geom"):
            return "Survey app closed or crashed before the farm boundary was saved."
        if row.get("chk_multipolygon"):
            return "Surveyor recorded two separate walks for the same farm ID without realising."
        if not row.get("chk_is_valid", True) or not row.get("chk_is_simple", True):
            return "Surveyor walked back across the farm while the phone was still recording the boundary."
        if not row.get("chk_id_format", True) or row.get("chk_special_chars"):
            return "ID was typed manually with a mistake or copied from a different format."
        if row.get("chk_id_duplicate"):
            return "Two survey teams used the same ID number without checking each other."
        if row.get("chk_attr_duplicate"):
            return "Same farm entry was submitted more than once."
        if row.get("chk_centroid_duplicate") or row.get("chk_area_centroid_match"):
            return "Same farm was surveyed twice and both records were submitted."
        if row.get("chk_overlap"):
            return "Farm boundary extends into a neighbour farm. Either the boundary is wrong or the two farmers share land."
        return "All checks passed"

    def _plain_what_to_do(self, row: pd.Series) -> str:
        if row.get("chk_null_geom"):
            return "Go back to the farm. Walk the full boundary again with the survey app. Make sure the boundary saves before leaving. Resubmit."
        if row.get("chk_multipolygon"):
            return "Go back to the farm. Check which recorded shape is correct. Delete the wrong one. Resubmit only the correct boundary."
        if not row.get("chk_is_valid", True) or not row.get("chk_is_simple", True):
            return "Go back to farm. Place sticks at all corners. Walk full boundary in one round without crossing. Resubmit."
        if not row.get("chk_id_format", True) or row.get("chk_special_chars"):
            return "Office to correct the farm ID to the proper format. Resubmit."
        if row.get("chk_id_duplicate"):
            return "Office to check records and give a new ID to one farm. Resubmit both."
        if row.get("chk_attr_duplicate"):
            return "Office to check records and remove the duplicate entry. Keep only one."
        if row.get("chk_centroid_duplicate") or row.get("chk_area_centroid_match"):
            return "Office to check if both records are the same farm. If yes — delete one. If different — resurvey and resubmit."
        if row.get("chk_overlap"):
            n = int(row.get("chk_overlap_partner_count", 1) or 1)
            partners = row.get("chk_overlap_partners", "")
            partner_list = partners.split(",")[:3]
            partner_str = ", ".join(p.strip() for p in partner_list)
            return (
                f"Bring all {n + 1} farmers together ({partner_str}). "
                "Agree on the correct boundary. Re-walk each farm boundary from scratch. Resubmit all."
            )
        return "No action needed. Farm proceeds to carbon calculation."

    def _plain_who(self, row: pd.Series) -> str:
        if row.get("chk_null_geom") or not row.get("chk_is_valid", True) or not row.get("chk_is_simple", True) or row.get("chk_multipolygon"):
            return "Surveyor"
        if not row.get("chk_id_format", True) or row.get("chk_special_chars") or row.get("chk_id_duplicate") or row.get("chk_attr_duplicate") or row.get("chk_centroid_duplicate") or row.get("chk_area_centroid_match"):
            return "Office"
        if row.get("chk_overlap"):
            return "Surveyor and Office"
        return "None"

    def _write_plain_excel(self, gdf: gpd.GeoDataFrame, run_id: str) -> None:
        try:
            import openpyxl
            from openpyxl.styles import PatternFill, Font, Alignment

            # Work in WGS84 so the Where column can report lon/lat coordinates.
            gdf_wgs = gdf.to_crs(epsg=4326)
            geom_lookup = dict(
                zip(gdf_wgs["plot_id"].astype(str), gdf_wgs.geometry)
            )

            rows = []
            for _, row in gdf_wgs.iterrows():
                status = row.get("final_eligibility", "PENDING")
                id_fmt = row.get("id_format", "UNKNOWN")
                if pd.isna(id_fmt):
                    id_fmt = "UNKNOWN"
                rows.append({
                    "Plot ID":     row.get("plot_id", ""),
                    "Status":      "PASS" if status == self.ELIGIBLE else "NEEDS_REVIEW",
                    "ID Format":   str(id_fmt),
                    "Problem":     self._plain_problem(row),
                    "Where":       self._plain_where(row, geom_lookup),
                    "Why":         self._plain_why(row),
                    "What to do":  self._plain_what_to_do(row),
                    "Who does it": self._plain_who(row),
                })

            df = pd.DataFrame(rows, columns=[
                "Plot ID", "Status", "ID Format", "Problem",
                "Where", "Why", "What to do", "Who does it",
            ])

            # Sort: NEEDS_REVIEW first
            df["_sort"] = df["Status"].map({"NEEDS_REVIEW": 0, "PASS": 1})
            df = df.sort_values("_sort").drop(columns=["_sort"])

            # Write to outputs dir (mapped in Docker: /app/data/outputs)
            out_path = self.outputs_dir / "phase1_results.xlsx"
            out_path.parent.mkdir(parents=True, exist_ok=True)

            color_map = {
                "PASS":         "C6EFCE",
                "NEEDS_REVIEW": "FFEB9C",
            }

            with pd.ExcelWriter(str(out_path), engine="openpyxl") as writer:
                df.to_excel(writer, index=False, sheet_name="Phase 1 Results")
                ws = writer.sheets["Phase 1 Results"]

                # Header styling
                for col_idx in range(1, len(df.columns) + 1):
                    cell = ws.cell(row=1, column=col_idx)
                    cell.font = Font(bold=True)
                    cell.fill = PatternFill(
                        start_color="1F4E79", end_color="1F4E79", fill_type="solid"
                    )
                    cell.font = Font(bold=True, color="FFFFFF")
                    cell.alignment = Alignment(wrap_text=True)

                status_col_idx = list(df.columns).index("Status") + 1
                for row_idx in range(2, len(df) + 2):
                    status_val = ws.cell(row=row_idx, column=status_col_idx).value
                    fill_color = color_map.get(str(status_val), "FFFFFF")
                    fill = PatternFill(
                        start_color=fill_color, end_color=fill_color, fill_type="solid"
                    )
                    for col_idx in range(1, len(df.columns) + 1):
                        cell = ws.cell(row=row_idx, column=col_idx)
                        cell.fill = fill
                        cell.alignment = Alignment(wrap_text=True, vertical="top")

                col_widths = {
                    "Plot ID": 22, "Status": 14, "ID Format": 12,
                    "Problem": 55, "Where": 45, "Why": 45,
                    "What to do": 60, "Who does it": 18,
                }
                for col_idx, col_name in enumerate(df.columns, 1):
                    ws.column_dimensions[
                        ws.cell(row=1, column=col_idx).column_letter
                    ].width = col_widths.get(col_name, 20)

            pass_count = int((df["Status"] == "PASS").sum())
            nr_count   = int((df["Status"] == "NEEDS_REVIEW").sum())
            logger.info(f"Plain-English Excel written: {out_path.name}")
            logger.info(f"  PASS: {pass_count:,} | NEEDS_REVIEW: {nr_count:,} | Total: {len(df):,}")

        except Exception as e:
            logger.warning(f"Plain-English Excel failed: {e}")

    # ── GEOJSON FOR DASHBOARD MAP (Corrections 9, 11) ─────────────────────────

    def _write_geojson(self, gdf: gpd.GeoDataFrame, run_id: str) -> None:
        try:
            logger.info("Generating GeoJSON for dashboard map...")
            gdf_wgs = gdf.to_crs(epsg=4326)

            features = []
            null_geom_count = 0

            for _, row in gdf_wgs.iterrows():
                geom = row.geometry
                props = {
                    "plot_id":    str(row.get("plot_id", "")),
                    "name":       str(row.get("Name", row.get("name", ""))),
                    "status":     str(row.get("final_eligibility", "PENDING")),
                    "area_ha":    round(float(row.get("area_ha", 0) or 0), 4),
                    "problem":    str(row.get("final_eligibility_reason", "")),
                    "id_format":  str(row.get("id_format", "UNKNOWN")),
                    "what_to_do": self._plain_what_to_do(row),
                    "is_point_fallback": False,
                }

                if geom is None or (hasattr(geom, "is_empty") and geom.is_empty):
                    null_geom_count += 1
                    props["is_point_fallback"] = True
                    feature = {"type": "Feature", "geometry": None, "properties": props}
                else:
                    try:
                        if not geom.is_valid:
                            centroid = geom.centroid
                            props["is_point_fallback"] = True
                            feature = {
                                "type": "Feature",
                                "geometry": {"type": "Point", "coordinates": [centroid.x, centroid.y]},
                                "properties": props,
                            }
                        else:
                            feature = {
                                "type": "Feature",
                                "geometry": mapping(geom),
                                "properties": props,
                            }
                    except Exception:
                        try:
                            centroid = geom.centroid
                            props["is_point_fallback"] = True
                            feature = {
                                "type": "Feature",
                                "geometry": {"type": "Point", "coordinates": [centroid.x, centroid.y]},
                                "properties": props,
                            }
                        except Exception:
                            props["is_point_fallback"] = True
                            feature = {"type": "Feature", "geometry": None, "properties": props}

                features.append(feature)

            geojson = {
                "type": "FeatureCollection",
                "total": len(features),
                "run_id": run_id,
                "generated_at": datetime.utcnow().isoformat(),
                "features": features,
            }

            def _clean(obj):
                """Recursively replace NaN/Inf floats with None for JSON safety."""
                if isinstance(obj, float):
                    return None if (obj != obj or obj == float("inf") or obj == float("-inf")) else obj
                if isinstance(obj, dict):
                    return {k: _clean(v) for k, v in obj.items()}
                if isinstance(obj, list):
                    return [_clean(v) for v in obj]
                return obj

            path = self.outputs_dir / f"1F_plots_{run_id}.geojson"
            with open(path, "w") as f:
                json.dump(_clean(geojson), f, allow_nan=False, default=str)

            logger.info(
                f"GeoJSON written: {path.name} "
                f"({len(features):,} plots, {null_geom_count:,} null geometries as None)"
            )

        except Exception as e:
            logger.warning(f"GeoJSON export failed: {e}")

    def _write_html_map(self, gdf: gpd.GeoDataFrame, run_id: str) -> None:
        try:
            import folium
            logger.info("Generating interactive HTML map (all plots)...")
            gdf_wgs = gdf.to_crs(epsg=4326)
            bounds  = gdf_wgs.total_bounds
            center  = [(bounds[1] + bounds[3]) / 2, (bounds[0] + bounds[2]) / 2]
            m       = folium.Map(location=center, zoom_start=11, tiles="OpenStreetMap",
                                 max_zoom=19)
            colors  = {
                self.ELIGIBLE:           "#2ecc71",
                self.INELIGIBLE:         "#e74c3c",
                self.PARTIALLY_ELIGIBLE: "#f39c12",
                self.NEEDS_REVIEW:       "#e67e22",
            }
            for _, row in gdf_wgs.iterrows():
                geom    = row.geometry
                color   = colors.get(row["final_eligibility"], "#95a5a6")
                tooltip = (
                    "ID: " + str(row["plot_id"]) + "<br>" +
                    "Status: " + str(row["final_eligibility"]) + "<br>" +
                    "Area: " + str(round(row["area_ha"], 4)) + " ha<br>" +
                    "Reason: " + str(row.get("final_eligibility_reason", ""))
                )
                if geom is None:
                    continue
                try:
                    folium.GeoJson(
                        geom.__geo_interface__,
                        style_function=lambda x, c=color: {
                            "fillColor": c, "color": c,
                            "weight": 1, "fillOpacity": 0.6,
                        },
                        tooltip=folium.Tooltip(tooltip),
                    ).add_to(m)
                except Exception:
                    try:
                        centroid = geom.centroid
                        folium.CircleMarker(
                            location=[centroid.y, centroid.x],
                            radius=5, color=color, fill=True,
                            fill_color=color, tooltip=tooltip,
                        ).add_to(m)
                    except Exception:
                        pass
            path = self.outputs_dir / f"1F_validation_map_{run_id}.html"
            m.save(str(path))
            logger.info(f"HTML map written: {path.name} ({len(gdf_wgs):,} plots)")
        except Exception as e:
            logger.warning(f"HTML map failed: {e}")

    def _write_json_report(self, report: dict, run_id: str) -> None:
        self.outputs_dir.mkdir(parents=True, exist_ok=True)
        path = self.outputs_dir / f"1F_run_summary_{run_id}.json"
        with open(path, "w") as f:
            json.dump(report, f, indent=2, default=str)
        logger.info(f"Run summary written: {path.name}")

    def _log_summary(self, gdf: gpd.GeoDataFrame, report: dict) -> None:
        summary = report.get("final_status_summary", {})
        total   = report.get("total_plots", len(gdf))
        logger.info("=" * 60)
        logger.info("PHASE 1F - FINAL SUMMARY")
        logger.info("=" * 60)
        logger.info(f"  Total plots processed:    {total:,}")
        logger.info("-" * 60)
        for status in [self.ELIGIBLE, self.PARTIALLY_ELIGIBLE, self.NEEDS_REVIEW, self.INELIGIBLE]:
            if status in summary:
                count = summary[status]["count"]
                pct   = summary[status]["pct"]
                logger.info(f"  {status:<25} {count:>8,} plots ({pct:.2f}%)")
        logger.info("-" * 60)
        logger.info("  OUTPUT FILES:")
        logger.info("    valid_plots.gpkg              ELIGIBLE plots")
        logger.info("    ineligible_plots.gpkg         INELIGIBLE plots")
        logger.info("    partial_plots.gpkg            PARTIALLY ELIGIBLE")
        logger.info("    needs_review_plots.gpkg       NEEDS REVIEW")
        logger.info("    phase1_results.xlsx           Plain-English report (7 columns)")
        logger.info("    1F_master_validation_report   CSV")
        logger.info("    1F_plots_*.geojson            Dashboard map data")
        logger.info("    1F_validation_map.html        Interactive map (all plots)")
        logger.info("    1F_run_summary.json           Audit trail")
        logger.info("=" * 60)
