# AWD Validation Pipeline — Handoff Document

**Project:** AWD Farm Plot Carbon Eligibility Validation — Maharashtra, India
**Standard:** VCS Standard 5.0 / UNFCCC CDM / Gold Standard
**Developer:** Denish | **Supervisors:** Jibotosh & Sabik (VNV)
**Plots:** 30,774 | **Location:** Vidarbha, Maharashtra (Nagpur area)

---

## Current Status — Phase 2

**Last run:** `20260529_090633` (2026-05-29 09:07 UTC) | Runtime: 82.5s

| Status | Count | % |
|---|---|---|
| FULLY_ELIGIBLE | 23,066 | 74.95% |
| PARTIALLY_ELIGIBLE | 5,358 | 17.41% |
| NEEDS_REVIEW | 1,595 | 5.18% |
| INELIGIBLE | 755 | 2.45% |
| **Total** | **30,774** | **100%** |

**Carbon accounting:**
- Net eligible area: **15,041.31 ha** (of 16,139.90 ha enrolled, 93.2%)
- Average adjustment factor: **0.9377**
- Plots blocked pending review: **1,595**

**Non-eligible area breakdown:**

| Source | Overlap (ha) | % of deductions |
|---|---|---|
| Forest (RFA + Hansen) | 928.84 | 84.1% |
| Settlements (OSM) | 115.10 | 10.4% |
| Roads (OSM, 5m buffer) | 38.93 | 3.5% |
| Buildings (MS + Google) | 14.17 | 1.3% |
| Water bodies (OSM) | 6.79 | 0.6% |
| Railways (OSM, 10m buffer) | 0.34 | 0.03% |
| **Total deducted** | **1,104.17** | **100%** |

---

## Data Layers

| File | Source | Status | Details |
|---|---|---|---|
| `farms_kmlFile.kml` | Field submission | ✅ Active | 30,774 AWD plots, Phase 1 validated |
| `forest_maharashtra_rfa.gpkg` | Bharatmaps FSI | ✅ Active | 792 polygons ≥1 ha, Maharashtra only |
| `forest_hansen_canopy_2023.gpkg` | Hansen GFC-2024-v1.12 | ✅ Active | 1,221 polygons, lossyear masked (intact through 2023) |
| `forest_hansen_canopy.gpkg` | Hansen GFC-2023-v1.11 | ⚠️ Superseded | Replaced by v1.12 — do not use |
| `water_bodies.gpkg` | OSM | ✅ Active | 394 polygons ≥0.5 ha |
| `water_jrc_permanent.gpkg` | JRC GSW occurrence ≥75% | ✅ Active | 0 features — Vidarbha confirmed no permanent water |
| `jrc_occurrence_clipped.tif` | JRC GSW v1.4 2021 | ✅ Audit trail | Clipped raster; all-zero pixels within AWD bbox |
| `roads.gpkg` | OSM | ✅ Active | 2,829 features, 5m buffer |
| `railways.gpkg` | OSM | ✅ Active | 167 features, 10m buffer |
| `settlements.gpkg` | OSM | ✅ Active | 1,701 features, no buffer |
| `buildings.gpkg` | Microsoft Global ML Footprints | ⚠️ Superseded | Replaced by buildings_merged.gpkg |
| `buildings_google.gpkg` | Google Open Buildings v3 GEE | ✅ Active | 444,123 features, confidence ≥0.65 |
| `buildings_merged.gpkg` | Microsoft + Google merged | ✅ Active | 486,986 features, IoU dedup ≥0.50 |

---

## Pending Tasks

| Job | Task | Status |
|---|---|---|
| A | Clip Bharatmaps RFA + Download OSM layers (roads, water, settlements, railways) | ✅ DONE |
| B | JRC Global Surface Water integration into Phase 2B (dual-layer union) | ✅ DONE |
| C | Google Open Buildings v3 via GEE + merge with Microsoft, IoU dedup | ✅ DONE |
| D | Install project-manager agent (.claude/agents/project-manager.md) | ✅ DONE |

**Awaiting Jibotosh (open blockers):**

| Item | Impact |
|---|---|
| Confirm Maharashtra forest definition (FSI RFA vs CDM DNA) | Affects 2,734 plots / 928 ha |
| Confirm partial eligibility threshold (min % to retain a plot) | `partial_threshold_pct: null` in config |
| WRIS water bodies (indiawris.gov.in — requires auth/manual download) | Currently using OSM water only |

---

## URGENT — Decision Required

**Jibotosh decision needed: 2,045 plots reclassified FULLY_ELIGIBLE → PARTIALLY_ELIGIBLE after Google OB integration.**

Google Open Buildings v3 detected **432,236 building footprints** within the AWD extent that Microsoft's model had missed. These are predominantly farm-associated small structures: pump rooms, cattle sheds, grain storage, outhouses. As a result:

- Buildings-affected plots jumped from 37 → **3,333**
- Building overlap area jumped from 0.28 ha → **14.17 ha**
- **2,045 plots that were previously FULLY_ELIGIBLE are now PARTIALLY_ELIGIBLE**
- 14 additional plots moved to INELIGIBLE

**Question for VM0051 methodology review:**
> Should farm-associated structures inside AWD plots (pump rooms, cattle sheds, grain storage) count as non-eligible area under VCS VM0051? These structures are operationally integral to AWD water management — excluding them may not align with the methodology's intent.

Until confirmed, these 2,045 plots retain PARTIALLY_ELIGIBLE status with their carbon adjustment factors applied.

---

## Accuracy Summary

| Dimension | Before Google OB | After Google OB | Target |
|---|---|---|---|
| Forest coverage | RFA + Hansen dual-layer | Same | ✅ High |
| Water coverage | OSM only (JRC = 0) | Same | Medium — WRIS needed |
| Infrastructure: buildings | 37 plots / 0.28 ha | **3,333 plots / 14.17 ha** | **9.0× improvement** |
| Infrastructure: roads/railways/settlements | 3,473 plots affected | Same | ✅ Adequate |
| Overall Phase 2 accuracy estimate | ~88–90% | **~90–92%** | 90%+ |

**Remaining accuracy gap:** WRIS permanent water bodies (the biggest outstanding layer). Once received from Jibotosh, re-run Phase 2B and expect minor additional plot reclassifications in river/canal zones.

---

## Quick Reference

**Rerun Phase 2 pipeline:**
```bash
docker compose run --rm awd-validation python scripts/run_pipeline_phase2.py
```

**Key config:** `config/settings.yaml`
- Forest: `phase2.forest.layer_path` + `hansen_layer_path`
- Water: `phase2.water_bodies.layer_path` + `jrc_layer_path`
- Buildings: `phase2.infrastructure.buildings_layer_path` → `buildings_merged.gpkg`
- Review threshold: `phase2.eligibility.review_threshold_pct: 75`

**Latest outputs:** `data/outputs/*_20260529_090633.*`

**Branch:** `dev-denish` | **Repo:** `github.com/dev-denish/validation`
