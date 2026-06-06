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
LOCAL_DIR = Path(os.environ.get("QFC_LOCAL_DIR", "./qfc_sync_data"))

PHOTO_WEB_DIR = Path(os.environ.get("PHOTO_WEB_DIR", "./web/photos"))

# --------------------------------------------------------------------------- #
# Timing
# --------------------------------------------------------------------------- #
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "10"))
PACKAGE_TIMEOUT = int(os.environ.get("PACKAGE_TIMEOUT", "180"))

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic"}