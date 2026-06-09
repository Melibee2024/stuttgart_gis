"""
config.py
---------
All configuration lives here. This is the ONLY file you need to edit.
client.py and main.py never need to be touched.

Search for the word "CHANGE": every spot you must personalize is marked.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# --------------------------------------------------------------------------- #
# QFieldCloud connection
# --------------------------------------------------------------------------- #
QFC_URL = os.environ.get("QFC_URL", "https://app.qfield.cloud/api/v1/")

# CHANGE: your QFieldCloud username and password
QFC_USER = os.environ.get("QFC_USER", "")
QFC_PASS = os.environ.get("QFC_PASS", "")

# Optional: use a token instead of username/password.
QFC_TOKEN = os.environ.get("QFC_TOKEN", "")

# CHANGE: project ID
PROJECT_ID = os.environ.get("QFC_PROJECT_ID", "")

# --------------------------------------------------------------------------- #
# PostGIS connection
# --------------------------------------------------------------------------- #
PG_CONN = os.environ.get("PG_CONN", "")

PG_SCHEMA = os.environ.get("PG_SCHEMA", "qfield_data")

# --------------------------------------------------------------------------- #
# ogr2ogr location
# --------------------------------------------------------------------------- #
OGR2OGR = os.environ.get(
    "OGR2OGR",
    r"C:\Program Files\QGIS 3.42.0\bin\ogr2ogr.exe"
)

# --------------------------------------------------------------------------- #
# Local folders
# --------------------------------------------------------------------------- #
# Anchor all relative paths to THIS file's location, not the current working
# directory. This makes the service behave identically no matter where you
# launch it from (PyCharm, a terminal in another folder, a scheduled task...).
BASE_DIR = Path(__file__).resolve().parent


def _resolve(env_var: str, default: str) -> Path:
    """Resolve a configured path: absolute paths are used as-is; relative paths
    are resolved against BASE_DIR (the qfield_service folder)."""
    raw = Path(os.environ.get(env_var, default))
    return raw if raw.is_absolute() else (BASE_DIR / raw).resolve()


# Where downloaded GeoPackages + raw photos are cached.
LOCAL_DIR = _resolve("QFC_LOCAL_DIR", "./qfc_sync_data")

# Where referenced photos are mirrored. This MUST match the folder the Cesium
# backend serves '/media' from (server.mjs: express.static('media')), otherwise
# the synced photos won't appear in the Cesium viewer.
PHOTO_WEB_DIR = _resolve("PHOTO_WEB_DIR", "../../cesium_web/stuttgart-backend/media")

# --------------------------------------------------------------------------- #
# Timing
# --------------------------------------------------------------------------- #
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "10"))
PACKAGE_TIMEOUT = int(os.environ.get("PACKAGE_TIMEOUT", "180"))

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic"}