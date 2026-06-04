"""
config.py
---------
All configuration lives here. This is the ONLY file you need to edit.
client.py and main.py never need to be touched.

Search for the word "CHANGE" below: every spot you must personalize is marked.
"""

import os
from pathlib import Path

# --------------------------------------------------------------------------- #
# QFieldCloud connection
# --------------------------------------------------------------------------- #
QFC_URL = os.environ.get("QFC_URL", "https://app.qfield.cloud/api/v1/")

# CHANGE: your QFieldCloud username and password
QFC_USER = os.environ.get("QFC_USER", "***REMOVED***")
QFC_PASS = os.environ.get("QFC_PASS", "***REMOVED***")

# Optional: use a token instead of username/password. Leave empty to use the
# username/password above.
QFC_TOKEN = os.environ.get("QFC_TOKEN", "")

# CHANGE: the UUID of your project. Find it in the project's URL or settings
# in the QFieldCloud web app (looks like 123e4567-e89b-12d3-a456-426614174000)
PROJECT_ID = os.environ.get("QFC_PROJECT_ID", "f829bc38-1f8c-4ea9-a891-521b0f67d58b")

# --------------------------------------------------------------------------- #
# PostGIS connection
# --------------------------------------------------------------------------- #
# CHANGE: put the password you set for the "postgres" user during install.
# On your laptop the host stays "localhost". On the university server it ALSO
# stays "localhost" (because the database lives on the same machine as this
# script), so you will not need to change the host when you move it there.
PG_CONN = os.environ.get(
    "PG_CONN",
    "dbname=nexus3d host=localhost port=5432 user=postgres password=***REMOVED***",
)

# Target schema. Leave as "qfield" (the empty schema you already created).
PG_SCHEMA = os.environ.get("PG_SCHEMA", "qfield")

# --------------------------------------------------------------------------- #
# ogr2ogr location
# --------------------------------------------------------------------------- #
# ogr2ogr ships with QGIS. If it is on your PATH you can leave this as "ogr2ogr".
# If not (very common on Windows), CHANGE this to the FULL path to ogr2ogr.exe
# inside your QGIS install. Example for QGIS 3.34:
#   r"C:\Program Files\QGIS 3.34\bin\ogr2ogr.exe"
# The leading r before the quotes is important on Windows (raw string).
OGR2OGR = os.environ.get("OGR2OGR", r"C:\Program Files\QGIS 3.42.0\bin\ogr2ogr.exe")

# --------------------------------------------------------------------------- #
# Local folders (no need to change these for a basic setup)
# --------------------------------------------------------------------------- #
# Scratch space for downloaded data (can be deleted anytime)
LOCAL_DIR = Path(os.environ.get("QFC_LOCAL_DIR", "./qfc_sync_data"))

# Where photos are copied so your web app / Cesium can serve them
PHOTO_WEB_DIR = Path(os.environ.get("PHOTO_WEB_DIR", "./web/photos"))

# --------------------------------------------------------------------------- #
# Timing
# --------------------------------------------------------------------------- #
# Seconds between each "is there anything new?" check.
# Use 10 while testing so you see the loop react quickly; 30 is fine in production.
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "10"))

# How long to wait (seconds) for a packaging job to finish before giving up.
PACKAGE_TIMEOUT = int(os.environ.get("PACKAGE_TIMEOUT", "180"))

# File extensions treated as photos/attachments.
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic"}