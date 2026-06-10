# QField → PostGIS → Cesium Photo Sync — Setup Summary

This document summarizes the work done to integrate the QField↔PostGIS photo
synchronization service into the `master` branch of this project, including
configuration, dependencies, and how to run it.

## 1. Background

The sync scripts (`client.py`, `config.py`, `main.py`, `debug_jobs.py`) located in
`py_updates/qfield_service/` were originally built and tested on a different
machine against a test database. This session covered integrating them into
this project's real environment (database `hft_db`) and configuring a proper
storage location for synced photos.

## 2. What the service does

A continuous background service (`main.py`) that:
1. Polls QFieldCloud for changes (every `POLL_INTERVAL` seconds, default 10s)
2. Requests a data "package" from QFieldCloud and waits for it to be ready
   (timeout controlled by `PACKAGE_TIMEOUT`, default 180s)
3. Downloads the resulting GeoPackage(s)
4. Loads the GeoPackage data into PostGIS via `ogr2ogr`
5. Mirrors/syncs the referenced photos into a local web-accessible folder

End-to-end, one full sync cycle (detect change → package → download → load
into PostGIS → copy photos) takes roughly ~30 seconds. This is expected: most
of that time is spent waiting on QFieldCloud's server-side packaging step
(hence the generous 180s timeout in the config) plus network transfer —
not a sign of misconfiguration.

## 3. Configuration (`.env`)

A new `.env` file was created at:
```
py_updates/qfield_service/.env
```

It is **not committed to git** (already covered by the `.env` rule in
`.gitignore`) since it contains credentials. Its structure:

```
QFC_URL=https://app.qfield.cloud/api/v1/

QFC_USER=<QFieldCloud account email>
QFC_PASS=<QFieldCloud account password>
QFC_TOKEN=

QFC_PROJECT_ID=<QFieldCloud project UUID>

PG_CONN=dbname=hft_db host=localhost port=5432 user=postgres password=<db password>

PG_SCHEMA=qfield_data

OGR2OGR=C:\OSGeo4W\bin\ogr2ogr.exe

POLL_INTERVAL=10
PACKAGE_TIMEOUT=180
```

### Key values discovered/decided during setup

| Setting | Value | Notes |
|---|---|---|
| `PG_CONN` (DB connection) | `dbname=hft_db host=localhost port=5432 user=postgres` | Found in `cesium_web/stuttgart-backend/.env` and `py_updates/database_configs/local_config_database.py` — this is the project's real database, not the test DB used originally |
| `PG_SCHEMA` | `qfield_data` | **Important fix**: the script's default value (`qfield`) does not exist in the database. The correct, existing schema is `qfield_data`. Using the wrong schema would cause the sync to fail or create an unwanted empty `qfield` schema |
| `OGR2OGR` | `C:\OSGeo4W\bin\ogr2ogr.exe` | The original path (`C:\Program Files\QGIS 3.42.0\bin\ogr2ogr.exe`) was specific to the other machine and doesn't exist here. Two local options were found (OSGeo4W and the one bundled with PostgreSQL 17); **OSGeo4W was chosen** because it's a full GDAL/OGR install with complete driver support (GeoPackage, etc.), more comparable to a QGIS-bundled GDAL than the minimal PostgreSQL-bundled build |

## 4. Photo storage location (CRITICAL)

Synced photos must land in the exact folder the Cesium backend serves `/media`
from, otherwise they will never appear in the viewer:

```
stuttgart_gis/cesium_web/stuttgart-backend/media/
```

This is enforced by `PHOTO_WEB_DIR` in the `.env`:

```
PHOTO_WEB_DIR=../../cesium_web/stuttgart-backend/media
```

### Why this matters — the full chain

1. The DB stores `file_path` like `DCIM/building-photos_<ts>.jpg`.
2. The frontend (`stuttgart-digital-twin/src/main.js`) builds the image URL
   from just the **basename**:
   `http://localhost:5000/media/building-photos_<ts>.jpg`.
3. The backend (`stuttgart-backend/server.mjs`) serves `/media` statically from
   its own `media/` folder: `express.static('media')`.
4. The sync service (`main.py` → `sync_photos`) copies each referenced photo,
   by **basename**, into `PHOTO_WEB_DIR`.

The filenames always match (both sides use the basename), so the ONLY thing
that has to be correct is that **`PHOTO_WEB_DIR` == the backend's `media/`
folder**. An earlier setup pointed `PHOTO_WEB_DIR` at a separate
`media/qfield_photos/` folder, which the backend never reads — that made newly
synced photos silently fail to show in Cesium. That folder has been removed.

