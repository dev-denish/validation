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
