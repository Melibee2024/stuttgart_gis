"""
ifc_to_postgis.py
=================
Nexus3D Stuttgart — Stage 2 + 3: IFC → PostGIS Ingestion
=========================================================
v5 fixes vs v4:
  - transform_vertex now applies full IfcMapConversion rotation (was
    translation-only in v4, causing ~90° misalignment in Cesium).
    Correct formula: wx = E + s*(cos_t*lx - sin_t*ly)
                     wy = N + s*(sin_t*lx + cos_t*ly)
  - read_map_conversion comment updated to reflect rotation IS applied

v4 fixes vs v3:
  - DROP TABLE uses CASCADE to handle dependent views (v_ifc_tiles,
    dynamic_ifc_alignment) — no more DependentObjectsStillExist error
  - Views recreated automatically after table rebuild
  - IfcStair / IfcStairFlight removed from ARCHITECTURAL — cast-in-place
    stairs produce 100% null geometry via IfcOpenShell tessellation,
    causing black artifact lines in QGIS
  - Null geometry skip logged per element for diagnostics

Each row in nexus3d.ifc_elements contains:
  - global_id            : IFC GlobalId (unique key)
  - ifc_class            : entity type (IfcWall, IfcSlab, etc.)
  - name                 : element name from IFC
  - storey               : parent building storey
  - z_min_ellipsoidal    : minimum Z in EPSG:25832 (ellipsoidal)
  - z_max_ellipsoidal    : maximum Z in EPSG:25832 (ellipsoidal)
  - geometry             : MULTIPOLYGON Z in EPSG:25832 (PostGIS)
  - attributes           : all Pset properties as JSONB

Requirements:
    pip install ifcopenshell psycopg2-binary

Usage:
    python ifc_to_postgis.py
"""

import sys
import time

import ifcopenshell
import ifcopenshell.geom
import ifcopenshell.util.element
import psycopg2
from psycopg2.extras import Json


# ══════════════════════════════════════════════════════════════════════════════
#  CONFIG — edit this section only
# ══════════════════════════════════════════════════════════════════════════════

IFC_PATH = r"C:\Users\abhir\PycharmProjects\stuttgart_gis\IFCGeoreferencing\HFT_Bau4_2025.04.22_georef.ifc"

DB_CONFIG = {
    "host":     "localhost",
    "port":     5432,
    "dbname":   "3DCity",
    "user":     "postgres",
    "password": "Abhi2345@com",
}

SCHEMA = "nexus3d"
TABLE  = "ifc_elements"

# IfcStair / IfcStairFlight excluded — cast-in-place stairs fail IfcOpenShell
# tessellation completely (24/24 null in Bau4), producing black artifact lines.
ARCHITECTURAL = [
    "IfcWall",
    "IfcSlab",
    "IfcWindow",
    "IfcDoor",
    "IfcColumn",
    "IfcRoof",
    "IfcSpace",
]

SRID = 25832

GEOM_SETTINGS = ifcopenshell.geom.settings()
GEOM_SETTINGS.set(GEOM_SETTINGS.USE_WORLD_COORDS, True)
GEOM_SETTINGS.set(GEOM_SETTINGS.WELD_VERTICES, True)


# ══════════════════════════════════════════════════════════════════════════════
#  FORMATTING HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _sep(char="─", width=62):
    print(char * width)

def _header(title):
    _sep("═")
    print(f"  {title}")
    _sep("═")


# ══════════════════════════════════════════════════════════════════════════════
#  GEOREFERENCING
# ══════════════════════════════════════════════════════════════════════════════