### Launch-location robustness

`config.py` now resolves all relative paths against the location of
`config.py` itself (the `qfield_service/` folder), **not** the current working
directory:

```python
BASE_DIR = Path(__file__).resolve().parent
# relative PHOTO_WEB_DIR / QFC_LOCAL_DIR are resolved against BASE_DIR
```

This means the service behaves identically whether launched from PyCharm, a
terminal in another folder, or a scheduled task.

## 5. Python environment & dependencies

A dedicated virtual environment was created (following the same `.venv/`
convention already used elsewhere in the project, and already covered by
`.gitignore`):

```
py_updates/qfield_service/.venv/
```

Installed packages:
- `python-dotenv`
- `requests`
- `qfieldcloud-sdk` (the official QFieldCloud SDK — required by `client.py`,
  was missing initially and had to be installed separately)

### How to run the service

```powershell
cd C:\3dcitydb-4.4.2\stuttgart_gis\py_updates\qfield_service
.\.venv\Scripts\activate
python main.py
```

Or without activating:
```powershell
.\.venv\Scripts\python.exe main.py
```

The service runs continuously (it's a polling loop, not a one-shot script).
To stop it: `Ctrl+C` in the terminal, or the stop button in PyCharm's Run panel.
You can run other scripts/terminals in PyCharm in parallel without stopping it.

## 6. Verification

After running the service, data was confirmed to be correctly synced:

- **Database**: `qfield_data.building_photos` table populated with photo
  records (`photo_id`, `alkis_id`, `file_path`, `direction`, `captured_at`,
  `geom_camera`, `photo_name`, etc.). A few auxiliary tables with hashed
  names were also created automatically by `ogr2ogr` — this is normal when
  loading GeoPackages exported from QField/QGIS with internal layers/relations.
- **Filesystem**: matching photo files appeared in
  `media/qfield_photos/` (e.g. `building-photos_20260518120100426.jpg`),
  with names corresponding to the `file_path`/`photo_name` values in the DB.

## 7. Notes on `.gitignore` (master branch)

While working on this, the `.gitignore` on `master` was also updated to
include rules that already existed on `genaubranch` (Claude Code local
settings, refined `.env.*` handling, `dist/`, generated 3D tiles output).
This was committed locally (commit `9e9d90c`) but **not pushed** yet, since
`origin/master` has diverged (it has newer commits this local copy doesn't
have yet). This should be reconciled with the team before pushing, to avoid
overwriting anyone else's work.

## 8. Backups made during this session

Before making any changes, full backups (excluding `node_modules`, which is
regenerable via `npm install`) of the following folders — as they existed on
`genaubranch` — were made to:
```
C:\backups\stuttgart_gis_backup\cesium_web\
C:\backups\stuttgart_gis_backup\IFCGeoreferencing\
```

All of `genaubranch`'s pending work (modified + untracked files) was also
committed locally (commit `61d1d9f`) before switching branches, so nothing
was lost.

## 9. Database credentials — no more hardcoded passwords

The DB password used to be hardcoded in several committed files. It has been
removed from all of them; credentials are now supplied per-machine and never
committed:

- **Python scripts** (`georeference_v4_fit.py`, `ifc_to_postgis.py`, the sync
  service) read DB settings from a local, git-ignored `.env`.
- **Node backend** (`server.mjs`) reads them from its own git-ignored `.env`.
- **QGIS project** (`qgis_templates/qfield_survey.qgz`) references a PostgreSQL
  *service* (`service='hft_db'`) instead of an inline password.

### Each machine needs a PostgreSQL service file

For the QGIS/QField project to connect, every machine that opens it needs an
`[hft_db]` entry in its PostgreSQL service file:

- **Windows:** `%APPDATA%\postgresql\.pg_service.conf`
- **Linux/macOS:** `~/.pg_service.conf`

Contents (adjust to your local DB):

```
[hft_db]
host=localhost
port=5432
dbname=hft_db
user=postgres
password=YOUR_LOCAL_PASSWORD
```

This file lives **outside** the repo, so the password is never committed.
After creating it, verify with:

```
psql "service=hft_db" -c "SELECT 1;"
```

> Note: the repo is private and the password was **not rotated** — this change
> only stops it from being committed going forward. If the repo ever becomes
> public, or wider access is granted, rotate the password and update every
> machine's `.env` and `.pg_service.conf`.
