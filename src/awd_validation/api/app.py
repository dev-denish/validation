import asyncio
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

import yaml
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

sys.path.insert(0, "/app/src")

app = FastAPI(title="AWD Plot Validation API", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

RUNS: Dict[str, dict] = {}
CURRENT_RUN: dict = {"run_id": None, "status": "idle", "started_at": None, "phase": None}

RAW_DIR  = Path("/app/data/raw")
OUTPUTS  = Path("/app/data/outputs")
LOG_DIR  = Path("/app/logs")
CONFIG   = Path("/app/config/settings.yaml")
STATIC   = Path("/app/src/awd_validation/api/static")

ANSI = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")

PHASE_SCRIPTS = {
    "phase1": "/app/scripts/run_pipeline.py",
    "phase2": "/app/scripts/run_pipeline_phase2.py",
    "full":   "/app/scripts/run_pipeline.py",
}

KEY_MAP = {
    "overlap_area_pct":         ["thresholds", "overlap_area_pct"],
    "centroid_distance_m":      ["thresholds", "centroid_distance_m"],
    "sliver_area_ha":           ["thresholds", "sliver_area_ha"],
    "cdm_min_area_ha":          ["phase2", "forest", "cdm_min_area_ha"],
    "cdm_canopy_pct":           ["phase2", "forest", "cdm_canopy_pct"],
    "min_area_ha":              ["phase2", "water_bodies", "min_area_ha"],
    "jrc_occurrence_threshold": ["phase2", "water_bodies", "jrc_occurrence_threshold"],
    "roads_buffer_m":           ["phase2", "infrastructure", "roads_buffer_m"],
    "railways_buffer_m":        ["phase2", "infrastructure", "railways_buffer_m"],
    "review_threshold_pct":     ["phase2", "eligibility", "review_threshold_pct"],
    "partial_threshold_pct":    ["phase2", "eligibility", "partial_threshold_pct"],
}


# ── HELPERS ───────────────────────────────────────────────────────────────────

def _classify_level(line: str) -> str:
    if "| SUCCESS" in line:
        return "success"
    if "| WARNING" in line:
        return "warning"
    if "| ERROR  " in line or "| ERROR\n" in line:
        return "error"
    return "info"


def _strip_loguru_prefix(line: str) -> str:
    return re.sub(
        r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+ \| \w+\s+\| [^|]+ \| ",
        "",
        line,
    )


def _nested_set(d: dict, keys: list, value: Any) -> None:
    for k in keys[:-1]:
        d = d.setdefault(k, {})
    d[keys[-1]] = value


def _nested_get(d: dict, keys: list, default=None) -> Any:
    for k in keys:
        if not isinstance(d, dict):
            return default
        d = d.get(k, default)
    return d


# ── PIPELINE THREAD ───────────────────────────────────────────────────────────

def _run_pipeline_thread(run_id: str, script: str, phase: str) -> None:
    LOG_DIR.mkdir(exist_ok=True)
    log_path = LOG_DIR / f"validation_{run_id}.log"

    CURRENT_RUN.update({"status": "running", "run_id": run_id, "phase": phase,
                         "started_at": datetime.utcnow().isoformat()})
    RUNS[run_id] = {"run_id": run_id, "status": "running", "phase": phase,
                    "logs": [], "outputs": [], "summary": {}}

    env = os.environ.copy()
    env["AWD_RUN_ID"] = run_id

    with open(log_path, "w") as lf:
        try:
            proc = subprocess.Popen(
                ["python", script],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=env,
            )
            for raw in proc.stdout:
                raw = raw.rstrip()
                lf.write(raw + "\n")
                lf.flush()
                clean = ANSI.sub("", raw)
                if clean:
                    RUNS[run_id]["logs"].append(clean)
            proc.wait()

            if proc.returncode == 0:
                RUNS[run_id]["status"] = "complete"
                CURRENT_RUN["status"] = "complete"
                _collect_outputs(run_id)
            else:
                RUNS[run_id]["status"] = "failed"
                CURRENT_RUN["status"] = "failed"
        except Exception as exc:
            RUNS[run_id]["status"] = "failed"
            CURRENT_RUN["status"] = "failed"
            RUNS[run_id]["logs"].append(f"ERROR: {exc}")


# ── LEGACY THREAD (kept for /upload endpoint) ─────────────────────────────────

def _run_pipeline(run_id: str, kml_path: str) -> None:
    RUNS[run_id]["status"] = "running"
    RUNS[run_id]["logs"].append(f"Pipeline started — {datetime.utcnow().isoformat()}")
    RUNS[run_id]["logs"].append(f"KML file: {Path(kml_path).name}")
    RUNS[run_id]["logs"].append("=" * 60)

    env = os.environ.copy()
    env["AWD_KML_PATH"] = kml_path
    env["AWD_RUN_ID"] = run_id

    try:
        proc = subprocess.Popen(
            ["python", "/app/scripts/run_pipeline.py"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, env=env,
        )
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                RUNS[run_id]["logs"].append(line)
        proc.wait()

        if proc.returncode == 0:
            RUNS[run_id]["status"] = "complete"
            RUNS[run_id]["logs"].append("=" * 60)
            RUNS[run_id]["logs"].append("PIPELINE COMPLETE")
            _collect_outputs(run_id)
        else:
            RUNS[run_id]["status"] = "failed"
            RUNS[run_id]["logs"].append(f"PIPELINE FAILED — exit code {proc.returncode}")
    except Exception as exc:
        RUNS[run_id]["status"] = "failed"
        RUNS[run_id]["logs"].append(f"ERROR: {exc}")


def _collect_outputs(run_id: str) -> None:
    file_map = {
        "1F_master_validation_report_*.xlsx": "Validation Report (Excel)",
        "1F_validation_map_*.html":           "Interactive Map (HTML)",
        "valid_plots_*.gpkg":                 "Valid Plots (GeoPackage)",
        "ineligible_plots_*.gpkg":            "Ineligible Plots (GeoPackage)",
        "partial_plots_*.gpkg":               "Partial Plots (GeoPackage)",
        "1F_master_validation_report_*.csv":  "Full Report (CSV)",
        "1F_run_summary_*.json":              "Run Summary (JSON)",
    }
    outputs = []
    for pattern, label in file_map.items():
        matches = sorted(glob.glob(str(OUTPUTS / pattern)))
        if matches:
            latest  = matches[-1]
            fname   = Path(latest).name
            size_mb = round(os.path.getsize(latest) / 1024 / 1024, 2)
            outputs.append({"label": label, "filename": fname, "size_mb": size_mb,
                            "url": f"/download/{fname}"})
            RUNS[run_id]["logs"].append(f"  OUTPUT: {label} ({size_mb} MB)")
    RUNS[run_id]["outputs"] = outputs

    summary: dict = {}
    for line in RUNS[run_id]["logs"]:
        try:
            if "ELIGIBLE" in line and "plots" in line and "PARTIALLY" not in line and "INELIGIBLE" not in line:
                parts = line.strip().split()
                for i, p in enumerate(parts):
                    if p == "ELIGIBLE" and i + 1 < len(parts):
                        summary["eligible"] = int(parts[i + 1].replace(",", ""))
            if "INELIGIBLE" in line and "plots" in line:
                parts = line.strip().split()
                for i, p in enumerate(parts):
                    if p == "INELIGIBLE" and i + 1 < len(parts):
                        summary["ineligible"] = int(parts[i + 1].replace(",", ""))
            if "PARTIALLY_ELIGIBLE" in line and "plots" in line:
                parts = line.strip().split()
                for i, p in enumerate(parts):
                    if p == "PARTIALLY_ELIGIBLE" and i + 1 < len(parts):
                        summary["partial"] = int(parts[i + 1].replace(",", ""))
            if "Total plots:" in line:
                summary["total"] = int(line.split("Total plots:")[-1].strip().replace(",", ""))
            if "Total runtime:" in line:
                summary["runtime"] = line.split("Total runtime:")[-1].strip()
        except Exception:
            pass
    RUNS[run_id]["summary"] = summary


# ── PLOTS API (for dashboard map) ────────────────────────────────────────────

@app.get("/api/plots")
async def get_plots():
    """
    Return all Phase 1 plots as GeoJSON for dashboard map rendering.
    Loads the latest 1F_plots_*.geojson file written by Phase 1F.
    Every plot is included — invalid geometries appear as null geometry.
    """
    files = sorted(OUTPUTS.glob("1F_plots_*.geojson"))
    if not files:
        raise HTTPException(
            status_code=404,
            detail="No Phase 1 plot data found. Run Phase 1 pipeline first.",
        )
    latest = files[-1]
    with open(latest) as f:
        data = json.load(f)
    return data


@app.get("/api/plots/count")
async def get_plots_count():
    """Return total plot count from latest Phase 1 run."""
    files = sorted(OUTPUTS.glob("1F_plots_*.geojson"))
    if not files:
        return {"total": 0, "source": None}
    latest = files[-1]
    with open(latest) as f:
        data = json.load(f)
    status_counts: dict = {}
    for feat in data.get("features", []):
        s = feat.get("properties", {}).get("status", "UNKNOWN")
        status_counts[s] = status_counts.get(s, 0) + 1
    return {
        "total": data.get("total", 0),
        "run_id": data.get("run_id"),
        "status_counts": status_counts,
        "source": latest.name,
    }


# ── STATIC FILE SERVING ───────────────────────────────────────────────────────

if STATIC.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


@app.get("/")
async def dashboard():
    dash = STATIC / "awd-dashboard.html"
    if dash.exists():
        return FileResponse(str(dash))
    return HTMLResponse("<h1>Dashboard not found</h1>", status_code=404)


# ── API: STATUS ───────────────────────────────────────────────────────────────

@app.get("/api/status")
async def api_status():
    return CURRENT_RUN


# ── API: RESULTS ──────────────────────────────────────────────────────────────

@app.get("/api/results/latest")
async def results_latest():
    files = sorted(OUTPUTS.glob("2E_phase2_summary_*.json"))
    if not files:
        raise HTTPException(status_code=404, detail="No Phase 2 results found")
    with open(files[-1]) as f:
        return json.load(f)


@app.get("/api/results/{run_id}")
async def results_by_run(run_id: str):
    path = OUTPUTS / f"2E_phase2_summary_{run_id}.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"No results for run {run_id}")
    with open(path) as f:
        return json.load(f)


# ── API: CONFIG THRESHOLDS ────────────────────────────────────────────────────

@app.get("/api/config/thresholds")
async def get_thresholds():
    with open(CONFIG) as f:
        cfg = yaml.safe_load(f)
    return {key: _nested_get(cfg, path) for key, path in KEY_MAP.items()}


@app.patch("/api/config/thresholds")
async def patch_thresholds(body: Dict[str, Any]):
    with open(CONFIG) as f:
        cfg = yaml.safe_load(f)

    updated = []
    for key, value in body.items():
        if key not in KEY_MAP:
            continue
        _nested_set(cfg, KEY_MAP[key], value)
        updated.append(key)

    if not updated:
        raise HTTPException(status_code=400, detail="No recognized threshold keys in body")

    with open(CONFIG, "w") as f:
        yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    return {"updated": updated, "rerun_required": True}


# ── API: PIPELINE RUN ─────────────────────────────────────────────────────────

@app.post("/api/run/{phase}")
async def run_pipeline(phase: str):
    if phase not in PHASE_SCRIPTS:
        raise HTTPException(status_code=400, detail=f"Unknown phase: {phase}. Use phase1, phase2, or full.")

    if CURRENT_RUN.get("status") == "running":
        raise HTTPException(status_code=409, detail="Pipeline already running")

    run_id = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    started_at = datetime.utcnow().isoformat()
    threading.Thread(
        target=_run_pipeline_thread,
        args=(run_id, PHASE_SCRIPTS[phase], phase),
        daemon=True,
    ).start()
    return {"run_id": run_id, "started_at": started_at, "phase": phase}


# ── API: SSE LOG STREAM ───────────────────────────────────────────────────────

@app.get("/api/logs/stream")
async def stream_logs_sse(request: Request):
    async def gen():
        run_id = CURRENT_RUN.get("run_id")
        if not run_id or run_id not in RUNS:
            payload = json.dumps({
                "level": "info",
                "msg": "No active run — connect after starting a pipeline",
                "ts": datetime.utcnow().isoformat(),
            })
            yield f"data: {payload}\n\n"
            return

        sent = 0
        while True:
            if await request.is_disconnected():
                break

            logs = RUNS[run_id]["logs"]
            while sent < len(logs):
                raw = logs[sent]
                sent += 1
                clean = ANSI.sub("", raw).strip()
                if not clean:
                    continue
                level = _classify_level(clean)
                msg = _strip_loguru_prefix(clean)
                payload = json.dumps({
                    "level": level,
                    "msg": msg,
                    "ts": datetime.utcnow().isoformat(),
                })
                yield f"data: {payload}\n\n"

            status = RUNS[run_id]["status"]
            if status in ("complete", "failed"):
                final = json.dumps({
                    "level": "success" if status == "complete" else "error",
                    "msg": f"Pipeline {status.upper()}",
                    "ts": datetime.utcnow().isoformat(),
                    "done": True,
                })
                yield f"data: {final}\n\n"
                break

            await asyncio.sleep(0.2)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                 "Access-Control-Allow-Origin": "*"},
    )