def read_map_conversion(ifc_file):
    """
    Read IfcMapConversion from IFC.
    Returns (E, N, H, cos_t, sin_t, scale) or raises RuntimeError.

    Translation (E, N, H) and rotation (XAxisAbscissa/Ordinate) are both
    applied during vertex transform:
        wx = E + scale * (cos_t * lx  -  sin_t * ly)
        wy = N + scale * (sin_t * lx  +  cos_t * ly)
        wz = H + scale * lz
    """
    mcs = ifc_file.by_type("IfcMapConversion")
    if not mcs:
        raise RuntimeError(
            "No IfcMapConversion found in IFC file.\n"
            "Run georeference_ifc.py first to inject georeferencing."
        )

    mc    = mcs[0]
    E     = float(mc.Eastings)
    N     = float(mc.Northings)
    H     = float(mc.OrthogonalHeight)
    cos_t = float(mc.XAxisAbscissa) if mc.XAxisAbscissa is not None else 1.0
    sin_t = float(mc.XAxisOrdinate) if mc.XAxisOrdinate is not None else 0.0
    scale = float(mc.Scale)         if mc.Scale         is not None else 1.0

    import math
    angle_deg = math.degrees(math.atan2(sin_t, cos_t))

    print(f"  IfcMapConversion found ✓")
    print(f"    Eastings:          {E}")
    print(f"    Northings:         {N}")
    print(f"    OrthogonalHeight:  {H}")
    print(f"    XAxisAbscissa:     {cos_t:.8f}  (cos θ)")
    print(f"    XAxisOrdinate:     {sin_t:.8f}  (sin θ)")
    print(f"    Rotation angle:    {angle_deg:.4f}°  (rotation WILL be applied)")
    print(f"    Scale:             {scale}")

    if not (510000 < E < 515000 and 5400000 < N < 5406000):
        print(f"  ⚠  WARNING: Eastings/Northings look wrong for Stuttgart.")
        print(f"     Expected ~512614 E, ~5403013 N (EPSG:25832)")

    return (E, N, H, cos_t, sin_t, scale)


def transform_vertex(lx, ly, lz, mc):
    """
    Apply full IfcMapConversion: rotation + translation.
    IFC local coords → EPSG:25832 world coords.

    World = Translation + Scale * Rotation * Local
    where Rotation = [[cos_t, -sin_t],
                       [sin_t,  cos_t]]

    This correctly handles buildings whose local X axis is not aligned
    with geographic East (XAxisAbscissa != 1.0).
    """
    E, N, H, cos_t, sin_t, scale = mc
    wx = E + scale * (cos_t * lx - sin_t * ly)
    wy = N + scale * (sin_t * lx + cos_t * ly)
    wz = H + scale * lz
    return wx, wy, wz


# ══════════════════════════════════════════════════════════════════════════════
#  IFC ATTRIBUTE HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def get_storey(element) -> str:
    """Walk up IFC decomposition tree to find the parent IfcBuildingStorey."""
    try:
        for rel in getattr(element, "ContainedInStructure", []):
            s = rel.RelatingStructure
            if s.is_a("IfcBuildingStorey"):
                return s.Name or "Unknown Storey"
            if s.is_a("IfcBuilding"):
                return "Building Level"
        for rel in getattr(element, "Decomposes", []):
            obj = rel.RelatingObject
            if obj.is_a("IfcBuildingStorey"):
                return obj.Name or "Unknown Storey"
    except Exception:
        pass
    return "Unknown"


def get_psets(element) -> dict:
    """Extract all property sets as a nested dict."""
    psets = {}
    try:
        raw = ifcopenshell.util.element.get_psets(element)
        for pset_name, props in raw.items():
            if isinstance(props, dict):
                psets[pset_name] = {
                    k: str(v) if not isinstance(v, (str, int, float, bool, type(None))) else v
                    for k, v in props.items()
                }
    except Exception:
        pass
    return psets


# ══════════════════════════════════════════════════════════════════════════════
#  GEOMETRY: mesh → WKT MULTIPOLYGON Z (world coords)
# ══════════════════════════════════════════════════════════════════════════════

