"""
run_pipeline.py
===============
Nexus3D Stuttgart — Full pipeline orchestrator

Stages:
  Pre-flight  → Drop dependent view so ifc_to_postgis.py can recreate the table
  Stage 2+3   → IFC → PostGIS  (ifc_to_postgis.py)
  Verify      → Check bounding box + Z values in PostGIS
  Rebuild     → Recreate nexus3d.v_ifc_tiles view
  Stage 5     → Regenerate 3D Tiles via pg2b3dm

Usage:
    python run_pipeline.py

Place this file in the same folder as ifc_to_postgis.py
"""

import subprocess
import sys
import os
import psycopg2

# ─── CONFIGURATION ────────────────────────────────────────────────────────────

DB = {
    "host":     "localhost",
    "port":     5432,
    "dbname":   "3DCity",
    "user":     "postgres",
    "password": "Abhi2345@com",
}

TILES_OUTPUT = (
    r"C:\Users\abhir\PycharmProjects\stuttgart_gis"
    r"\cesium_web\stuttgart-backend\public\tiles"
)

PG2B3DM_CONN = (
    f"Host={DB['host']};"
    f"Database={DB['dbname']};"
    f"Username={DB['user']};"
    f"Password={DB['password']}"
)

PG2B3DM_ATTRS = (
    "global_id,ifc_class,name,storey,"
    "z_min_ellipsoidal,z_max_ellipsoidal,element_height_m"
)

# Stuttgart verification ranges
EXPECTED_EASTING_MIN  = 512500
EXPECTED_EASTING_MAX  = 512750
EXPECTED_NORTHING_MIN = 5402900
EXPECTED_NORTHING_MAX = 5403200
EXPECTED_Z_MIN        = 230   # basement can be ~5m below site origin (245-5=239.96)
EXPECTED_Z_MAX        = 275

# ─── TILE VIEW SQL ────────────────────────────────────────────────────────────
# Recreated after every ingestion run.
#
# Vertical alignment is NOT done here and uses NO hardcoded geoid. The tiles
# carry the IFC at its raw EPSG:25832 height (z_min/z_max are the unmodified
# orthometric values). The Cesium frontend then measures the matching citydb
# building's rendered base live (ray-cast its roof, minus its DB height) and
# shifts the IFC tileset so its bottom snaps onto that base. This is robust to
# whatever vertical datum the ion tileset uses and stays glued on terrain
# toggle. See main.js alignIFCToCityDBBase() and /api/georef.

VIEW_SQL = """
CREATE VIEW nexus3d.v_ifc_tiles AS
SELECT
    id,
    ST_CollectionExtract(ST_Transform(geometry, 4326), 3) AS geom,
    global_id,
    ifc_class,
    name,
    storey,
    ROUND(ST_ZMin(geometry)::numeric, 3) AS z_min_ellipsoidal,
    ROUND(ST_ZMax(geometry)::numeric, 3) AS z_max_ellipsoidal,
    ROUND((ST_ZMax(geometry) - ST_ZMin(geometry))::numeric, 3) AS element_height_m
FROM nexus3d.ifc_elements
WHERE geometry IS NOT NULL;
"""

# ─── HELPERS ──────────────────────────────────────────────────────────────────

def banner(title):
    print()
    print("─" * 62)
    print(f"  {title}")
    print("─" * 62)

def ok(msg):   print(f"  ✅  {msg}")
def err(msg):  print(f"  ❌  {msg}")
def info(msg): print(f"  ℹ   {msg}")

def get_conn():
    return psycopg2.connect(**DB)

# ─── PRE-FLIGHT — DROP DEPENDENT VIEW ─────────────────────────────────────────

def preflight_drop_view():
    banner("PRE-FLIGHT — Drop dependent view")
    try:
        conn = get_conn()
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("DROP VIEW IF EXISTS nexus3d.v_ifc_tiles;")
        ok("nexus3d.v_ifc_tiles dropped (will be recreated after ingestion)")
        cur.close()
        conn.close()
        return True
    except Exception as e:
        err(f"Could not drop view: {e}")
        return False

