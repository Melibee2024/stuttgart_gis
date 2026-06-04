"""
client.py
---------
The "communication layer". Everything that talks to QFieldCloud lives here:
authentication, detecting new activity, triggering packaging, and downloading.

It exposes one class, QfcSyncClient, that main.py uses. main.py never needs to
know how the QFieldCloud SDK works internally.
"""

import sys
import time
import logging

from qfieldcloud_sdk import sdk
from qfieldcloud_sdk.sdk import JobTypes, FileTransferStatus

import config

log = logging.getLogger("qfc_sync.client")


class QfcSyncClient:
    """Thin wrapper around the QFieldCloud SDK for our sync needs."""

    def __init__(self) -> None:
        self.client = sdk.Client(url=config.QFC_URL, token=config.QFC_TOKEN)

        # If no token was provided, log in with username/password.
        if not config.QFC_TOKEN:
            if not (config.QFC_USER and config.QFC_PASS):
                sys.exit("ERROR: set QFC_TOKEN, or both QFC_USER and QFC_PASS.")
            log.info("Logging in as %s ...", config.QFC_USER)
            self.client.login(config.QFC_USER, config.QFC_PASS)

    # ----------------------------------------------------------------- #
    # Change detection
    # ----------------------------------------------------------------- #
    def remote_fingerprint(self) -> str:
        """
        Build a single fingerprint string from all remote files and their
        md5sums (etags).

        This is the robust way to detect changes: a new photo, an edited
        geometry, ANY change shows up as a new or changed file. (Watching
        'delta_apply' jobs does NOT work, because uploading a photo to an
        existing feature is a file change, not a delta.)

        If the fingerprint differs from the previous cycle, something changed.
        """
        files = self.client.list_remote_files(config.PROJECT_ID)
        # Sort by name so the fingerprint is stable regardless of order.
        parts = sorted(
            f"{f.get('name')}:{f.get('md5sum') or f.get('etag')}" for f in files
        )
        return "|".join(parts)

    # ----------------------------------------------------------------- #
    # Packaging
    # ----------------------------------------------------------------- #
    def wait_until_idle(self) -> None:
        """
        Wait until the project has no jobs still queued or running.

        After a QField push, QFieldCloud may still be applying changes
        (a 'delta_apply' job in 'queued' or 'started' state). If we package
        before that finishes, the package would miss the most recent edit
        (the classic 'always one photo behind' problem). So we wait for the
        job queue to be empty first.
        """
        waited = 0
        while waited < config.PACKAGE_TIMEOUT:
            jobs = self.client.list_jobs(config.PROJECT_ID)
            busy = [j for j in jobs if j.get("status") in ("queued", "started", "pending")]
            if not busy:
                return
            log.info("Waiting for QFieldCloud to finish %d pending job(s) ...", len(busy))
            time.sleep(3)
            waited += 3

    def ensure_fresh_package(self) -> bool:
        """
        Trigger a 'package' job and wait until it finishes.
        Returns True if a fresh package is ready, False otherwise.
        """
        # Make sure all field changes are fully applied before we package,
        # otherwise the latest edit could be left out.
        self.wait_until_idle()

        log.info("Triggering package job ...")
        self.client.job_trigger(config.PROJECT_ID, JobTypes.PACKAGE, force=True)

        waited = 0
        while waited < config.PACKAGE_TIMEOUT:
            status = self.client.package_latest(config.PROJECT_ID)
            state = status.get("status")
            if state == "finished":
                log.info("Package ready.")
                return True
            if state == "failed":
                log.error("Packaging failed: %s", status)
                return False
            time.sleep(3)
            waited += 3

        log.warning("Timed out waiting for the package (%ss).", config.PACKAGE_TIMEOUT)
        return False

    # ----------------------------------------------------------------- #
    # Download (incremental thanks to ETags)
    # ----------------------------------------------------------------- #
    def download_package(self) -> list[dict]:
        """
        Download the latest packaged files into LOCAL_DIR.
        The SDK compares ETags, so only changed files are actually transferred.
        """
        config.LOCAL_DIR.mkdir(parents=True, exist_ok=True)
        files = self.client.package_download(
            project_id=config.PROJECT_ID,
            local_dir=str(config.LOCAL_DIR),
            filter_glob="*",
            show_progress=False,
        )
        changed = [f for f in files if f.get("status") == FileTransferStatus.SUCCESS]
        log.info("%d file(s) downloaded/updated.", len(changed))
        return changed