def shape_to_wkt(shape, mc) -> str | None:
    """
    Convert IfcOpenShell triangulated mesh → WKT MULTIPOLYGON Z in EPSG:25832.
    Each triangle becomes one polygon in the MULTIPOLYGON.
    """
    try:
        geo   = shape.geometry
        verts = geo.verts
        faces = geo.faces

        if not verts or not faces:
            return None

        local_pts = [
            (verts[i], verts[i+1], verts[i+2])
            for i in range(0, len(verts), 3)
        ]

        world_pts = [transform_vertex(x, y, z, mc) for x, y, z in local_pts]

        polygons = []
        for i in range(0, len(faces), 3):
            a, b, c    = faces[i], faces[i+1], faces[i+2]
            p0, p1, p2 = world_pts[a], world_pts[b], world_pts[c]
            ring = (
                f"{p0[0]:.4f} {p0[1]:.4f} {p0[2]:.4f},"
                f"{p1[0]:.4f} {p1[1]:.4f} {p1[2]:.4f},"
                f"{p2[0]:.4f} {p2[1]:.4f} {p2[2]:.4f},"
                f"{p0[0]:.4f} {p0[1]:.4f} {p0[2]:.4f}"
            )
            polygons.append(f"(({ring}))")

        if not polygons:
            return None

        return "MULTIPOLYGON Z (" + ",".join(polygons) + ")"

    except Exception:
        return None


# ══════════════════════════════════════════════════════════════════════════════
#  DATABASE
# ══════════════════════════════════════════════════════════════════════════════

DDL = f"""
CREATE TABLE IF NOT EXISTS {SCHEMA}.{TABLE} (
    id                  SERIAL PRIMARY KEY,
    global_id           TEXT                        NOT NULL UNIQUE,
    ifc_class           TEXT                        NOT NULL,
    name                TEXT,
    storey              TEXT,
    z_min_ellipsoidal   DOUBLE PRECISION
        GENERATED ALWAYS AS (ST_ZMin(geometry)) STORED,
    z_max_ellipsoidal   DOUBLE PRECISION
        GENERATED ALWAYS AS (ST_ZMax(geometry)) STORED,
    geometry            GEOMETRY(MultiPolygonZ, {SRID}),
    attributes          JSONB
);

CREATE INDEX IF NOT EXISTS idx_{TABLE}_global_id
    ON {SCHEMA}.{TABLE} (global_id);

CREATE INDEX IF NOT EXISTS idx_{TABLE}_ifc_class
    ON {SCHEMA}.{TABLE} (ifc_class);

CREATE INDEX IF NOT EXISTS idx_{TABLE}_geometry
    ON {SCHEMA}.{TABLE} USING GIST (geometry);

CREATE INDEX IF NOT EXISTS idx_{TABLE}_attributes
    ON {SCHEMA}.{TABLE} USING GIN (attributes);
"""

# ── Views recreated after DROP CASCADE ───────────────────────────────────────
# v_ifc_tiles serves EPSG:4326 geometry at the IFC's RAW EPSG:25832 height (no
# hardcoded geoid). Vertical alignment to the citydb building base is done live
# in the Cesium frontend (alignIFCToCityDBBase). Keep identical to
# run_pipeline.py's VIEW_SQL — run_pipeline recreates it after ingestion, but
# this guards a standalone ifc_to_postgis.py run.
RECREATE_VIEWS_SQL = f"""
CREATE OR REPLACE VIEW {SCHEMA}.v_ifc_tiles AS
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
FROM {SCHEMA}.{TABLE}
WHERE geometry IS NOT NULL;

CREATE OR REPLACE VIEW {SCHEMA}.dynamic_ifc_alignment AS
SELECT
    id,
    global_id,
    ifc_class,
    z_min_ellipsoidal,
    geometry AS geom
FROM {SCHEMA}.{TABLE}
WHERE geometry IS NOT NULL;
"""

INSERT_SQL = f"""
INSERT INTO {SCHEMA}.{TABLE}
    (global_id, ifc_class, name, storey, geometry, attributes)
VALUES
    (%s, %s, %s, %s, ST_GeomFromText(%s, {SRID}), %s)
ON CONFLICT (global_id) DO UPDATE SET
    ifc_class  = EXCLUDED.ifc_class,
    name       = EXCLUDED.name,
    storey     = EXCLUDED.storey,
    geometry   = EXCLUDED.geometry,
    attributes = EXCLUDED.attributes;
"""

# CASCADE drops dependent views (v_ifc_tiles, dynamic_ifc_alignment)
DROP_RECREATE_SQL = f"""
DROP TABLE IF EXISTS {SCHEMA}.{TABLE} CASCADE;
"""