# ─── STAGE 2+3 — IFC → POSTGIS ────────────────────────────────────────────────

def run_ingestion():
    banner("STAGE 2+3 — IFC ingestion → PostGIS")
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "ifc_to_postgis.py")
    if not os.path.exists(script):
        err(f"ifc_to_postgis.py not found at: {script}")
        return False

    info(f"Running: {script}")
    result = subprocess.run([sys.executable, script])

    if result.returncode != 0:
        err(f"ifc_to_postgis.py exited with code {result.returncode}")
        return False

    ok("Ingestion script completed")
    return True

# ─── VERIFY — POSTGIS BOUNDING BOX + Z VALUES ─────────────────────────────────

def verify_postgis():
    banner("VERIFY — PostGIS geometry check")
    passed = True

    try:
        conn = get_conn()
        cur  = conn.cursor()

        # Row counts
        cur.execute("""
            SELECT
                COUNT(*)                                        AS total,
                COUNT(*) FILTER (WHERE geometry IS NOT NULL)   AS with_geom,
                COUNT(*) FILTER (WHERE geometry IS NULL)       AS no_geom
            FROM nexus3d.ifc_elements;
        """)
        total, with_geom, no_geom = cur.fetchone()
        info(f"Rows: {total} total — {with_geom} with geometry — {no_geom} without")

        if total == 0:
            err("Table is empty — ingestion wrote no rows")
            return False

        # Bounding box
        cur.execute("""
            SELECT
                ROUND(ST_XMin(ST_Extent(geometry))::numeric, 2),
                ROUND(ST_XMax(ST_Extent(geometry))::numeric, 2),
                ROUND(ST_YMin(ST_Extent(geometry))::numeric, 2),
                ROUND(ST_YMax(ST_Extent(geometry))::numeric, 2)
            FROM nexus3d.ifc_elements
            WHERE geometry IS NOT NULL;
        """)
        x_min, x_max, y_min, y_max = cur.fetchone()
        info(f"X (Easting):  {x_min} → {x_max}")
        info(f"Y (Northing): {y_min} → {y_max}")

        if EXPECTED_EASTING_MIN < float(x_min) < EXPECTED_EASTING_MAX:
            ok("Easting in Stuttgart range ✓")
        else:
            err(f"Easting wrong — expected ~512500–512750, got {x_min}")
            err("  → IfcMapConversion translation not being applied in ifc_to_postgis.py")
            passed = False

        if EXPECTED_NORTHING_MIN < float(y_min) < EXPECTED_NORTHING_MAX:
            ok("Northing in Stuttgart range ✓")
        else:
            err(f"Northing wrong — expected ~5402900–5403200, got {y_min}")
            passed = False

        # Z range
        cur.execute("""
            SELECT
                ROUND(MIN(ST_ZMin(geometry))::numeric, 2),
                ROUND(MAX(ST_ZMax(geometry))::numeric, 2)
            FROM nexus3d.ifc_elements
            WHERE geometry IS NOT NULL;
        """)
        z_bottom, z_top = cur.fetchone()
        info(f"Z (Height):   {z_bottom} m → {z_top} m (orthometric, DHHN2016)")

        if EXPECTED_Z_MIN < float(z_bottom) < EXPECTED_Z_MAX:
            ok(f"Z base correct (~245m expected) ✓")
        else:
            err(f"Z base wrong — expected ~245m, got {z_bottom}m")
            if float(z_bottom) < 50:
                err("  → World coordinate transform not applied (still local IFC coords)")
            passed = False

        height_span = float(z_top) - float(z_bottom)
        info(f"Building height span: {height_span:.1f} m")
        if 10 < height_span < 40:
            ok("Height span looks reasonable ✓")
        else:
            err(f"Unusual height span: {height_span:.1f}m (expected 15–30m for Bau4)")

        # Sample rows
        cur.execute("""
            SELECT ifc_class,
                   ROUND(ST_X(ST_Centroid(ST_Force2D(geometry)))::numeric, 1) AS cx,
                   ROUND(ST_Y(ST_Centroid(ST_Force2D(geometry)))::numeric, 1) AS cy
            FROM nexus3d.ifc_elements
            WHERE geometry IS NOT NULL
            LIMIT 3;
        """)
        info("Sample centroids:")
        for row in cur.fetchall():
            info(f"  {row[0]:15s}  E={row[1]}  N={row[2]}")

        cur.close()
        conn.close()

    except Exception as e:
        err(f"PostGIS error: {e}")
        return False

    return passed

