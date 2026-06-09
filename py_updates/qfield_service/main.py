"""
main.py
-------
The orchestrator. Runs the polling loop:

    every POLL_INTERVAL seconds:
        1. ask QFieldCloud "is there new activity?"
        2. if yes -> repackage, download (only changes), load to PostGIS, copy photos

Run this file. It uses config.py for settings and client.py to talk to QFieldCloud.

Requirements:
    pip install qfieldcloud-sdk
    ogr2ogr available on PATH (part of GDAL / shipped with QGIS / OSGeo4W)
"""

import time
import shutil
import sqlite3
import logging
import subprocess
from pathlib import Path

import config
from client import QfcSyncClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("qfc_sync.main")


# --------------------------------------------------------------------------- #
# Detect which layer inside a GeoPackage holds the photo records
# --------------------------------------------------------------------------- #
def find_photo_layer(gpkg: Path) -> str | None:
    """
    QField exports GeoPackages with hash-based table names that change every
    sync cycle.  Scan the GeoPackage for the ONE table that has a 'file_path'
    or 'photo_name' column — that is the building_photos layer regardless of
    its internal hash name.

    Returns the table name (the source layer for ogr2ogr), or None if not found.
    """
    try:
        con = sqlite3.connect(str(gpkg))
        cur = con.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [r[0] for r in cur.fetchall()]
        for table in tables:
            try:
                cur.execute(f'PRAGMA table_info("{table}")')
                cols = {r[1] for r in cur.fetchall()}
                if "file_path" in cols or "photo_name" in cols:
                    con.close()
                    return table
            except Exception:
                continue
        con.close()
    except Exception as exc:
        log.warning("Could not inspect %s for photo layer: %s", gpkg.name, exc)
    return None