VERIFY_SQL = f"""
SELECT
    ifc_class,
    COUNT(*)                                        AS total,
    COUNT(geometry)                                 AS with_geometry,
    COUNT(*) - COUNT(geometry)                      AS no_geometry,
    ROUND(AVG(ST_NPoints(geometry))::numeric, 1)    AS avg_points
FROM {SCHEMA}.{TABLE}
GROUP BY ifc_class
ORDER BY total DESC;
"""

BBOX_SQL = f"""
SELECT
    ST_AsText(ST_Extent(geometry))  AS bbox_2d,
    MIN(ST_ZMin(geometry))          AS z_min,
    MAX(ST_ZMax(geometry))          AS z_max
FROM {SCHEMA}.{TABLE}
WHERE geometry IS NOT NULL;
"""

SAMPLE_SQL = f"""
SELECT global_id, ifc_class,
       ST_X(ST_Centroid(ST_Force2D(ST_GeometryN(geometry,1)))) AS sample_x,
       ST_Y(ST_Centroid(ST_Force2D(ST_GeometryN(geometry,1)))) AS sample_y
FROM {SCHEMA}.{TABLE}
WHERE geometry IS NOT NULL
LIMIT 3;
"""


def setup_database(conn):
    """
    Drop (CASCADE) and recreate ifc_elements table, then recreate dependent views.
    CASCADE handles v_ifc_tiles and dynamic_ifc_alignment automatically.
    """
    with conn.cursor() as cur:
        cur.execute(DROP_RECREATE_SQL)   # CASCADE drops dependent views
        cur.execute(DDL)                 # recreate table + indexes
        cur.execute(RECREATE_VIEWS_SQL)  # recreate views
    conn.commit()
    print(f"  Table {SCHEMA}.{TABLE} created fresh ✓")
    print(f"  Geometry type: MultiPolygonZ, SRID: {SRID}")
    print(f"  Indexes: global_id, ifc_class, geometry (GIST), attributes (GIN) ✓")
    print(f"  Views recreated: v_ifc_tiles, dynamic_ifc_alignment ✓")


def _flush(conn, batch):
    with conn.cursor() as cur:
        cur.executemany(INSERT_SQL, batch)
    conn.commit()


def verify(conn):
    with conn.cursor() as cur:

        cur.execute(VERIFY_SQL)
        rows = cur.fetchall()
        print(f"\n  {'Class':<28} {'Total':>6} {'w/Geom':>8} {'NoGeom':>8} {'AvgPts':>8}")
        _sep()
        for row in rows:
            print(f"  {row[0]:<28} {row[1]:>6} {row[2]:>8} {row[3]:>8} {str(row[4]):>8}")

        cur.execute(BBOX_SQL)
        bbox = cur.fetchone()
        if bbox and bbox[0]:
            print(f"\n  Bounding box:  {bbox[0]}")
            print(f"  Z range:       {bbox[1]:.2f} m → {bbox[2]:.2f} m")
            if "512" in bbox[0] and "5403" in bbox[0]:
                print(f"\n  ✅ Coordinates confirmed — Stuttgart / EPSG:25832 range")
            else:
                print(f"\n  ❌ Coordinates look wrong — expected ~512xxx, 5403xxx")

        cur.execute(SAMPLE_SQL)
        samples = cur.fetchall()
        if samples:
            print(f"\n  Sample centroids (first 3 elements):")
            for s in samples:
                print(f"    {s[1]:<20} {s[0][:12]}...  X={s[2]:.1f}  Y={s[3]:.1f}")


# ══════════════════════════════════════════════════════════════════════════════
#  INGESTION
# ══════════════════════════════════════════════════════════════════════════════

