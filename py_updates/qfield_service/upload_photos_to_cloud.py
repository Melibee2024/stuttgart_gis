"""
upload_photos_to_cloud.py
-------------------------
One-off helper. Uploads the local field photos into the QFieldCloud PROJECT
files (under DCIM/), so the sync service recognises them as legitimate remote
attachments instead of deleting them from the web folder as orphans.

Use this after recreating the cloud project from scratch (when the photos exist
locally but were never re-uploaded to the cloud). Reuses config.py, so it reads
the same .env (credentials + QFC_PROJECT_ID) the service uses.

Idempotent: re-running only uploads files whose content changed.
"""
from pathlib import Path

from qfieldcloud_sdk import sdk
from qfieldcloud_sdk.sdk import FileTransferType

import config

# The folder the Cesium backend serves /media from — where the recovered
# photos currently live.
MEDIA_DIR = Path(
    r"C:\3dcitydb-4.4.2\stuttgart_gis\cesium_web\stuttgart-backend\media"
)
# Matches building_photos.file_path -> "DCIM/<name>"
REMOTE_PREFIX = "DCIM"


def main() -> None:
    client = sdk.Client(url=config.QFC_URL, token=config.QFC_TOKEN)
    if not config.QFC_TOKEN:
        client.login(config.QFC_USER, config.QFC_PASS)

    print(f"Target project: {config.PROJECT_ID}")
    photos = sorted(MEDIA_DIR.glob("*.jpg"))
    print(f"Found {len(photos)} local photo(s) in {MEDIA_DIR}")

    ok = 0
    for p in photos:
        remote = f"{REMOTE_PREFIX}/{p.name}"
        try:
            client.upload_file(
                config.PROJECT_ID,
                FileTransferType.PROJECT,
                p,
                remote,
                False,  # show_progress
            )
            print(f"  uploaded  {remote}")
            ok += 1
        except Exception as err:
            print(f"  FAILED    {remote} -> {err}")

    print(f"\nDone: {ok}/{len(photos)} photo(s) uploaded to the cloud project.")


if __name__ == "__main__":
    main()
