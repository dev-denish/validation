# Validation Pipeline

A tool that checks a set of farm plot polygons (uploaded as a `.kml` file) and tells you
which plots are **eligible**, **partially eligible**, or **ineligible** based on geometry
and data quality rules.

You upload a KML file through a browser page, the pipeline runs automatically, and you get
back an Excel report, an interactive map, and split GeoPackage files — all downloadable
from the same page.

---

## What it checks

| Phase | What it looks for |
|-------|-------------------|
| 1A | Reads the KML, fixes coordinate dimensions, calculates plot areas |
| 1B | Finds broken polygon shapes — self-intersections, null geometries |
| 1C | Checks plot IDs for formatting errors, special characters, and duplicates |
| 1D | Finds two different plots registered at the same physical location |
| 1E | Finds plots that overlap each other |
| 1F | Assigns a final status to every plot and writes all output files |

## Output files

After the pipeline runs you can download:

- **Excel report** — every plot with a pass/fail per check, colour coded
- **Interactive map** — open in any browser, click plots to see their status
- **Valid plots GeoPackage** — the clean eligible plots ready for the next stage
- **Ineligible plots GeoPackage** — plots the field team needs to fix and resubmit
- **Partial plots GeoPackage** — plots with minor overlaps that need boundary review
- **CSV** — full row-by-row audit trail

---

## Getting started

Pick the setup that matches your machine. No prior GIS or programming experience needed —
just follow the steps in order.

---

### Windows

**Step 1 — Install Miniconda** (skip if you already have conda or Anaconda)

Download and run the installer from:
https://docs.conda.io/en/latest/miniconda.html

Accept all defaults. When it finishes, open **Anaconda Prompt** from the Start menu
(not regular Command Prompt).

**Step 2 — Create the environment**

Copy and paste these lines one at a time into Anaconda Prompt:

```bash
conda create -n awd python=3.11 -y
```
```bash
conda activate awd
```
```bash
conda install -c conda-forge gdal geopandas openpyxl folium -y
```
```bash
pip install fastapi "uvicorn[standard]" python-multipart loguru pyyaml
```

This takes 3–5 minutes on first run.

**Step 3 — Download the project**

Still in Anaconda Prompt:

```bash
git clone https://github.com/dev-denish/validation.git
cd validation
```

If you do not have git installed, download it from https://git-scm.com/download/win,
install it, then restart Anaconda Prompt and try again.

**Step 4 — Start the application**

```bash
conda activate awd
cd validation
python scripts/run_server.py
```

You should see something like:
```
INFO:     Uvicorn running on http://0.0.0.0:8000
```

**Step 5 — Open in your browser**

Go to: http://localhost:8000

You will see the upload page. Drag your `.kml` file onto it, click
**Run Validation Pipeline**, and watch it run.

When it finishes, download buttons appear at the bottom of the page.

**To stop the server:** press `Ctrl + C` in Anaconda Prompt.

**Next time you want to use it:**

```bash
conda activate awd
cd validation
python scripts/run_server.py
```

---

### macOS

**Step 1 — Install Homebrew** (skip if you already have it)

