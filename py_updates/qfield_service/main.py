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
# Mirror photos to the web-served folder (copy new, delete orphans)
# --------------------------------------------------------------------------- #
def sync_photos(remote_photo_names: list[str]) -> None:
    """
    Make PHOTO_WEB_DIR an exact mirror of the cloud photos.

    - Copies photos that are new or changed.
    - Deletes local photos that no longer exist in the cloud (so deleting a
      photo in QField removes it here too).

    remote_photo_names: the photo names currently in the cloud (from the
    client), e.g. "DCIM/building-photos_2026....jpg". We compare by the file's
    base name, since PHOTO_WEB_DIR is flat.
    """
    config.PHOTO_WEB_DIR.mkdir(parents=True, exist_ok=True)

    # Base names of the photos that SHOULD exist locally.
    expected = {Path(name).name for name in remote_photo_names}

    # 1. Copy new/changed photos from the download dir into the web folder,
    #    skipping any that were deleted from the cloud (not in expected).
    copied = 0
    for path in config.LOCAL_DIR.rglob("*"):
        if path.is_file() and path.suffix.lower() in config.IMAGE_EXTS:
            if path.name not in expected:
                continue  # deleted remotely; don't re-copy it
            dest = config.PHOTO_WEB_DIR / path.name
            if not dest.exists() or dest.stat().st_size != path.stat().st_size:
                shutil.copy2(path, dest)
                copied += 1

    # 2. Delete local photos that are no longer in the cloud (orphans).
    removed = 0
    for dest in config.PHOTO_WEB_DIR.iterdir():
        if dest.is_file() and dest.suffix.lower() in config.IMAGE_EXTS:
            if dest.name not in expected:
                dest.unlink()
                removed += 1

    if copied or removed:
        log.info("Photos mirrored: %d copied, %d removed.", copied, removed)


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

                    # Photos come from the direct project files (real ETag
                    # skipping, so unchanged photos are NOT re-downloaded).
                    remote_photos = qfc.download_photos()
                    # Mirror them to the web folder (copies new, deletes orphans).
                    sync_photos(remote_photos)

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