# ── API: REPORTS ──────────────────────────────────────────────────────────────

@app.get("/api/reports/{run_id}/excel")
async def report_excel(run_id: str):
    path = OUTPUTS / f"2E_master_eligibility_report_{run_id}.xlsx"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Excel report not found for run {run_id}")
    return FileResponse(str(path), filename=path.name,
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.get("/api/reports/{run_id}/map")
async def report_map(run_id: str):
    path = OUTPUTS / f"2E_eligibility_map_{run_id}.html"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Eligibility map not found for run {run_id}")
    return FileResponse(str(path), media_type="text/html")


# ── API: PHASE 1 REPORT DOWNLOAD ─────────────────────────────────────────────

@app.get("/api/download/phase1-report")
async def download_phase1_report():
    """Download the latest Phase 1 Excel report."""
    # Try explicit phase1_results.xlsx first, then pattern-match latest 1F report
    candidates = [OUTPUTS / "phase1_results.xlsx"]
    candidates += sorted(OUTPUTS.glob("1F_master_validation_report_*.xlsx"))
    for path in candidates:
        if path.exists():
            return FileResponse(
                str(path),
                filename=path.name,
                media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
    raise HTTPException(status_code=404, detail="No Phase 1 report found. Run Phase 1 pipeline first.")


# ── API: PHASE 1 SUMMARY (dashboard Phase 1 QC section) ──────────────────────

def _latest_phase1_excel() -> Optional[Path]:
    candidates = [OUTPUTS / "phase1_results.xlsx"]
    candidates += sorted(OUTPUTS.glob("1F_master_validation_report_*.xlsx"))
    for path in candidates:
        if path.exists():
            return path
    return None


def _truthy(v: Any) -> bool:
    return str(v).strip().lower() in ("true", "1", "1.0", "yes", "t")


@app.get("/api/phase1/summary")
async def phase1_summary():
    """
    Live Phase 1 QC breakdown read from the latest master validation CSV.

    The master CSV carries the structured boolean check columns (chk_*) and the
    final_eligibility status, so the figures are exact rather than parsed from
    prose. Powers the dashboard 'Phase 1 — Plot Creation QC' section.
    """
    import csv

    files = sorted(OUTPUTS.glob("1F_master_validation_report_*.csv"))
    if not files:
        raise HTTPException(status_code=404, detail="No Phase 1 master report found. Run Phase 1 first.")
    path = files[-1]

    # Each breakdown row: label → predicate over the boolean check columns.
    issue_defs = [
        ("Self-intersecting ring",       lambda r: not _truthy(r.get("chk_is_valid", "True")) or not _truthy(r.get("chk_is_simple", "True"))),
        ("Special character in ID",      lambda r: _truthy(r.get("chk_special_chars"))),
        ("Spatial overlap",              lambda r: _truthy(r.get("chk_overlap"))),
        ("MultiPolygon detected",        lambda r: _truthy(r.get("chk_multipolygon"))),
        ("Duplicate ID",                 lambda r: _truthy(r.get("chk_id_duplicate"))),
        ("Spatial duplicate (centroid)", lambda r: _truthy(r.get("chk_centroid_duplicate"))),
        ("Null geometry",                lambda r: _truthy(r.get("chk_null_geom"))),
    ]
    counts: Dict[str, int] = {label: 0 for label, _ in issue_defs}

    n_eligible = n_review = n_partial = n_ineligible = 0
    special_chars = 0
    total = 0

    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            total += 1
            status = (r.get("final_eligibility") or "").strip()
            if status == "ELIGIBLE":
                n_eligible += 1
            elif status == "PARTIALLY_ELIGIBLE":
                n_partial += 1
            elif status == "INELIGIBLE":
                n_ineligible += 1
            else:
                n_review += 1
            if _truthy(r.get("chk_special_chars")):
                special_chars += 1
            for label, pred in issue_defs:
                try:
                    if pred(r):
                        counts[label] += 1
                except Exception:
                    pass

    pct = lambda v: round(v / total * 100, 2) if total else 0.0
    breakdown = [
        {"type": label, "count": counts[label], "pct": pct(counts[label])}
        for label, _ in issue_defs if counts[label] > 0
    ]
    breakdown.sort(key=lambda d: d["count"], reverse=True)

    # PARTIALLY_ELIGIBLE plots (minor overlap) still require field correction,
    # so they are counted together with NEEDS_REVIEW on the dashboard card.
    # The Excel download already returns all 705 of these rows as NEEDS_REVIEW,
    # so combining here makes the card consistent with the download.
    combined_review = n_review + n_partial

    return {
        "total": total,
        "pass": n_eligible,
        "needs_review": combined_review,
        "partial": n_partial,
        "ineligible": n_ineligible,
        "pass_pct": pct(n_eligible),
        "needs_review_pct": pct(combined_review),
        "special_chars": special_chars,
        "breakdown": breakdown,
        "source": path.name,
    }


# ── API: DOWNLOADS (dashboard download dropdown) ─────────────────────────────

@app.get("/api/download/phase2-report")
async def download_phase2_report():
    """Download the latest Phase 2 Excel report, or 404 if not yet produced."""
    candidates = [OUTPUTS / "phase2_results.xlsx"]
    candidates += sorted(OUTPUTS.glob("2E_master_eligibility_report_*.xlsx"))
    for path in candidates:
        if path.exists():
            return FileResponse(
                str(path),
                filename=path.name,
                media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
    raise HTTPException(status_code=404, detail="Phase 2 report not yet available.")


@app.get("/api/download/plots-geojson")
async def download_plots_geojson():
    """Download the latest Phase 1F plots GeoJSON."""
    files = sorted(OUTPUTS.glob("1F_plots_*.geojson"))
    if not files:
        raise HTTPException(status_code=404, detail="No plot GeoJSON found. Run Phase 1 first.")
    path = files[-1]
    return FileResponse(str(path), filename=path.name, media_type="application/geo+json")


@app.get("/api/download/plots-kml")
async def download_plots_kml():
    """Convert the latest Phase 1F plots GeoJSON to KML on the fly."""
    import tempfile
    import geopandas as gpd

    files = sorted(OUTPUTS.glob("1F_plots_*.geojson"))
    if not files:
        raise HTTPException(status_code=404, detail="No plot GeoJSON found. Run Phase 1 first.")

    gdf = gpd.read_file(str(files[-1]))
    gdf = gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty].copy()
    if gdf.empty:
        raise HTTPException(status_code=404, detail="No mappable geometries to export.")
    if gdf.crs is None:
        gdf = gdf.set_crs(epsg=4326)
    else:
        gdf = gdf.to_crs(epsg=4326)

    tmp = Path(tempfile.gettempdir()) / "awd_plots_export.kml"
    gdf.to_file(str(tmp), driver="KML")
    return FileResponse(
        str(tmp), filename="awd_plots_export.kml",
        media_type="application/vnd.google-earth.kml+xml",
    )


@app.get("/api/download/needs-review")
async def download_needs_review():
    """Filter phase1_results.xlsx to NEEDS_REVIEW rows only and return a new workbook."""
    import io
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment

    path = _latest_phase1_excel()
    if path is None:
        raise HTTPException(status_code=404, detail="No Phase 1 report found. Run Phase 1 first.")

    src = openpyxl.load_workbook(str(path))
    src_ws = src.active
    out = openpyxl.Workbook()
    out_ws = out.active
    out_ws.title = "Needs Review"

    rows = list(src_ws.iter_rows(values_only=True))
    header = rows[0]
    out_ws.append(list(header))
    status_idx = list(header).index("Status") if "Status" in header else 1
    for r in rows[1:]:
        if str(r[status_idx]) == "NEEDS_REVIEW":
            out_ws.append(list(r))

    amber = PatternFill(start_color="FFEB9C", end_color="FFEB9C", fill_type="solid")
    for col_idx in range(1, len(header) + 1):
        cell = out_ws.cell(row=1, column=col_idx)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill(start_color="1F4E79", end_color="1F4E79", fill_type="solid")
        cell.alignment = Alignment(wrap_text=True)
    widths = [22, 14, 12, 55, 45, 45, 60, 18]
    for col_idx in range(1, len(header) + 1):
        out_ws.column_dimensions[out_ws.cell(row=1, column=col_idx).column_letter].width = (
            widths[col_idx - 1] if col_idx - 1 < len(widths) else 20
        )
    for row_cells in out_ws.iter_rows(min_row=2):
        for cell in row_cells:
            cell.fill = amber
            cell.alignment = Alignment(wrap_text=True, vertical="top")

    buf = io.BytesIO()
    out.save(buf)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=needs_review_plots.xlsx"},
    )



# ── API: PHASE 2 DOWNLOAD HELPERS ────────────────────────────────────────────

# Column spec for all Phase 2 Excel exports (A–R).
_P2_HEADERS = [
    "Plot ID",                    # A
    "Status",                     # B
    "Farmer Name / ID",           # C  (Name field from KML)
    "Village / Location",         # D  (Description field)
    "Total Plot Area (ha)",       # E
    "Eligible Area (ha)",         # F
    "Non-Eligible Area (ha)",     # G
    "Non-Eligible % of Plot",     # H
    "Forest Overlap (sq m)",      # I
    "Water Body Overlap (sq m)",  # J
    "Settlement Overlap (sq m)",  # K  (no separate wetland; settlements tracked here)
    "Building Overlap (sq m)",    # L
    "Road Overlap (sq m)",        # M
    "Railway Overlap (sq m)",     # N
    "Primary Reason",             # O
    "Data Sources Used",          # P
    "Centroid Longitude",         # Q
    "Centroid Latitude",          # R
]

_P2_COL_WIDTHS = [22, 22, 20, 22, 18, 18, 18, 18, 18, 18, 18, 18, 18, 18, 55, 38, 16, 16]

_P2_STATUS_FILLS = {
    "FULLY_ELIGIBLE":     "C6EFCE",
    "PARTIALLY_ELIGIBLE": "FFEB9C",
    "NEEDS_REVIEW":       "E2E8F0",
    "INELIGIBLE":         "FFC7CE",
}

_P2_HEADER_FILL = "1F4E79"


def _load_phase2_gdf():
    """Load all Phase 2 plot data (30,774 Phase-1-passing plots).

    Concatenates the four 2E status GeoPackages so every row carries
    phase2_eligibility, phase2_eligibility_reason, area columns, and geometry.
    Falls back to 2D interim GeoPackage when 2E gpkgs are absent.
    Returns a WGS84 GeoDataFrame.
    """
    import geopandas as gpd
    import pandas as pd

    gpkg_specs = [
        ("2E_eligible_plots_*.gpkg",  "eligible_plots"),
        ("2E_partial_plots_*.gpkg",   "partial_plots"),
        ("2E_review_plots_*.gpkg",    "review_plots"),
        ("2E_ineligible_plots_*.gpkg","ineligible_plots"),
    ]
    frames = []
    for pattern, layer in gpkg_specs:
        matches = sorted(OUTPUTS.glob(pattern))
        if matches:
            try:
                g = gpd.read_file(str(matches[-1]))
                frames.append(g)
            except Exception:
                pass

    if frames:
        gdf = gpd.GeoDataFrame(pd.concat(frames, ignore_index=True), geometry="geometry")
        if gdf.crs is None:
            gdf = gdf.set_crs(epsg=4326)
        elif gdf.crs.to_epsg() != 4326:
            gdf = gdf.to_crs(epsg=4326)
        return gdf

    # Fallback: 2D interim (no phase2_eligibility column)
    interim = Path("/app/data/interim")
    files_2d = sorted(interim.glob("2D_plots_net_area_*.gpkg"))
    if not files_2d:
        raise HTTPException(status_code=404, detail="No Phase 2 data found. Run Phase 2 first.")
    gdf = gpd.read_file(str(files_2d[-1]))
    if gdf.crs is None:
        gdf = gdf.set_crs(epsg=4326)
    elif gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs(epsg=4326)

    # Add phase2_eligibility from 2E master Excel if present
    excel_files = sorted(OUTPUTS.glob("2E_master_eligibility_report_*.xlsx"))
    if excel_files:
        df2e = pd.read_excel(
            str(excel_files[-1]),
            usecols=["plot_id", "phase2_eligibility", "phase2_eligibility_reason"],
        )
        gdf = gdf.merge(df2e, on="plot_id", how="left")
    elif "phase_2d_status" in gdf.columns:
        gdf["phase2_eligibility"] = gdf["phase_2d_status"]
        gdf["phase2_eligibility_reason"] = ""

    return gdf


def _build_phase2_excel(gdf, sheet_title: str) -> "io.BytesIO":
    """Build a structured A–R Phase 2 Excel workbook and return a BytesIO buffer."""
    import io
    import math
    import geopandas as gpd
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter

    wb = openpyxl.Workbook(write_only=False)
    ws = wb.active
    ws.title = sheet_title[:31]

    hdr_font  = Font(bold=True, color="FFFFFF")
    hdr_fill  = PatternFill(start_color=_P2_HEADER_FILL, end_color=_P2_HEADER_FILL, fill_type="solid")
    hdr_align = Alignment(wrap_text=True, horizontal="center", vertical="center")

    # Write headers
    for col_i, hdr in enumerate(_P2_HEADERS, 1):
        cell = ws.cell(row=1, column=col_i, value=hdr)
        cell.font  = hdr_font
        cell.fill  = hdr_fill
        cell.alignment = hdr_align
        ws.column_dimensions[get_column_letter(col_i)].width = _P2_COL_WIDTHS[col_i - 1]
    ws.row_dimensions[1].height = 30

    def _flt(v, scale=1.0, digits=4):
        try:
            f = float(v)
            return round(f * scale, digits) if math.isfinite(f) else 0.0
        except Exception:
            return 0.0

    def _str(v):
        return "" if v is None or (isinstance(v, float) and math.isnan(v)) else str(v)

    # Pre-sort: status order
    status_order = {"FULLY_ELIGIBLE": 0, "PARTIALLY_ELIGIBLE": 1, "NEEDS_REVIEW": 2, "INELIGIBLE": 3}
    gdf_sorted = gdf.copy()
    if "phase2_eligibility" in gdf_sorted.columns:
        gdf_sorted["_s"] = gdf_sorted["phase2_eligibility"].map(status_order).fillna(9)
    else:
        gdf_sorted["_s"] = 9
    gdf_sorted = gdf_sorted.sort_values("_s").drop(columns=["_s"])

    for row_i, (_, row) in enumerate(gdf_sorted.iterrows(), 2):
        status = _str(row.get("phase2_eligibility") or row.get("phase_2d_status") or "")

        # Centroid in WGS84
        lon = lat = ""
        geom = row.geometry
        if geom is not None and not (hasattr(geom, "is_empty") and geom.is_empty):
            try:
                c = geom.centroid
                lon = round(c.x, 6)
                lat = round(c.y, 6)
            except Exception:
                pass

        # Data sources
        sources = set()
        fds = _str(row.get("forest_definition_source"))
        fos = _str(row.get("forest_overlap_source"))
        wos = _str(row.get("water_overlap_source"))
        for s in (fds, fos, wos):
            if s and s.lower() not in ("none", ""):
                sources.add(s)
        src_str = "; ".join(sorted(sources)) if sources else "Hansen GFC, OSM, JRC GSW"

        non_elig_ha = _flt(row.get("total_non_eligible_ha"))
        area_ha     = _flt(row.get("area_ha"))
        non_elig_pct = round(non_elig_ha / area_ha * 100, 2) if area_ha > 0 else 0.0

        row_vals = [
            _str(row.get("plot_id")),
            status,
            _str(row.get("Name") or row.get("name") or row.get("plot_id")),
            _str(row.get("Description") or row.get("description") or ""),
            _flt(area_ha),
            _flt(row.get("eligible_area_ha")),
            _flt(non_elig_ha),
            non_elig_pct,
            round(_flt(row.get("forest_overlap_ha"), 10000), 1),
            round(_flt(row.get("water_overlap_ha"), 10000), 1),
            round(_flt(row.get("settlements_overlap_ha"), 10000), 1),
            round(_flt(row.get("buildings_overlap_ha"), 10000), 1),
            round(_flt(row.get("roads_overlap_ha"), 10000), 1),
            round(_flt(row.get("railways_overlap_ha"), 10000), 1),
            _str(row.get("phase2_eligibility_reason") or row.get("final_eligibility_reason") or ""),
            src_str,
            lon,
            lat,
        ]

        fill_color = _P2_STATUS_FILLS.get(status, "FFFFFF")
        row_fill   = PatternFill(start_color=fill_color, end_color=fill_color, fill_type="solid")
        row_align  = Alignment(vertical="top", wrap_text=False)

        for col_i, val in enumerate(row_vals, 1):
            cell = ws.cell(row=row_i, column=col_i, value=val)
            cell.fill      = row_fill
            cell.alignment = row_align

    # Freeze header row
    ws.freeze_panes = "A2"

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


def _p2_excel_response(buf: "io.BytesIO", filename: str):
    """Return a Phase 2 Excel BytesIO as a streaming download response."""
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


# ── API: PHASE 2 DOWNLOAD ENDPOINTS ──────────────────────────────────────────

@app.get("/api/download/phase2-all")
async def download_phase2_all():
    """All 30,774 Phase-2-eligible plots with full eligibility results (A–R columns)."""
    gdf = _load_phase2_gdf()
    buf = _build_phase2_excel(gdf, "Phase 2 All Plots")
    return _p2_excel_response(buf, "phase2_results.xlsx")


@app.get("/api/download/phase2-fully-eligible")
async def download_phase2_fully_eligible():
    """FULLY_ELIGIBLE plots only."""
    gdf = _load_phase2_gdf()
    col = "phase2_eligibility" if "phase2_eligibility" in gdf.columns else "phase_2d_status"
    subset = gdf[gdf[col] == "FULLY_ELIGIBLE"].copy()
    if subset.empty:
        raise HTTPException(status_code=404, detail="No fully eligible plots found.")
    buf = _build_phase2_excel(subset, "Fully Eligible Plots")
    return _p2_excel_response(buf, "fully_eligible_plots.xlsx")


@app.get("/api/download/phase2-partially-eligible")
async def download_phase2_partially_eligible():
    """PARTIALLY_ELIGIBLE plots only — includes eligible/non-eligible area breakdown."""
    gdf = _load_phase2_gdf()
    col = "phase2_eligibility" if "phase2_eligibility" in gdf.columns else "phase_2d_status"
    subset = gdf[gdf[col] == "PARTIALLY_ELIGIBLE"].copy()
    if subset.empty:
        raise HTTPException(status_code=404, detail="No partially eligible plots found.")
    buf = _build_phase2_excel(subset, "Partially Eligible Plots")
    return _p2_excel_response(buf, "partially_eligible_plots.xlsx")


@app.get("/api/download/phase2-needs-review")
async def download_phase2_needs_review():
    """NEEDS_REVIEW plots from Phase 2."""
    gdf = _load_phase2_gdf()
    col = "phase2_eligibility" if "phase2_eligibility" in gdf.columns else "phase_2d_status"
    subset = gdf[gdf[col] == "NEEDS_REVIEW"].copy()
    if subset.empty:
        raise HTTPException(status_code=404, detail="No Phase 2 needs-review plots found.")
    buf = _build_phase2_excel(subset, "Phase 2 Needs Review")
    return _p2_excel_response(buf, "phase2_needs_review.xlsx")


@app.get("/api/download/phase2-ineligible")
async def download_phase2_ineligible():
    """INELIGIBLE plots — includes reason and triggering layer."""
    gdf = _load_phase2_gdf()
    col = "phase2_eligibility" if "phase2_eligibility" in gdf.columns else "phase_2d_status"
    subset = gdf[gdf[col] == "INELIGIBLE"].copy()
    if subset.empty:
        raise HTTPException(status_code=404, detail="No ineligible plots found.")
    buf = _build_phase2_excel(subset, "Ineligible Plots")
    return _p2_excel_response(buf, "ineligible_plots.xlsx")


@app.get("/api/download/phase2-noneligible-breakdown")
async def download_phase2_noneligible_breakdown():
    """One row per plot: non-eligible area breakdown by category (sq m)."""
    import io
    import math
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter

    gdf = _load_phase2_gdf()

    headers = [
        "Plot ID", "Status",
        "Total Plot Area (ha)", "Eligible Area (ha)",
        "Forest (sq m)", "Water Body (sq m)", "Settlement (sq m)",
        "Building (sq m)", "Road (sq m)", "Railway (sq m)",
        "Total Non-Eligible (sq m)", "Eligible Area (ha) [confirmed]",
        "Centroid Longitude", "Centroid Latitude",
    ]
    widths = [22, 22, 18, 18, 16, 16, 16, 16, 16, 16, 20, 20, 16, 16]

    wb  = openpyxl.Workbook()
    ws  = wb.active
    ws.title = "Non-Eligible Breakdown"

    hdr_fill  = PatternFill(start_color=_P2_HEADER_FILL, end_color=_P2_HEADER_FILL, fill_type="solid")
    hdr_font  = Font(bold=True, color="FFFFFF")
    hdr_align = Alignment(wrap_text=True, horizontal="center", vertical="center")
    for col_i, h in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col_i, value=h)
        cell.font = hdr_font; cell.fill = hdr_fill; cell.alignment = hdr_align
        ws.column_dimensions[get_column_letter(col_i)].width = widths[col_i - 1]
    ws.row_dimensions[1].height = 30
    ws.freeze_panes = "A2"

    def _f(v, scale=1.0, d=1):
        try:
            x = float(v)
            return round(x * scale, d) if math.isfinite(x) else 0.0
        except Exception:
            return 0.0

    col = "phase2_eligibility" if "phase2_eligibility" in gdf.columns else "phase_2d_status"
    status_order = {"FULLY_ELIGIBLE": 0, "PARTIALLY_ELIGIBLE": 1, "NEEDS_REVIEW": 2, "INELIGIBLE": 3}
    gdf_s = gdf.copy()
    gdf_s["_s"] = gdf_s[col].map(status_order).fillna(9)
    gdf_s = gdf_s.sort_values("_s").drop(columns=["_s"])

    for row_i, (_, row) in enumerate(gdf_s.iterrows(), 2):
        status = str(row.get(col) or "")
        lon = lat = ""
        geom = row.geometry
        if geom is not None and not (hasattr(geom, "is_empty") and geom.is_empty):
            try:
                c = geom.centroid; lon = round(c.x, 6); lat = round(c.y, 6)
            except Exception:
                pass

        vals = [
            str(row.get("plot_id") or ""),
            status,
            _f(row.get("area_ha"), 1, 4),
            _f(row.get("eligible_area_ha"), 1, 4),
            _f(row.get("forest_overlap_ha"), 10000, 1),
            _f(row.get("water_overlap_ha"), 10000, 1),
            _f(row.get("settlements_overlap_ha"), 10000, 1),
            _f(row.get("buildings_overlap_ha"), 10000, 1),
            _f(row.get("roads_overlap_ha"), 10000, 1),
            _f(row.get("railways_overlap_ha"), 10000, 1),
            _f(row.get("total_non_eligible_ha"), 10000, 1),
            _f(row.get("eligible_area_ha"), 1, 4),
            lon, lat,
        ]
        fill_color = _P2_STATUS_FILLS.get(status, "FFFFFF")
        rf = PatternFill(start_color=fill_color, end_color=fill_color, fill_type="solid")
        ra = Alignment(vertical="top")
        for col_i, val in enumerate(vals, 1):
            cell = ws.cell(row=row_i, column=col_i, value=val)
            cell.fill = rf; cell.alignment = ra

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=noneligible_area_breakdown.xlsx"},
    )