# --------------------------------------------------------------------------- #
# Load GeoPackages into PostGIS
# --------------------------------------------------------------------------- #
def load_geopackages_to_postgis() -> None:
    gpkgs = list(config.LOCAL_DIR.rglob("*.gpkg"))
    if not gpkgs:
        log.warning("No GeoPackages found in %s", config.LOCAL_DIR)
        return

    for gpkg in gpkgs:
        # Detect the hash-named layer that contains photo records, so we can:
        #   a) load ONLY that layer (skip the other auxiliary hash tables), and
        #   b) rename it to "building_photos" in PostGIS via -nln.
        source_layer = find_photo_layer(gpkg)
        if source_layer is None:
            log.warning("No photo layer found in %s — skipping.", gpkg.name)
            continue

        log.info(
            "Loading %s (layer: %s) into PostGIS as 'building_photos' (schema %s) ...",
            gpkg.name, source_layer[:16] + "...", config.PG_SCHEMA,
        )
        cmd = [
            config.OGR2OGR,
            "-f", "PostgreSQL",
            f"PG:{config.PG_CONN}",
            str(gpkg),
            source_layer,                 # load ONLY the photo layer (not all layers)
            "-nln", "building_photos",    # always name it 'building_photos' in PostGIS
            "-lco", "OVERWRITE=YES",      # idempotent: replaces the table each cycle
            "-lco", f"SCHEMA={config.PG_SCHEMA}",
            "-lco", "GEOMETRY_NAME=geom",
            "-lco", "FID=id",
            "-nlt", "PROMOTE_TO_MULTI",   # avoids mixed-geometry-type errors
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            log.error("ogr2ogr failed on %s:\n%s", gpkg.name, result.stderr.strip())
        else:
            log.info("OK: building_photos loaded (%s).", gpkg.name)


# --------------------------------------------------------------------------- #
# Query the GeoPackage for photos actually referenced by features
# --------------------------------------------------------------------------- #
def get_referenced_photos() -> set[str]:
    """
    Read all GeoPackages in LOCAL_DIR and return the SET of photo base-names
    (e.g. "building-photos_20260517034209430.jpg") that are referenced in any
    table that has a 'file_path' or 'photo_name' column.

    This is the ground truth: if a photo feature was deleted in QField, it
    will no longer appear here — even if the .jpg file still exists in
    QFieldCloud's project files.
    """
    referenced: set[str] = set()
    gpkgs = list(config.LOCAL_DIR.rglob("*.gpkg"))

    for gpkg in gpkgs:
        try:
            con = sqlite3.connect(str(gpkg))
            cur = con.cursor()

            # Find every table that has a 'file_path' or 'photo_name' column.
            cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = [row[0] for row in cur.fetchall()]

            for table in tables:
                try:
                    cur.execute(f'PRAGMA table_info("{table}")')
                    columns = {row[1] for row in cur.fetchall()}
                except Exception:
                    continue

                # Prefer 'file_path' (full relative path like "DCIM/foo.jpg"),
                # fall back to 'photo_name' (bare filename).
                if "file_path" in columns:
                    cur.execute(
                        f'SELECT file_path FROM "{table}" WHERE file_path IS NOT NULL AND file_path != ""'
                    )
                    for (val,) in cur.fetchall():
                        referenced.add(Path(val).name)
                elif "photo_name" in columns:
                    cur.execute(
                        f'SELECT photo_name FROM "{table}" WHERE photo_name IS NOT NULL AND photo_name != ""'
                    )
                    for (val,) in cur.fetchall():
                        referenced.add(Path(val).name)

            con.close()
        except Exception as exc:
            log.warning("Could not read %s for photo references: %s", gpkg.name, exc)

    log.info(
        "GeoPackage references %d photo(s) across all layers.", len(referenced)
    )
    return referenced


# --------------------------------------------------------------------------- #
# Mirror photos to the web-served folder (copy new, delete orphans)
# --------------------------------------------------------------------------- #
def sync_photos(remote_photo_names: list[str], referenced: set[str]) -> None:
    """
    Make PHOTO_WEB_DIR an exact mirror of photos that are BOTH:
      - present in the cloud (remote_photo_names), AND
      - referenced by a feature in the GeoPackage (referenced).

    Photos that exist in the cloud but are no longer attached to any feature
    (i.e. the user deleted the feature/photo in QField) are treated as orphans
    and removed from PHOTO_WEB_DIR.

    remote_photo_names : list of remote file paths, e.g. "DCIM/foo.jpg"
    referenced         : set of base-names that appear in GeoPackage features
    """
    config.PHOTO_WEB_DIR.mkdir(parents=True, exist_ok=True)

    # Only keep photos that exist in the cloud AND are still referenced.
    expected = {
        Path(name).name
        for name in remote_photo_names
        if Path(name).name in referenced
    }

    # 1. Copy new/changed photos from the download dir into the web folder.
    copied = 0
    for path in config.LOCAL_DIR.rglob("*"):
        if path.is_file() and path.suffix.lower() in config.IMAGE_EXTS:
            if path.name not in expected:
                continue  # orphan or deleted — skip
            dest = config.PHOTO_WEB_DIR / path.name
            if not dest.exists() or dest.stat().st_size != path.stat().st_size:
                shutil.copy2(path, dest)
                copied += 1

    # 2. Delete photos from the web folder that are no longer expected.
    removed = 0
    for dest in config.PHOTO_WEB_DIR.iterdir():
        if dest.is_file() and dest.suffix.lower() in config.IMAGE_EXTS:
            if dest.name not in expected:
                dest.unlink()
                log.info("Removed orphan photo from web folder: %s", dest.name)
                removed += 1

    if copied or removed:
        log.info("Photos mirrored: %d copied, %d removed.", copied, removed)
    else:
        log.info("Photos already up to date.")


# --------------------------------------------------------------------------- #
# Main polling loop
# --------------------------------------------------------------------------- #
def main() -> None:
    if not config.PROJECT_ID:
        raise SystemExit("ERROR: set QFC_PROJECT_ID.")

    qfc = QfcSyncClient()
    log.info(
        "Service started. Polling every %ss. Project: %s",
        config.POLL_INTERVAL,
        config.PROJECT_ID,
    )

    last_fingerprint = None
    first_run = True

    while True:
        try:
            current_fingerprint = qfc.remote_fingerprint()

            if first_run or (current_fingerprint != last_fingerprint):
                if first_run:
                    log.info("Initial synchronization ...")
                else:
                    log.info("Change detected in project files.")

                if qfc.ensure_fresh_package():
                    # Geometry comes from the package (small, fast).
                    qfc.download_geometry_from_package()
                    load_geopackages_to_postgis()

                    # Find which photos are actually referenced in the GeoPackage.
                    # This is the ground truth: deleted features won't appear here.
                    referenced = get_referenced_photos()

                    # Photos come from the direct project files (real ETag
                    # skipping, so unchanged photos are NOT re-downloaded).
                    remote_photos = qfc.download_photos()

                    # Mirror only photos that are referenced by a feature.
                    sync_photos(remote_photos, referenced)

                    # Remember the state we just synced, so the next loop only
                    # reacts to genuinely new changes.
                    last_fingerprint = current_fingerprint
                    log.info("Sync cycle complete.\n")

                first_run = False
            else:
                log.debug("No changes.")

        except KeyboardInterrupt:
            log.info("Stopped by user.")
            break
        except Exception as exc:
            # Never let a single error kill the service; log and keep going.
            log.error("Error in cycle: %s", exc)

        time.sleep(config.POLL_INTERVAL)


if __name__ == "__main__":
    main()
