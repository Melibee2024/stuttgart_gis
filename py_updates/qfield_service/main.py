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
import logging
import subprocess

import config
from client import QfcSyncClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("qfc_sync.main")


# --------------------------------------------------------------------------- #
# Load GeoPackages into PostGIS
# --------------------------------------------------------------------------- #
def load_geopackages_to_postgis() -> None:
    gpkgs = list(config.LOCAL_DIR.rglob("*.gpkg"))
    if not gpkgs:
        log.warning("No GeoPackages found in %s", config.LOCAL_DIR)
        return

    for gpkg in gpkgs:
        log.info("Loading %s into PostGIS (schema %s) ...", gpkg.name, config.PG_SCHEMA)
        cmd = [
            config.OGR2OGR,
            "-f", "PostgreSQL",
            f"PG:{config.PG_CONN}",
            str(gpkg),
            "-overwrite",                 # idempotent: replaces the table each cycle
            "-lco", f"SCHEMA={config.PG_SCHEMA}",
            "-lco", "GEOMETRY_NAME=geom",
            "-lco", "FID=id",
            "-nlt", "PROMOTE_TO_MULTI",   # avoids mixed-geometry-type errors
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            log.error("ogr2ogr failed on %s:\n%s", gpkg.name, result.stderr.strip())
        else:
            log.info("OK: %s loaded.", gpkg.name)


# --------------------------------------------------------------------------- #
# Copy photos to the web-served folder
# --------------------------------------------------------------------------- #
def sync_photos() -> None:
    config.PHOTO_WEB_DIR.mkdir(parents=True, exist_ok=True)
    count = 0
    for path in config.LOCAL_DIR.rglob("*"):
        if path.is_file() and path.suffix.lower() in config.IMAGE_EXTS:
            dest = config.PHOTO_WEB_DIR / path.name
            # Copy only if missing or the size changed (fast and good enough).
            if not dest.exists() or dest.stat().st_size != path.stat().st_size:
                shutil.copy2(path, dest)
                count += 1
    if count:
        log.info("%d photo(s) copied to %s", count, config.PHOTO_WEB_DIR)


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
                    qfc.download_package()
                    load_geopackages_to_postgis()
                    sync_photos()
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