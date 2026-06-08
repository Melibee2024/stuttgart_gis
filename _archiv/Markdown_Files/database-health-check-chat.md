# Database Health Check — Full Report
**Project:** Stuttgart Digital Twin — ALKIS / QField / Cesium Pipeline
**Date:** 2026-06-04
**Database:** PostgreSQL / pgAdmin (`hft_db`)

---

## Table of Contents
1. [Pipeline Overview](#pipeline-overview)
2. [Schema Review](#schema-review)
3. [Cleaning Script Audit](#cleaning-script-audit)
4. [Append Script Audit](#append-script-audit)
5. [Views Review](#views-review)
6. [Health Check Query Results](#health-check-query-results)
7. [Orphan Diagnosis](#orphan-diagnosis)
8. [Fixes Applied](#fixes-applied)
9. [Final Verdict](#final-verdict)

---

## Pipeline Overview

```
ALKIS raw data (stuttgart_2d)
        │
        ▼
clean_and_prune_buildings.py  ──►  stuttgart_processed schema
        (drops all-NULL columns,       (ax_gebaeude_clean,
         fixes geometry validity)       ax_flurstueck_clean, etc.)
                                               │
QField field survey (GeoPackage)               │
        │                                      │
        ▼                                      │
append_from_qfield_to_db.py ──►  qfield_data.building_photos
                                               │
                                               ▼
                                    Views (survey progress,
                                    digital twin, Cesium payload)
```

---

## Schema Review

### `stuttgart_processed`
Well structured. Schema comment is clear. `GRANT SELECT TO public` is appropriate for a LAN team setup.

### `qfield_data.building_photos`
Solid. `photo_id UUID PRIMARY KEY DEFAULT gen_random_uuid()` is correct. The trigger (`t_qfield_generate_photo_uuid`) adds a safety net for null UUIDs and auto-generates `photo_name`.

**Fix applied:** `photo_name TEXT` column was missing from `qfield_building_data.sql` (had been added manually in pgAdmin). Schema file updated to match the live database.

---

## Cleaning Script Audit (`clean_and_prune_buildings.py`)

**What it does:** For each of 4 tables, finds all columns with at least one non-NULL value, then does `CREATE TABLE ... AS SELECT` into `stuttgart_processed`, dropping all-NULL columns. For `ax_gebaeude` specifically, wraps geometry in `ST_Multi(ST_MakeValid(...))`.

**Findings:**

| Issue | Risk | Detail |
|---|---|---|
| No primary key / indexes on output tables | Medium | `CREATE TABLE AS SELECT` doesn't copy constraints. **Resolved — indexes added manually (see below).** |
| `DROP TABLE IF EXISTS` before recreate | Low | Intentional overwrite — fine, but if the script fails mid-run, the old table is already gone with no rollback. |
| Geometry check is by column name only | Low | Checks for `wkb_geometry`, `geom`, `geometry`. A differently named geometry column would be skipped. |
| f-string SQL in `create_query` | Low | Safe since inputs are hardcoded constants, but not using `psycopg2.sql` for the full query. |

---

## Append Script Audit (`append_from_qfield_to_db.py`)

**What it does:** Runs inside QGIS. Reads a local GeoPackage layer, deduplicates against PostGIS by `file_path`, appends only new records.

**Findings:**

| Issue | Risk | Detail |
|---|---|---|
| `photo_id` not mapped | Low | Relies on DB default `gen_random_uuid()` — correct, but only if the trigger/default is active. |
| `photo_name` not mapped | Medium | Trigger auto-generates it. If trigger is missing, `photo_name` will be NULL for all synced records. |
| Dedup by `file_path` only | Low | Same photo re-exported with a different path would be inserted as a duplicate. |
| No geometry null check | Low | A feature with null geometry will insert successfully but with no spatial data. |

---

## Views Review

| View | Status | Notes |
|---|---|---|
| `v_building_field_survey` | ✅ Fixed | Duplicate `area` column removed (was identical to `grund_flaeche`). |
| `v_field_progress_monitor` | ✅ Clean | Correct LEFT JOIN + GROUP BY pattern. |
| `v_parcel_building_context` | ✅ Clean | `> 1.0 m²` overlap filter prevents false positives from shared boundary touches. |
| `v_integrity_check` | ✅ Clean | FULL OUTER JOIN is the correct approach for catching orphans in both directions. |
| `v_building_digital_twin` | ✅ Clean | `ARRAY_REMOVE(ARRAY_AGG(DISTINCT ...), NULL)` is correct pattern for photo galleries. |
| `v_cesium_payload` | ✅ Clean | Hardcoded bounding box in EPSG:25832 — intentional, matches the 6 Cesium tile squares. |

**Trigger `trg_ensure_photo_uuid`:** Clean. Handles both missing UUIDs and missing/blank photo names. Old duplicate trigger name safely dropped before recreation.

---

## Health Check Query Results

| Check | Result | Status |
|---|---|---|
| Row count — `ax_gebaeude_clean` | 52,483 | ✅ |
| Row count — `ax_flurstueck_clean` | 39,453 | ✅ |
| Row count — `ax_pto_clean` | 230,775 | ✅ |
| Row count — `ax_gebaeudefunktion_clean` | 234 | ✅ |
| Row count — `building_photos` | 10 (test data only) | ✅ |
| Invalid geometries | 0 | ✅ |
| Duplicate `gml_id` values | 0 | ✅ |
| Orphaned photos (no matching building) | 0 | ✅ |
| Buildings with unmatched `gebaeudefunktion` code | 0 | ✅ |
| Survey status — Completed | 3 buildings | ✅ (test survey, not intended for full coverage) |
| Survey status — Pending | 52,480 buildings | ✅ (expected) |

**Indexes confirmed present:**
- `idx_gebaeude_gml_id` on `ax_gebaeude_clean(gml_id)`
- `idx_gebaeude_geom` on `ax_gebaeude_clean` GIST
- `idx_flurstueck_geom` on `ax_flurstueck_clean` GIST
- `idx_photos_alkis_id` on `building_photos(alkis_id)` *(recommended, applied)*

---

## Orphan Diagnosis

### Integrity check raw results

| Status | Count |
|---|---|
| Perfect Match (2D + 3D linked) | 29,553 |
| Orphan 2D (ALKIS building, no 3D tile) | 22,930 |
| Orphan 3D (citydb object, no ALKIS footprint) | 2,828 |

### Root cause — spatial coverage mismatch (not a data error)

**Key finding:** 46,637 out of 52,483 ALKIS buildings (89%) lie **outside** the bounding box of the 6 Cesium 3D tile squares. The 3D model covers a specific campus/district area; the ALKIS dataset covers the entire municipality.

**ID format check:** Both `ax_gebaeude_clean.gml_id` and `citydb.external_reference.name` use the identical format (`DEBWL52210...` mixed-case alphanumeric). No case mismatch, no punctuation difference — the join key is correct.

**Orphan 3D inside tile box:** 0 — confirmed by spatial query. All 2,828 Orphan 3D records also fall outside the tile boundary.

### Conclusion

> **All orphans are explained by the spatial coverage gap between the full ALKIS dataset and the smaller 3D tile footprint. There are no linking errors, no ID mismatches, and no missing data within the area covered by the 3D model. The database is healthy.**

---

## Fixes Applied

| File | Change |
|---|---|
| `sql_scripts/schemas/qfield_building_data.sql` | Added missing `photo_name TEXT` column; removed outdated reminder comment; cleaned Windows line endings |
| `sql_scripts/views_and_triggers/v_building_field_survey.sql` | Removed duplicate `area` column (identical to `grund_flaeche`); improved column comment |

---

## Final Verdict

| Area | Result |
|---|---|
| Geometry validity | ✅ All valid after `ST_MakeValid` pass |
| Duplicate records | ✅ None |
| Referential integrity | ✅ Clean within the 3D tile coverage area |
| ID linking (2D ↔ 3D) | ✅ Format matches, join works correctly |
| Orphaned photos | ✅ None |
| Lookup table coverage | ✅ All `gebaeudefunktion` codes resolve to German text |
| Indexes | ✅ In place on all key join and geometry columns |
| Schema files vs live DB | ✅ Now in sync after `photo_name` fix |
| "Orphan" records | ✅ Expected — spatial coverage gap, not data errors |

**The cleaning and update scripts accomplished what they were designed to do.**