Open Terminal and run:
```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

**Step 2 — Install GDAL and Python**

```bash
brew install gdal python@3.11
```

**Step 3 — Download the project**

```bash
git clone https://github.com/dev-denish/validation.git
cd validation
```

**Step 4 — Create the environment and install dependencies**

```bash
python3.11 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install GDAL==$(gdal-config --version) --no-binary GDAL
pip install -r requirements.txt
```

**Step 5 — Start the application**

```bash
python scripts/run_server.py
```

Open http://localhost:8000 in your browser.

---

### Ubuntu / Debian Linux

**Step 1 — Install system dependencies**

```bash
sudo apt-get update
sudo apt-get install -y gdal-bin libgdal-dev python3.11 python3.11-venv git
```

**Step 2 — Download the project**

```bash
git clone https://github.com/dev-denish/validation.git
cd validation
```

**Step 3 — Create environment and install dependencies**

```bash
python3.11 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install GDAL==$(gdal-config --version) --no-binary GDAL
pip install -r requirements.txt
```

**Step 4 — Start the application**

```bash
python scripts/run_server.py
```

Open http://localhost:8000 in your browser.

---

### Docker (any OS — Windows, macOS, Linux)

Docker packages everything the pipeline needs into a container, so you do not need to
install Python, GDAL, or any other dependency yourself. If you already have Docker Desktop
installed this is the quickest way to get started.

**Step 1 — Install Docker Desktop** (skip if you already have it)

Download and install from: https://www.docker.com/products/docker-desktop

Accept all defaults. After installation, open Docker Desktop and wait until the whale icon
in the taskbar shows **"Docker Desktop is running"** before continuing.

**Step 2 — Download the project**

Open a terminal (Terminal on macOS/Linux, Command Prompt or PowerShell on Windows) and run:

```bash
git clone https://github.com/dev-denish/validation.git
cd validation
```

If you do not have git, download it first:
- Windows: https://git-scm.com/download/win
- macOS: `brew install git` (requires Homebrew — see macOS section above)
- Linux: `sudo apt-get install git`

**Step 3 — Pull the image from Docker Hub**

```bash
docker pull denish1/awd-validation:latest
```

This downloads the pre-built pipeline image (~800 MB). Only needed once.

**Step 4 — Start the containers**

```bash
docker compose up -d
```

The `-d` flag runs it in the background. You should see:

```
✔ Container awd-postgis      Started
✔ Container awd-validation   Started
```

**Step 5 — Open in your browser**

Go to: http://localhost:8000

Drag your `.kml` file onto the upload zone, click **Run Validation Pipeline**, and watch it run.
When it finishes, download buttons appear at the bottom of the page.

**To stop the containers when you are done:**

```bash
docker compose down
```

**Next time you want to use it** (containers are already pulled):

```bash
cd validation
docker compose up -d
# open http://localhost:8000
```

**Run the pipeline from the terminal instead of the browser:**

```bash
# Copy your KML into the data folder
cp /path/to/your_file.kml data/raw/

# Run the full pipeline inside the container
docker exec -it awd-validation python /app/scripts/run_pipeline.py
```

**Copy outputs to your desktop (Windows with WSL):**

```bash
cp ~/validation/data/outputs/*.xlsx "/mnt/c/Users/$USER/Desktop/"
cp ~/validation/data/outputs/*.html  "/mnt/c/Users/$USER/Desktop/"
```

**Useful Docker commands:**

```bash
# Check containers are running
docker ps

# See container logs if something goes wrong
docker logs awd-validation
docker logs awd-postgis

# Open a shell inside the container
docker exec -it awd-validation bash

# Remove containers completely (data/outputs is kept on your machine)
docker compose down --remove-orphans
```

---

## Running from the command line instead of the browser

If you prefer not to use the web interface:

```bash
# Copy your KML into the data folder first
cp /path/to/your_file.kml data/raw/

# Run the full pipeline
python scripts/run_pipeline.py
```

Outputs are written to `data/outputs/`.

---

## Troubleshooting

**"GDAL not found" or import errors on Windows**
Make sure you installed via conda and that the environment is activated (`conda activate awd`).
Do not use regular pip to install GDAL on Windows — it will fail without conda.

**"Port 8000 already in use"**
Something else is running on that port. Either stop it, or open `scripts/run_server.py`
and change `port=8000` to `port=8001` at the bottom of the file,
then go to http://localhost:8001 instead.

**The page loads but nothing happens after uploading**
Check that the terminal still shows the server running. If it crashed, restart it
(`python scripts/run_server.py`) and try again.

**Pipeline finishes but download buttons do not appear**
Refresh the page and re-upload the file. This can happen if the browser
connection dropped during a long run.

**Docker — "port 8000 is already allocated"**
Another application is using port 8000. Open `docker-compose.yml`, find the line
`"8000:8000"` and change it to `"8001:8000"`, then run `docker compose up -d` again
and go to http://localhost:8001 instead.

**Docker — "Cannot connect to the Docker daemon"**
Docker Desktop is not running. Open Docker Desktop and wait for it to fully start
(whale icon in taskbar), then try again.

**Docker — containers start but http://localhost:8000 shows nothing**
Wait 10 seconds and refresh — the pipeline container takes a moment to be ready after
`docker compose up`. If it still fails, run `docker logs awd-validation` to see the error.

---

## Project layout

```
validation/
├── config/
│   └── settings.yaml        ← thresholds and paths
├── data/
│   ├── raw/                 ← put your KML files here
│   ├── interim/             ← phase-by-phase working files (auto-generated)
│   └── outputs/             ← your reports and downloads (auto-generated)
├── src/awd_validation/      ← pipeline source code
├── scripts/
│   ├── run_pipeline.py      ← run full pipeline from command line
│   ├── run_server.py        ← start the web UI
│   └── run_phase_1a.py … run_phase_1f.py
├── requirements.txt
└── README.md
```

> The `data/` folders are excluded from git — your KML files and outputs stay on your machine only.
