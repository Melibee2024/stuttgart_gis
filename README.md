# HFT Stuttgart Digital Twin

A proof-of-concept urban Digital Twin linking 3D CityGML building geometry, 2D ALKIS cadastral data, and live QField field observations, visualized in a browser-based CesiumJS viewer.

---

## How It Generally Works

Field data is collected in QField on mobile, synced via QFieldCloud, and written into PostGIS by a Python polling service (`qfield_service`). The Node.js backend queries the database and serves 3D tiles and building attributes to the CesiumJS frontend. Clicking a building in the viewer displays cadastral (ALKIS) and field survey data, including field photos, in a side panel.

---

## Project Structure

```
stuttgart_gis/
├── sql_scripts/
│   ├── schemas/               # Schema and table setup scripts
│   └── views_and_triggers/    # SQL views and triggers for data linking and QA
│
├── py_updates/
│   ├── database_configs/      # DB connection parameters
│   └── qfield_service/        # QFieldCloud sync service
│
├── qgis_templates/            # QGIS project files for desktop analysis and QField packaging
│
├── cesium_web/
│   ├── stuttgart-backend/     # Node.js Express API and static tile server
│   └── stuttgart-digital-twin/ # Vite/CesiumJS frontend
│
└── media/                     # Project media. NOTE: QField photos are mirrored into
                               # cesium_web/stuttgart-backend/media/ (served at /media),
                               # NOT here — see PHOTO_WEB_DIR in qfield_service/.env
```

---

## Database

Three schemas in `hft_db` (PostgreSQL/PostGIS):

| Schema | Source | Purpose |
|--------|--------|---------|
| `citydb` | 3DCityDB v4 + LGL-BW CityGML tiles | 3D building geometry |
| `stuttgart_2d` | norGIS ALKIS-Import (gid7) | 2D cadastral footprints |
| `qfield_data` | QField surveys via qfield_service | Field observations and photos |

### SQL Scripts (`sql_scripts/`)

These scripts were run to set up the database structure and verify data integrity:

**Schemas:**
- `qfield_building_data.sql` — creates the `qfield_data.building_photos` table and the UUID trigger (`t_qfield_generate_photo_uuid`). This table is queried live by the Cesium backend.
- `stuttgart_processed_schema.sql` — sets up the `stuttgart_processed` schema used as the base for the views below.

**Views (data linking and QA):**
- `v_building_digital_twin.sql` — main triple-schema join across ALKIS, citydb, and QField photos, used to verify the data links are correct.
- `v_cesium_payload.sql` — spatial subset of the above for the HFT campus area, designed as a prototype Cesium data source. The live backend uses `public.data_fusion_view` (a partner-created view) instead, but the logic is equivalent.
- `v_integrity_check.sql` — validates 2D/3D match status (Perfect Match / Orphan 2D / Orphan 3D). Used during setup to confirm all buildings linked correctly.
- `v_field_progress_monitor.sql` — tracks survey completion per building. Used to monitor field data collection progress.
- `v_building_field_survey.sql` — field survey data joined to buildings. Restricted
  to buildings that also exist in the citydb 3D model (via `citydb.external_reference`),
  so the QField survey layer and the Cesium `data_fusion_view` cover the **same**
  building set — surveyors can't attach photos to a building that isn't viewable in 3D.
- `v_parcel_building_context.sql` — ALKIS parcel context per building.

---

## Setup (New Machine)

### Prerequisites
- PostgreSQL 14+ with PostGIS and `hft_db` database restored
- Node.js 18+
- Python 3.10+
- QGIS (for `ogr2ogr`)
- pg2b3dm (for regenerating LoD2 3D tiles if needed)

### 1. Environment files
Copy the example files and fill in real credentials:
```
copy .env.example .env
copy cesium_web\stuttgart-backend\.env.example cesium_web\stuttgart-backend\.env
```
Make sure `PHOTO_WEB_DIR` in `py_updates/qfield_service/.env` points to the backend media folder:
```
PHOTO_WEB_DIR=../../cesium_web/stuttgart-backend/media
```

Database credentials are **not** committed to the repo. The Python scripts and
Node backend read them from their (git-ignored) `.env` files. The QGIS project
(`qgis_templates/qfield_survey.qgz`) connects via a PostgreSQL **service**, so
each machine also needs a service file with an `[hft_db]` entry:

- **Windows:** `%APPDATA%\postgresql\.pg_service.conf`
- **Linux/macOS:** `~/.pg_service.conf`

```
[hft_db]
host=localhost
port=5432
dbname=hft_db
user=postgres
password=YOUR_LOCAL_PASSWORD
```
Verify it works: `psql "service=hft_db" -c "SELECT 1;"`

### 2. Install dependencies
```powershell
cd cesium_web\stuttgart-backend && npm install
cd cesium_web\stuttgart-digital-twin && npm install
cd py_updates\qfield_service && pip install python-dotenv qfieldcloud-sdk
```

### 3. Create database views and helper objects
The selective `pg_dump` migration only carries the `stuttgart_2d`,
`stuttgart_processed`, `qfield_data`, and `citydb` schemas — anything in the
fresh `public` schema (notably the `public.data_fusion_view` the backend
queries) and the `nexus3d` base-tile schema must be (re)created from
`nexus3d_setup.sql`. Skipping this causes the backend to fail with
`relation "public.data_fusion_view" does not exist` and no building data/photos
in the viewer.
```powershell
psql "service=hft_db" -f cesium_web\stuttgart-backend\nexus3d_setup.sql
```

### 4. Regenerate LoD2 3D Tiles (if `tiles_citydb/` is missing)
The tile folder is gitignored — regenerate it from the database:
```powershell
pg2b3dm -h localhost -p 5432 -d hft_db -U postgres `
  --schemaname nexus3d --tablename citydb_base_tiles `
  --geometrycolumn wkb_geometry --output cesium_web\stuttgart-backend\public\tiles_citydb
```

---

## Running the System

Open three terminals:

**Terminal 1 — Backend**
```powershell
cd cesium_web\stuttgart-backend
npm start
```

**Terminal 2 — Frontend**
```powershell
cd cesium_web\stuttgart-digital-twin
npm run dev
```

**Terminal 3 — QField sync service**
```powershell
cd py_updates\qfield_service
python main.py
```

Open `http://localhost:5173` in the browser.

---

## Stopping the Servers

```powershell
# Backend (port 5000)
Get-NetTCPConnection -LocalPort 5000 | Select-Object -ExpandProperty OwningProcess | ForEach-Object { Stop-Process -Id $_ -Force }

# Frontend (port 5173)
Get-NetTCPConnection -LocalPort 5173 | Select-Object -ExpandProperty OwningProcess | ForEach-Object { Stop-Process -Id $_ -Force }

# QField sync service: Ctrl+C in its terminal
```

---

## External Tools Used

- **3DCityDB Importer/Exporter** — CityGML tile import into the `citydb` schema
- **norGIS ALKIS-Import (gid7)** — cadastral data import into `stuttgart_2d`
- **pg2b3dm** — generates LoD2 3D Tiles from PostGIS for Cesium
- **QFieldCloud SDK** — used by `qfield_service` to poll and download field packages
- **ogr2ogr** — used by `qfield_service` to write QField data into PostGIS