@app.get("/api/download/eligible-area-summary-pdf")
async def download_eligible_area_summary_pdf():
    """One-page PDF summary: eligible area, plot counts, non-eligible breakdown, run metadata."""
    import io
    import json as _json
    import math
    from fpdf import FPDF

    # Load latest Phase 2 summary JSON
    summary_files = sorted(OUTPUTS.glob("2E_phase2_summary_*.json"))
    if not summary_files:
        raise HTTPException(status_code=404, detail="No Phase 2 summary found. Run Phase 2 first.")
    with open(summary_files[-1]) as f:
        summary = _json.load(f)

    run_id    = summary.get("run_id", "—")
    timestamp = summary.get("timestamp", "")[:16].replace("T", " ")
    standard  = summary.get("standard", "VCS v5.0")
    total     = summary.get("total_plots", 0)
    carbon    = summary.get("carbon_summary", {})
    p2_status = summary.get("phase2_status_summary", {})

    eligible_ha   = carbon.get("total_eligible_area_ha", 0.0)
    adj_factor    = carbon.get("area_weighted_adj_factor", carbon.get("avg_adjustment_factor", 0.0))
    for_review    = carbon.get("plots_needing_review", 0)

    # Accumulate non-eligible areas from Phase 2 GeoPackage data
    try:
        gdf = _load_phase2_gdf()
        def _sum(col): return float(gdf[col].fillna(0).sum()) if col in gdf.columns else 0.0
        forest_ha  = _sum("forest_overlap_ha")
        water_ha   = _sum("water_overlap_ha")
        settle_ha  = _sum("settlements_overlap_ha")
        build_ha   = _sum("buildings_overlap_ha")
        roads_ha   = _sum("roads_overlap_ha")
        rail_ha    = _sum("railways_overlap_ha")
    except Exception:
        forest_ha = water_ha = settle_ha = build_ha = roads_ha = rail_ha = 0.0

    # ── Build PDF ─────────────────────────────────────────────────────────────
    class _PDF(FPDF):
        def header(self):
            self.set_fill_color(31, 78, 121)
            self.rect(0, 0, 210, 20, "F")
            self.set_font("Helvetica", "B", 14)
            self.set_text_color(255, 255, 255)
            self.set_xy(10, 5)
            self.cell(0, 10, "AWD Plot Eligibility - Eligible Area Summary", align="L")

        def footer(self):
            self.set_y(-12)
            self.set_font("Helvetica", "", 8)
            self.set_text_color(150, 150, 150)
            self.cell(0, 5, "AWD Validation Pipeline  |  VCS v5.0  |  Maharashtra, India", align="C")

    pdf = _PDF(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=False)
    pdf.add_page()

    # ── Meta block ────────────────────────────────────────────────────────────
    pdf.set_xy(10, 26)
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(100, 100, 100)
    pdf.cell(80, 5, f"Run ID: {run_id}", ln=0)
    pdf.cell(80, 5, f"Standard: {standard}", ln=0)
    pdf.cell(0,  5, f"Generated: {timestamp} UTC", ln=1)

    # ── Key numbers ──────────────────────────────────────────────────────────
    def kpi_card(x, y, label, value, sub=""):
        pdf.set_fill_color(232, 243, 233)
        pdf.rect(x, y, 44, 22, "F")
        pdf.set_xy(x + 2, y + 2)
        pdf.set_font("Helvetica", "B", 16)
        pdf.set_text_color(45, 106, 79)
        pdf.cell(40, 9, str(value), ln=1, align="C")
        pdf.set_xy(x + 2, y + 11)
        pdf.set_font("Helvetica", "", 7)
        pdf.set_text_color(80, 80, 80)
        pdf.cell(40, 4, label, ln=1, align="C")
        if sub:
            pdf.set_xy(x + 2, y + 15)
            pdf.set_font("Helvetica", "I", 7)
            pdf.set_text_color(120, 120, 120)
            pdf.cell(40, 4, sub, ln=1, align="C")

    fe  = p2_status.get("FULLY_ELIGIBLE", {}).get("count", 0)
    pe  = p2_status.get("PARTIALLY_ELIGIBLE", {}).get("count", 0)
    nr2 = p2_status.get("NEEDS_REVIEW", {}).get("count", 0)
    ine = p2_status.get("INELIGIBLE", {}).get("count", 0)

    kpi_card(10,  35, "Total Plots (Phase 2)",   f"{total:,}",           "Phase 1 passing plots")
    kpi_card(58,  35, "Fully Eligible",           f"{fe:,}",              f"{p2_status.get('FULLY_ELIGIBLE',{}).get('pct',0):.1f}%")
    kpi_card(106, 35, "Partially Eligible",       f"{pe:,}",              f"{p2_status.get('PARTIALLY_ELIGIBLE',{}).get('pct',0):.1f}%")
    kpi_card(154, 35, "Net Eligible Area",        f"{eligible_ha:,.0f} ha", f"Adj. factor: {adj_factor:.4f}")

    # ── Status breakdown ─────────────────────────────────────────────────────
    pdf.set_xy(10, 64)
    pdf.set_font("Helvetica", "B", 10)
    pdf.set_text_color(31, 78, 121)
    pdf.cell(0, 7, "Phase 2 Eligibility Status Breakdown", ln=True)
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(50, 50, 50)

    status_rows = [
        ("Fully Eligible",       fe,  p2_status.get("FULLY_ELIGIBLE",{}).get("pct",0),      (198, 239, 206)),
        ("Partially Eligible",   pe,  p2_status.get("PARTIALLY_ELIGIBLE",{}).get("pct",0),  (255, 235, 156)),
        ("Needs Review",         nr2, p2_status.get("NEEDS_REVIEW",{}).get("pct",0),         (226, 232, 240)),
        ("Ineligible",           ine, p2_status.get("INELIGIBLE",{}).get("pct",0),           (255, 199, 206)),
    ]
    for label, count, pct, rgb in status_rows:
        pdf.set_fill_color(*rgb)
        pdf.set_xy(10, pdf.get_y())
        pdf.cell(80, 7, f"  {label}", fill=True, border=0)
        pdf.cell(40, 7, f"{count:,}", fill=True, border=0, align="R")
        pdf.cell(30, 7, f"{pct:.2f}%", fill=True, border=0, align="R")
        pdf.ln(7)

    # ── Non-eligible area breakdown ──────────────────────────────────────────
    pdf.ln(4)
    pdf.set_font("Helvetica", "B", 10)
    pdf.set_text_color(31, 78, 121)
    pdf.cell(0, 7, "Non-Eligible Area Breakdown (cumulative across all plots)", ln=True)
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(50, 50, 50)

    nelig_rows = [
        ("Forest cover",     forest_ha,  "Bharatmaps RFA + Hansen GFC"),
        ("Water bodies",     water_ha,   "OSM + JRC Global Surface Water"),
        ("Settlements",      settle_ha,  "OSM place/landuse"),
        ("Buildings",        build_ha,   "Microsoft/Google Open Buildings"),
        ("Roads (+/-5m)",    roads_ha,   "OSM highway"),
        ("Railways (+/-10m)", rail_ha,   "OSM railway"),
    ]
    alt = True
    for label, ha, source in nelig_rows:
        pdf.set_fill_color(247, 248, 250) if alt else pdf.set_fill_color(255, 255, 255)
        alt = not alt
        pdf.set_xy(10, pdf.get_y())
        pdf.cell(75, 7, f"  {label}", fill=True)
        pdf.cell(35, 7, f"{ha:,.2f} ha", fill=True, align="R")
        pdf.cell(80, 7, f"  {source}", fill=True)
        pdf.ln(7)

    # ── Notes ────────────────────────────────────────────────────────────────
    pdf.ln(4)
    pdf.set_fill_color(254, 243, 199)
    note_y = pdf.get_y()
    pdf.set_xy(10, note_y)
    pdf.set_font("Helvetica", "I", 8)
    pdf.set_text_color(146, 64, 14)
    pdf.multi_cell(
        190, 5,
        f"NOTE: Carbon stock must be calculated using eligible_area_ha, not total plot area. "
        f"Area-weighted adjustment factor: {adj_factor:.4f}. "
        f"{for_review:,} plots flagged for manual review before carbon calculation. "
        "Phase 2 covers only the 30,774 plots that passed Phase 1 geometry QC - "
        "641 Phase 1 NEEDS_REVIEW plots are excluded from Phase 2.",
        fill=True,
    )

    buf = io.BytesIO()
    pdf.output(buf)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/pdf",
        headers={"Content-Disposition": "attachment; filename=eligible_area_summary.pdf"},
    )


