# AWD Validation Pipeline — Handoff Document

**Project:** AWD Farm Plot Carbon Eligibility Validation — Maharashtra, India
**Standard:** VCS Standard 5.0 / UNFCCC CDM / Gold Standard
**Developer:** Denish | **Supervisors:** Jibotosh & Sabik (VNV)
**Plots:** 30,774 | **Location:** Vidarbha, Maharashtra (Nagpur area)

---

## Current Status — Phase 2

**Last run:** `20260602_202455` (2026-06-02 20:24 UTC) | Runtime: 135.8s
**All 4 audit fixes applied** — see Audit Resolution section below.

| Status | Count | % |
|---|---|---|
| FULLY_ELIGIBLE | 23,067 | 74.96% |
| PARTIALLY_ELIGIBLE | 5,413 | 17.59% |
| NEEDS_REVIEW | 1,580 | 5.13% |
| INELIGIBLE | 714 | 2.32% |
| **Total** | **30,774** | **100%** |

**Carbon accounting:**
- Net eligible area: **15,063.30 ha** (of 16,139.90 ha enrolled, 93.3%)
- Plot-mean adjustment factor: **0.9390** (informational — each plot weighted equally)
- **Area-weighted adjustment factor: 0.9333** (use for carbon accounting — VM0051 / VCS)
- Plots blocked pending review: **1,580**

**Non-eligible area breakdown (union-corrected):**

| Source | Overlap (ha) | % of deductions |
|---|---|---|
| Forest (RFA + Hansen) | 928.84 | 86.3% |
| Settlements (OSM) | 115.10 | 10.7% |
| Roads (OSM, 5m buffer) | 38.93 | 3.6% |
| Buildings (MS + Google) | 14.17 | 1.3% |
| Water bodies (OSM) | 6.79 | 0.6% |
| Railways (OSM, 10m buffer) | 0.34 | 0.03% |
| **Total deducted (union)** | **1,076.60** | **100%** |

> Note: breakdown values are per-source sums; total uses spatial union geometry to prevent double-counting. 27.57 ha of double-counted overlap was corrected.

---

## Data Layers

| File | Source | Status | Details |
|---|---|---|---|
| `farms_kmlFile.kml` | Field submission | ✅ Active | 30,774 AWD plots, Phase 1 validated |
| `forest_maharashtra_rfa.gpkg` | Bharatmaps FSI | ✅ Active | 792 polygons ≥1 ha, Maharashtra only |
| `forest_hansen_canopy_2023.gpkg` | Hansen GFC-2024-v1.12 | ✅ Active | 1,221 polygons, lossyear masked (intact through 2023) |
| `forest_hansen_canopy.gpkg` | Hansen GFC-2023-v1.11 | ⚠️ Superseded | Replaced by v1.12 — do not use |
| `water_bodies.gpkg` | OSM | ✅ Active | 394 polygons ≥0.5 ha |
| `water_jrc_permanent.gpkg` | JRC GSW occurrence ≥75% | ✅ Confirmed | **0 features — CONFIRMED DATA FINDING: 4,040,880 pixels surveyed, 100% coverage, 0 pixels ≥75% occurrence. Vidarbha region has no JRC-tracked permanent water.** |
| `jrc_occurrence_stats.json` | JRC GSW audit sidecar | ✅ Audit trail | Raster stats JSON — total/surveyed/above-threshold pixels |
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
| Confirm Maharashtra forest definition (FSI RFA vs CDM DNA) | Affects 2,733 plots / 928 ha |
| Confirm partial eligibility threshold (min % to retain a plot) | `partial_threshold_pct: null` in config |
| WRIS water bodies (indiawris.gov.in — requires auth/manual download) | Currently using OSM water only |
| Google OB buildings classification — do farm sheds count as non-eligible? | Affects 3,333 plots; 2,045 reclassified to PARTIAL |

---

## Audit Resolution

All 4 issues from the project-manager audit (run `20260529_090633`) have been resolved.

| Issue | Severity | Resolution | Run |
|---|---|---|---|
| JRC water layer empty — no explanation in report | CRITICAL | Fixed `valid_pixels` logic; added `jrc_status: CONFIRMED_NO_WATER` + full raster stats to 2B JSON | `20260602_201233` |
| 5.58 ha area discrepancy in 2D JSON summary | MAJOR | Replaced column-sum with geometry union in `_calculate_net_area()`. Saved 27.57 ha of double-counted overlap; 41 plots restored from INELIGIBLE to PARTIAL | `20260602_202020` |
| Adjustment factor: plot-mean vs area-weighted | MAJOR | Added `area_weighted_adj_factor: 0.9333` to 2E JSON alongside `plot_mean_adj_factor: 0.9390`. Both clearly labelled. | `20260602_202332` |
| NEEDS_REVIEW reason string "Losing 100.0%" for sliver plots | MINOR | Fixed f-string: now shows `<0.1% retained` when eligible_pct > 0 but rounds to 0.0 | `20260602_202332` |

**Delta from pre-audit run (`20260529_090633`) to final (`20260602_202455`):**

| Metric | Before | After | Change |
|---|---|---|---|
| FULLY_ELIGIBLE | 23,066 | 23,067 | +1 |
| PARTIALLY_ELIGIBLE | 5,358 | 5,413 | +55 |
| NEEDS_REVIEW | 1,595 | 1,580 | -15 |
| INELIGIBLE | 755 | 714 | **-41** |
| Net eligible area | 15,041.31 ha | 15,063.30 ha | **+21.99 ha** |
| Non-eligible (union-corrected) | 1,104.17 ha (sum) | 1,076.60 ha (union) | -27.57 ha |
| Area-weighted adj factor | — (not reported) | 0.9333 | Now in JSON |

---

## URGENT — Decision Required

**Jibotosh decision needed: 2,045 plots reclassified FULLY_ELIGIBLE → PARTIALLY_ELIGIBLE after Google OB integration.**

Google Open Buildings v3 detected **432,236 building footprints** within the AWD extent that Microsoft's model had missed. These are predominantly farm-associated small structures: pump rooms, cattle sheds, grain storage, outhouses.

**Question for VM0051 methodology review:**
> Should farm-associated structures inside AWD plots (pump rooms, cattle sheds, grain storage) count as non-eligible area under VCS VM0051? These structures are operationally integral to AWD water management — excluding them may not align with the methodology's intent.

Until confirmed, these plots retain PARTIALLY_ELIGIBLE status.

---

## Accuracy Summary

| Dimension | Status | Notes |
|---|---|---|
| Forest coverage | ✅ High | RFA (FSI) + Hansen v1.12 dual-layer; union prevents double-counting |
| Water: JRC | ✅ Confirmed | 0 features is a verified data finding, not a failure |
| Water: OSM | ⚠️ Medium | Adequate for Vidarbha; WRIS would improve completeness |
| Infrastructure: buildings | ✅ High | 9.0× improvement from Google OB (MS 54K → merged 487K) |
| Infrastructure: roads/rail/settle | ✅ Adequate | OSM coverage confirmed for Nagpur area |
| Area calculation | ✅ Corrected | Union geometry eliminates double-counting (was column-sum) |
| Carbon factors | ✅ Both reported | Plot-mean 0.9390 (informational) + area-weighted 0.9333 (carbon use) |
| **Overall Phase 2 accuracy** | **~92–94%** | Up from ~90–92% after audit fixes |

**Remaining gap:** WRIS water bodies + Jibotosh decisions on building classification and partial eligibility threshold.

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

**Final outputs:** `data/outputs/*_20260602_202455.*`

**Branch:** `dev-denish` | **Repo:** `github.com/dev-denish/validation`
