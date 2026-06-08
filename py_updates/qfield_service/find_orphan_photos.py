"""
find_orphan_photos.py
---------------------
Helper utility to find (and optionally delete) "orphan" photos that still
exist as files in QFieldCloud's project storage but are no longer referenced
by any feature in the GeoPackage (i.e. you deleted the record/feature in
QField, but the underlying image file was left behind in the cloud).

This reuses the exact same "ground truth" logic as main.py
(get_referenced_photos): a photo is considered an orphan if its filename does
NOT appear in any 'file_path'/'photo_name' column of any table in the
downloaded GeoPackage(s).

SAFE BY DEFAULT: running this script only LISTS orphans, it does not delete
anything. Pass --delete to actually remove them from QFieldCloud.

Usage:
    python find_orphan_photos.py            # just list orphans (dry run)
    python find_orphan_photos.py --delete   # list AND delete them (asks for
                                             # confirmation before deleting)
"""

import sys
import logging
from pathlib import Path

import config
from client import QfcSyncClient

# Reuse the exact reference-detection logic from main.py so results stay
# consistent with what the regular sync considers "referenced".
from main import get_referenced_photos

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("qfc_sync.find_orphans")


def main() -> None:
    do_delete = "--delete" in sys.argv

    client = QfcSyncClient()

    # 1. Make sure we have an up-to-date GeoPackage to compute "referenced" from.
    log.info("Refreshing GeoPackage from the latest package ...")
    client.ensure_fresh_package()
    client.download_geometry_from_package()

    # 2. Ground truth: which photo filenames are still referenced by features?
    referenced = get_referenced_photos()

    # 3. What photo files actually exist in QFieldCloud's project storage?
    remote_photos = client.list_remote_photos()
    log.info("QFieldCloud currently stores %d photo file(s).", len(remote_photos))

    # 4. Orphans = exist remotely, but their filename is NOT referenced anymore.
    orphans = [
        f for f in remote_photos
        if Path(f["name"]).name not in referenced
    ]

    if not orphans:
        log.info("No orphan photos found. Everything in the cloud is referenced. ✅")
        return

    print()
    print(f"Found {len(orphans)} orphan photo(s) (in QFieldCloud but not referenced by any feature):")
    for f in orphans:
        print(f"   - {f['name']}")
    print()

    if not do_delete:
        log.info("Dry run only — nothing was deleted. Re-run with --delete to remove these from QFieldCloud.")
        return

    # 5. Confirm before doing anything destructive.
    answer = input(
        f"Type 'DELETE' (all caps) to permanently remove these {len(orphans)} file(s) "
        f"from QFieldCloud's project storage, or anything else to cancel: "
    )
    if answer.strip() != "DELETE":
        log.info("Cancelled. Nothing was deleted.")
        return

    names = [f["name"] for f in orphans]
    result = client.client.delete_files(
        project_id=config.PROJECT_ID,
        glob_patterns=names,
        throw_on_error=False,
    )
    log.info("Delete request sent. Result: %s", result)


if __name__ == "__main__":
    main()