def ingest(ifc_file, conn, mc):
    stats = {cls: {"total": 0, "geom_ok": 0, "geom_fail": 0}
             for cls in ARCHITECTURAL}

    total_elements = sum(len(ifc_file.by_type(cls)) for cls in ARCHITECTURAL)
    print(f"  Elements to process: {total_elements}  (IfcStair excluded — null geometry)")
    _sep()

    processed  = 0
    batch      = []
    BATCH_SIZE = 50

    for ifc_class in ARCHITECTURAL:
        elements = ifc_file.by_type(ifc_class)
        print(f"  {ifc_class:<25} {len(elements):>4} elements")

        for element in elements:
            stats[ifc_class]["total"] += 1
            processed += 1

            # Geometry — tessellate + rotate + translate to world coords
            wkt = None
            try:
                shape = ifcopenshell.geom.create_shape(GEOM_SETTINGS, element)
                wkt   = shape_to_wkt(shape, mc)
                if wkt:
                    stats[ifc_class]["geom_ok"] += 1
                else:
                    stats[ifc_class]["geom_fail"] += 1
                    print(f"    [null geom] {ifc_class} {element.GlobalId[:12]}")
            except Exception as ex:
                stats[ifc_class]["geom_fail"] += 1
                print(f"    [geom err]  {ifc_class} {element.GlobalId[:12]}: {ex}")

            # Attributes
            psets  = get_psets(element)
            storey = get_storey(element)
            attrs  = {
                "ifc_class": ifc_class,
                "name":      getattr(element, "Name", None),
                "tag":       getattr(element, "Tag", None),
                "storey":    storey,
                "psets":     psets,
            }

            batch.append((
                element.GlobalId,
                ifc_class,
                getattr(element, "Name", None),
                storey,
                wkt,
                Json(attrs),
            ))

            if len(batch) >= BATCH_SIZE:
                _flush(conn, batch)
                batch.clear()
                print(f"  Inserted {processed}/{total_elements} ...", end="\r")

    if batch:
        _flush(conn, batch)

    print(f"\n  Inserted {processed}/{total_elements} elements ✓")
    return stats


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    _header("Nexus3D Stuttgart — IFC → PostGIS Ingestion  (Stage 2+3 v5)")

    # ── Load IFC ──────────────────────────────────────────────────────────────
    fname = IFC_PATH.split("\\")[-1]
    print(f"\nLoading IFC:  {fname}")
    t0 = time.time()
    ifc_file = ifcopenshell.open(IFC_PATH)
    print(f"Loaded:       {len(list(ifc_file))} entities  ({time.time()-t0:.1f}s)")

    # ── Read MapConversion ────────────────────────────────────────────────────
    _sep()
    print("Reading IfcMapConversion...")
    try:
        mc = read_map_conversion(ifc_file)
    except RuntimeError as e:
        print(f"\n  ❌ {e}")
        sys.exit(1)

    # ── Connect to PostGIS ────────────────────────────────────────────────────
    _sep()
    print("Connecting to PostGIS...")
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        print(f"  Connected → {DB_CONFIG['dbname']} @ {DB_CONFIG['host']}:{DB_CONFIG['port']} ✓")
    except Exception as e:
        print(f"  ❌ Connection failed: {e}")
        sys.exit(1)

    # ── Setup table ───────────────────────────────────────────────────────────
    _sep()
    print("Setting up database (DROP CASCADE + recreate table + views)...")
    setup_database(conn)

    # ── Ingest ────────────────────────────────────────────────────────────────
    _sep()
    print("Ingesting architectural elements...")
    _sep()
    t1    = time.time()
    stats = ingest(ifc_file, conn, mc)
    elapsed = time.time() - t1

    # ── Verify ────────────────────────────────────────────────────────────────
    _sep()
    print("Verification — PostGIS row counts + bounding box:")
    verify(conn)

    conn.close()

    # ── Summary ───────────────────────────────────────────────────────────────
    _sep("═")
    print("  DONE")
    _sep("═")
    total_ok   = sum(v["geom_ok"]   for v in stats.values())
    total_fail = sum(v["geom_fail"] for v in stats.values())
    total_rows = total_ok + total_fail
    print(f"  Rows inserted:    {total_rows}")
    print(f"  With geometry:    {total_ok}")
    print(f"  No geometry:      {total_fail}  (stored with NULL geometry)")
    print(f"  Time:             {elapsed:.1f}s")
    print(f"  Database:         {DB_CONFIG['dbname']}.{SCHEMA}.{TABLE}")
    _sep("═")
    print("\nNext steps:")
    print("  1. Bounding box above should show ~512xxx, 5403xxx")
    print("  2. Regenerate pg2b3dm tiles")
    print("  3. Test alignment in Cesium viewer")


if __name__ == "__main__":
    main()