# ─── REBUILD VIEW ─────────────────────────────────────────────────────────────

def rebuild_view():
    banner("REBUILD — Recreate nexus3d.v_ifc_tiles")
    try:
        conn = get_conn()
        conn.autocommit = True
        cur = conn.cursor()

        cur.execute("DROP VIEW IF EXISTS nexus3d.v_ifc_tiles;")
        cur.execute(VIEW_SQL)
        ok("nexus3d.v_ifc_tiles recreated ✓")

        # Quick sanity check
        cur.execute("SELECT COUNT(*) FROM nexus3d.v_ifc_tiles;")
        count = cur.fetchone()[0]
        info(f"View row count: {count}")

        cur.close()
        conn.close()
        return True
    except Exception as e:
        err(f"Could not recreate view: {e}")
        return False

# ─── STAGE 5 — REGENERATE 3D TILES ────────────────────────────────────────────

def regenerate_tiles():
    banner("STAGE 5 — Regenerate 3D Tiles (pg2b3dm)")
    os.makedirs(TILES_OUTPUT, exist_ok=True)
    info(f"Output: {TILES_OUTPUT}")

    cmd = [
        "pg2b3dm",
        "--connection", PG2B3DM_CONN,
        "-t", "nexus3d.v_ifc_tiles",
        "-c", "geom",
        "-a", PG2B3DM_ATTRS,
        "-o", TILES_OUTPUT,
    ]

    info("Running pg2b3dm...")
    result = subprocess.run(cmd)

    if result.returncode != 0:
        err(f"pg2b3dm exited with code {result.returncode}")
        return False

    tileset = os.path.join(TILES_OUTPUT, "tileset.json")
    if not os.path.exists(tileset):
        err("tileset.json not found — pg2b3dm may have failed silently")
        return False

    ok("tileset.json created ✓")

    tile_files = [
        f for f in os.listdir(TILES_OUTPUT)
        if f.endswith(".glb") or f.endswith(".b3dm")
    ]
    ok(f"{len(tile_files)} tile file(s) written")
    return True

# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    print()
    print("══════════════════════════════════════════════════════════════")
    print("  Nexus3D Stuttgart — Pipeline Runner")
    print("══════════════════════════════════════════════════════════════")

    stages = [
        ("Pre-flight — drop view",       preflight_drop_view),
        ("Stage 2+3 — ingestion",        run_ingestion),
        ("Verify    — PostGIS check",    verify_postgis),
        ("Rebuild   — recreate view",    rebuild_view),
        ("Stage 5   — tile generation",  regenerate_tiles),
    ]

    results = {}
    for name, fn in stages:
        results[name] = fn()
        if not results[name]:
            print()
            print(f"  ⛔  Stopped at: {name}")
            print("      Fix errors above before re-running.")
            break

    print()
    print("══════════════════════════════════════════════════════════════")
    print("  SUMMARY")
    print("══════════════════════════════════════════════════════════════")
    for name, passed in results.items():
        print(f"  {'✅ PASS' if passed else '❌ FAIL'}  {name}")

    if all(results.values()) and len(results) == len(stages):
        print()
        print("  🎉  All stages complete.")
        print("  → Hard refresh Cesium (Ctrl+Shift+R) to load the new tiles.")
    else:
        print()
        print("  ⛔  Pipeline incomplete — see errors above.")

    print("══════════════════════════════════════════════════════════════")


if __name__ == "__main__":
    main()