# ── API: KML UPLOAD → GEOJSON (dashboard map overlay) ────────────────────────

@app.post("/api/upload/kml")
async def upload_kml_overlay(file: UploadFile = File(...)):
    """
    Parse an uploaded .kml/.kmz file and return its plots as GeoJSON (EPSG:4326).

    Used by the dashboard 'Upload KML' button to render an ad-hoc plot layer on
    the map. The file is NOT ingested into the pipeline — it is parsed and
    discarded; only the GeoJSON is returned.
    """
    import tempfile
    import geopandas as gpd
    import pandas as pd

    name = (file.filename or "").lower()
    if not (name.endswith(".kml") or name.endswith(".kmz")):
        raise HTTPException(status_code=400, detail="Only .kml or .kmz files are accepted")

    suffix = ".kmz" if name.endswith(".kmz") else ".kml"
    tmp = Path(tempfile.gettempdir()) / f"awd_upload_{uuid.uuid4().hex[:8]}{suffix}"
    with open(tmp, "wb") as f:
        shutil.copyfileobj(file.file, f)

    try:
        frames = []
        try:
            from pyogrio import list_layers
            layers = list_layers(str(tmp))
            for layer_name in [row[0] for row in layers]:
                try:
                    g = gpd.read_file(str(tmp), layer=layer_name)
                    if len(g):
                        frames.append(g)
                except Exception:
                    continue
        except Exception:
            pass
        if frames:
            gdf = pd.concat(frames, ignore_index=True)
            gdf = gpd.GeoDataFrame(gdf, geometry="geometry", crs=frames[0].crs)
        else:
            gdf = gpd.read_file(str(tmp))

        if gdf.crs is None:
            gdf = gdf.set_crs(epsg=4326)
        else:
            gdf = gdf.to_crs(epsg=4326)
        gdf = gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty].copy()

        geojson = json.loads(gdf.to_json())
        geojson["count"] = len(gdf)
        geojson["filename"] = file.filename
        return geojson
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not parse KML: {exc}")
    finally:
        try:
            tmp.unlink()
        except Exception:
            pass


