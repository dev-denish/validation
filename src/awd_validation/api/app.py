import asyncio
import glob
import json
import os
import shutil
import sys
import threading
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse

sys.path.insert(0, "/app/src")

app = FastAPI(title="AWD Plot Validation API", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

RUNS    = {}
RAW_DIR = Path("/app/data/raw")
OUTPUTS = Path("/app/data/outputs")


def _run_pipeline(run_id: str, kml_path: str):
    import subprocess
    RUNS[run_id]["status"] = "running"
    RUNS[run_id]["logs"].append(f"Pipeline started — {datetime.utcnow().isoformat()}")
    RUNS[run_id]["logs"].append(f"KML file: {Path(kml_path).name}")
    RUNS[run_id]["logs"].append("=" * 60)

    env = os.environ.copy()
    env["AWD_KML_PATH"] = kml_path
    env["AWD_RUN_ID"]   = run_id

    try:
        proc = subprocess.Popen(
            ["python", "/app/scripts/run_pipeline.py"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
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
    except Exception as e:
        RUNS[run_id]["status"] = "failed"
        RUNS[run_id]["logs"].append(f"ERROR: {e}")


def _collect_outputs(run_id: str):
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
            outputs.append({"label": label, "filename": fname, "size_mb": size_mb, "url": f"/download/{fname}"})
            RUNS[run_id]["logs"].append(f"  OUTPUT: {label} ({size_mb} MB)")
    RUNS[run_id]["outputs"] = outputs

    summary = {}
    for line in RUNS[run_id]["logs"]:
        try:
            if "ELIGIBLE" in line and "plots" in line and "PARTIALLY" not in line and "INELIGIBLE" not in line:
                parts = line.strip().split()
                for i, p in enumerate(parts):
                    if p == "ELIGIBLE" and i + 1 < len(parts):
                        summary["eligible"] = int(parts[i+1].replace(",",""))
            if "INELIGIBLE" in line and "plots" in line:
                parts = line.strip().split()
                for i, p in enumerate(parts):
                    if p == "INELIGIBLE" and i + 1 < len(parts):
                        summary["ineligible"] = int(parts[i+1].replace(",",""))
            if "PARTIALLY_ELIGIBLE" in line and "plots" in line:
                parts = line.strip().split()
                for i, p in enumerate(parts):
                    if p == "PARTIALLY_ELIGIBLE" and i + 1 < len(parts):
                        summary["partial"] = int(parts[i+1].replace(",",""))
            if "Total plots:" in line:
                summary["total"] = int(line.split("Total plots:")[-1].strip().replace(",",""))
            if "Total runtime:" in line:
                summary["runtime"] = line.split("Total runtime:")[-1].strip()
        except Exception:
            pass
    RUNS[run_id]["summary"] = summary


@app.get("/", response_class=HTMLResponse)
async def serve_ui():
    html_path = Path("/app/src/awd_validation/api/index.html")
    if html_path.exists():
        return HTMLResponse(content=html_path.read_text())
    return HTMLResponse("<h1>UI not found</h1>")


@app.post("/upload")
async def upload_kml(file: UploadFile = File(...)):
    if not file.filename.lower().endswith(".kml"):
        raise HTTPException(status_code=400, detail="Only .kml files are accepted")
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    kml_path = str(RAW_DIR / file.filename)
    with open(kml_path, "wb") as f:
        shutil.copyfileobj(file.file, f)
    run_id = datetime.utcnow().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
    RUNS[run_id] = {"run_id": run_id, "status": "queued", "kml_file": file.filename, "logs": [], "outputs": [], "summary": {}}
    threading.Thread(target=_run_pipeline, args=(run_id, kml_path), daemon=True).start()
    size_mb = round(os.path.getsize(kml_path) / 1024 / 1024, 2)
    return {"run_id": run_id, "kml_file": file.filename, "file_size_mb": size_mb, "status": "queued"}


@app.get("/status/{run_id}")
async def get_status(run_id: str):
    if run_id not in RUNS:
        raise HTTPException(status_code=404, detail="Run not found")
    run = RUNS[run_id]
    return {"run_id": run_id, "status": run["status"], "outputs": run["outputs"], "summary": run["summary"]}


@app.get("/logs/{run_id}")
async def stream_logs(run_id: str):
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

    return StreamingResponse(event_generator(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Access-Control-Allow-Origin": "*"})


@app.get("/download/{filename}")
async def download_file(filename: str):
    file_path = OUTPUTS / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail=f"File not found: {filename}")
    return FileResponse(path=str(file_path), filename=filename, media_type="application/octet-stream")
