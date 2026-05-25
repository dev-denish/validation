# AWD Plot Eligibility Validation Framework
# Maharashtra, India — VCS Standard v5.0

## Project Overview
This system validates ~19,000–20,000 AWD (Alternate Wetting and Drying) farm plots
for carbon project eligibility under VCS Standard 5, UNFCCC CDM, and Gold Standard.
Two validation phases: Phase 1 (plot creation QC) and Phase 2 (eligibility checks).

**Assigned Developer:** Denish | **Supervised by:** Jibotosh & Sabik (VNV)
**Location:** Bengaluru, Karnataka, India

---

## Pipeline Architecture

### Phase 1 — Plot Creation QC (COMPLETE ✅)
```
1A → Ingest KML → Standardize → GeoPackage
1B → Geometry validity (invalid polygons, self-intersections, unclosed rings)
1C → Unique ID duplicates (field: "Name", pattern: ^[0-9]+-[0-9]+-[A-Z]+-[0-9]+$)
1D → Spatial/locational duplicates (centroid tolerance: 2.0m)
1E → Overlap/topology (>1% overlap area = real overlap)
1F → Master report + HTML map + Excel output
```

### Phase 2 — Eligibility Cross-Check (IN PROGRESS ❌)
```
2A → Forest cover check (Maharashtra state definition → CDM DNA fallback)
2B → Permanent water body / wetland overlap
2C → Man-made non-eligible areas (roads +5m, railways +10m, settlements, buildings)
2D → Net eligible area calculation per plot
2E → Final eligibility classification + carbon adjustment factor
```

---

## Critical Configuration

**Input Files:**
- `data/raw/farms_kmlFile.kml` — primary AWD plots
- `data/raw/cosmosVDC.kml` — secondary reference

**Unique ID Field:** `Name`
**ID Pattern:** `^[0-9]+-[0-9]+-[A-Z]+-[0-9]+$`

**CRS:**
- Input: EPSG:4326 (WGS84)
- Working: EPSG:32643 (UTM Zone 43N — correct for Maharashtra)
- Output: EPSG:4326

**Database:** PostGIS at `localhost:5432`, db=`awd_validation`, schema=`awd`

**Docker paths:** All scripts use `/app/` prefix (not `~/awd-validation/`)

---

## Forest Definition (Phase 2 — PENDING CONFIRMATION)
- First check: Maharashtra State Forest Department official definition
- Fallback: UNFCCC CDM DNA → https://cdm.unfccc.int/DNA/index.html
- CDM default: 10% canopy cover, 5m height, 0.5 ha minimum area
- DO NOT proceed with Phase 2 forest checks until Jibotosh confirms definition

---

## Non-Eligible Area Categories (Phase 2)
Priority order for overlap checks:
1. Forest (per confirmed definition above)
2. Permanent water bodies (rivers, lakes, reservoirs >0.5 ha)
3. Roads: use 5m buffer
4. Railways: use 10m buffer
5. Settlements, buildings, factories, schools: no buffer

---

## Eligibility Classification Logic
```
if geometry_invalid              → ERROR (flag for manual review)
elif non_eligible_area == 0      → FULLY_ELIGIBLE
elif 0 < non_eligible_area < total_area → PARTIALLY_ELIGIBLE
elif non_eligible_area >= total_area    → INELIGIBLE
```

**Carbon Adjustment (CRITICAL):**
- Adjustment Factor = Eligible Area / Total Area
- NEVER calculate carbon on total area if non-eligible area exists
- Flag plots losing >25% area for Jibotosh review

---

## Coding Standards (MANDATORY)
- Python 3.8+, PEP 8, Black formatter (100 char line length)
- Type hints required on ALL functions
- Google-style docstrings on ALL public methods
- pylint score minimum 8.5/10
- pytest coverage minimum 80%
- No bare `except:` clauses
- No hardcoded paths — always use `Config` class from `src/awd_validation/utils/config.py`

---

## Output Naming Convention
ALL output files must use timestamp suffix: `YYYYMMDD_HHMMSS`
NEVER overwrite existing output files.
```
data/outputs/1F_master_validation_report_{run_id}.xlsx
data/outputs/valid_plots_{run_id}.gpkg
data/outputs/ineligible_plots_{run_id}.gpkg
data/outputs/partial_plots_{run_id}.gpkg
```

---

## Performance Targets
- Full Phase 1 pipeline: < 45 minutes for 19,000–20,000 plots
- Memory usage: < 2 GB
- Alert if any single operation exceeds 10 seconds
- Use R-tree spatial indexing for all spatial joins
- Batch process in chunks of 5,000 if memory constrained

---

## Git Commit Format
```
[FEATURE] Add forest definition validator
[BUGFIX] Fix CRS mismatch in overlap detection
[PERF] Optimize spatial indexing for 19000+ plots
[DOCS] Update README with carbon adjustment formula
[TEST] Add unit tests for geometry validator
[REFACTOR] Split eligibility checks into Phase 2 modules
```

---

## Open Items (Awaiting Jibotosh)
- [ ] Maharashtra forest definition (or CDM DNA fallback confirmed)
- [ ] Forest classification GIS layer
- [ ] Water bodies / wetlands GIS layer
- [ ] Infrastructure layer (roads, railways, settlements)
- [ ] Partial eligibility threshold (minimum % to retain a plot)