# ── LEGACY ENDPOINTS (kept for backward compatibility) ────────────────────────

@app.post("/upload")
async def upload_kml(file: UploadFile = File(...)):
    if not file.filename.lower().endswith(".kml"):
        raise HTTPException(status_code=400, detail="Only .kml files are accepted")
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    kml_path = str(RAW_DIR / file.filename)
    with open(kml_path, "wb") as f:
        shutil.copyfileobj(file.file, f)
    run_id = datetime.utcnow().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
    RUNS[run_id] = {"run_id": run_id, "status": "queued", "kml_file": file.filename,
                    "logs": [], "outputs": [], "summary": {}}
    threading.Thread(target=_run_pipeline, args=(run_id, kml_path), daemon=True).start()
    size_mb = round(os.path.getsize(kml_path) / 1024 / 1024, 2)
    return {"run_id": run_id, "kml_file": file.filename, "file_size_mb": size_mb, "status": "queued"}


@app.get("/status/{run_id}")
async def get_status(run_id: str):
    if run_id not in RUNS:
        raise HTTPException(status_code=404, detail="Run not found")
    run = RUNS[run_id]
    return {"run_id": run_id, "status": run["status"], "outputs": run["outputs"],
            "summary": run["summary"]}


@app.get("/logs/{run_id}")
async def stream_logs_legacy(run_id: str):
    if run_id not in RUNS:
        raise HTTPException(status_code=404, detail="Run not found")

    async def event_generator():
        sent = 0
        while True:
            logs = RUNS[run_id]["logs"]
            while sent < len(logs):
                line = logs[sent].replace("\n", " ")
                yield f"data: {json.dumps({'line': line})}\n\n"
                sent += 1
            status = RUNS[run_id]["status"]
            if status in ("complete", "failed"):
                yield f"data: {json.dumps({'status': status, 'outputs': RUNS[run_id]['outputs'], 'summary': RUNS[run_id]['summary']})}\n\n"
                yield f"data: {json.dumps({'done': True})}\n\n"
                break
            await asyncio.sleep(0.3)

    return StreamingResponse(
        event_generator(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                 "Access-Control-Allow-Origin": "*"},
    )


@app.get("/download/{filename}")
async def download_file(filename: str):
    file_path = OUTPUTS / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail=f"File not found: {filename}")
    return FileResponse(path=str(file_path), filename=filename,
                        media_type="application/octet-stream")
