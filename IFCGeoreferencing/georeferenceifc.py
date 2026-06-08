"""
georeference_ifc.py  (v3 — robust transform)
============================================
Nexus3D — Automated IFC Georeferencing Pipeline
Stages 0–4: inspect IFC → measure IFC's OWN local footprint → query citydb
            footprint → fit full 2D rigid transform (rotation + origin)
            → store in PostGIS → inject IfcMapConversion into IFC

WHAT CHANGED vs v2 (the rotation fix):
  - OLD: rotation derived ONLY from the world footprint, assuming the IFC's
         local long axis = local +X (read from WCS RefDirection ≈ identity).
         This was an unverified assumption → wrong rotation for any building
         not modelled along local X. Translation used the footprint centroid
         as the IfcMapConversion origin, which is only correct if the IFC
         local origin == building centroid (it usually isn't → metres of shift).
  - NEW: derive_transform() tessellates the IFC's OWN footprint in local
         (project) coordinates, fits the rigid transform (R, T) that maps it
         onto the citydb footprint, and resolves the 90/180/270 ambiguity by
         picking the rotation with the highest polygon overlap (IoU).
         Translation is computed so the IFC local origin lands correctly:
             (E, N) = world_centroid - R * local_centroid

Usage:
    # Auto-match by rough location (preferred — the IFC carries no real location):
    python georeferenceifc.py --ifc HFT_Bau5.ifc --lat 48.7803 --lon 9.1716
    python georeferenceifc.py --ifc HFT_Bau5.ifc --lat 48.7803 --lon 9.1716 \
                              --radius 80 --topn 5 --min-iou 0.7 --dry-run

    # Direct lookup when you already know the building:
    python georeferenceifc.py --ifc HFT_Bau4_2025.04.22.ifc --objectid DEBWL52210005DwE
    python georeferenceifc.py --ifc HFT_Bau4_2025.04.22.ifc --feature-id 109496

    # Unattended batch (no interactive hint on low fit — exits non-zero instead):
    python georeferenceifc.py --ifc HFT_Bau5.ifc --lat 48.7803 --lon 9.1716 --no-prompt

WHAT CHANGED vs v3 (auto-match):
  - Seed by rough --lat/--lon: find_candidates() pulls the nearest citydb
    footprints; auto_match() footprint-fits each and keeps the highest IoU,
    deciding BOTH which building and whether the match is trustworthy.
  - IoU is now a hard confidence gate. Below --min-iou it falls back to an
    interactive hint (objectid or corrected lat,lon) and retries, or exits
    non-zero under --no-prompt.
  - IFC local footprint uses a concave outline (not convex hull) so L-shaped
    buildings disambiguate the 180° rotation correctly.

Requirements:
    pip install ifcopenshell psycopg2-binary shapely pyproj
"""

import argparse
import math
import os
import sys
import psycopg2
import ifcopenshell
import ifcopenshell.geom
from pyproj import Transformer
from shapely import wkt as shapely_wkt
from shapely.geometry import MultiPoint, MultiPolygon, Polygon
from shapely.affinity import affine_transform
from shapely.ops import unary_union

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────
DB_CONFIG = {
    "host":     "localhost",
    "port":     5432,
    "dbname":   "3DCity",
    "user":     "postgres",
    "password": os.getenv("PGPASSWORD", "Abhi2345@com"),
}

GEOID_UNDULATION = 47.2
OUTPUT_DIR = r"C:\Users\abhir\PycharmProjects\stuttgart_gis\IFCGeoreferencing"

# ── Auto-match defaults (overridable via CLI) ────────────────────────────────
DEFAULT_RADIUS_M  = 80      # candidate search radius around the seed lat/lon (m)
DEFAULT_TOPN      = 5       # how many nearest footprints to footprint-fit
DEFAULT_MIN_IOU   = 0.70    # confidence gate — below this we ask for a manual hint
FOOTPRINT_Z_FRAC  = 0.15    # use the lowest 15% (by Z) of slab geometry as the floor
FOOTPRINT_CLOSE_M = 0.05    # morphological close (m) to dissolve triangle-edge slivers


# ─────────────────────────────────────────────────────────────────────────────
# Coordinate helper
# ─────────────────────────────────────────────────────────────────────────────
def wgs84_to_utm32n(lat, lon):
    """Rough WGS84 (lat, lon) → EPSG:25832 (easting, northing) for seeding."""
    t = Transformer.from_crs("EPSG:4326", "EPSG:25832", always_xy=True)
    e, n = t.transform(lon, lat)
    return e, n


# ─────────────────────────────────────────────────────────────────────────────
# STAGE 0 — IFC inspection (diagnostics only — no longer used for rotation)
# ─────────────────────────────────────────────────────────────────────────────
def inspect_ifc_local_axis(ifc_model):
    print("\n── STAGE 0: IFC inspection (diagnostics) ───────────────────────")
    mcs = ifc_model.by_type("IfcMapConversion")
    if mcs:
        mc = mcs[0]
        print(f"  [found] Existing IfcMapConversion:")
        print(f"          E={mc.Eastings}, N={mc.Northings}, H={mc.OrthogonalHeight}")
        print(f"          XAxisAbscissa={mc.XAxisAbscissa}, XAxisOrdinate={mc.XAxisOrdinate}")
        print(f"          (will be replaced)")
    else:
        print("  [info]  No IfcMapConversion found — will create fresh")

    seen = set()
    for ctx in ifc_model.by_type("IfcGeometricRepresentationContext"):
        if ctx.ContextType == "Model" and ctx.id() not in seen:
            seen.add(ctx.id())
            if ctx.TrueNorth:
                tn = ctx.TrueNorth.DirectionRatios
                print(f"  [found] TrueNorth: X={tn[0]:.6f}, Y={tn[1]:.6f}")
            else:
                print("  [info]  No TrueNorth set")


# ─────────────────────────────────────────────────────────────────────────────
# STAGE 1 — Query citydb footprint
#
# The true building footprint is the 2D union of the LoD2 PolyhedralSurface
# faces, NOT the MultiLineString (which is a 3D wireframe sitting above ground).
# This CTE projects the solid to 2D and dissolves the faces into a footprint
# (Multi)Polygon. {filter} selects either a proximity bbox or a direct id;
# {order} / {limit} differ per call. The bbox `&&` uses the GIST index.
# ─────────────────────────────────────────────────────────────────────────────
_FOOTPRINT_SQL = """
    WITH pt AS (SELECT ST_SetSRID(ST_MakePoint(%(e)s, %(n)s), 25832) AS g),
    cand AS (
        SELECT f.objectid, f.id AS feature_id, gd.id AS geom_data_id,
               gd.geometry AS geom3d
        FROM citydb.geometry_data gd
        JOIN citydb.feature f ON gd.feature_id = f.id
        WHERE ST_GeometryType(gd.geometry) = 'ST_PolyhedralSurface'
          AND {filter}
    ),
    foot AS (
        SELECT c.objectid, c.feature_id, c.geom_data_id,
               ST_ZMin(c.geom3d) AS z_min,
               ST_CollectionExtract(u.poly2d, 3) AS poly2d
        FROM cand c
        CROSS JOIN LATERAL (
            SELECT ST_UnaryUnion(ST_Collect(ST_MakeValid(d.geom))) AS poly2d
            FROM ST_Dump(ST_Force2D(c.geom3d)) d
        ) u
    )
    SELECT objectid, feature_id, geom_data_id, ST_AsText(poly2d),
           ROUND(z_min::numeric, 3),
           ST_Distance(ST_Centroid(poly2d), (SELECT g FROM pt)) AS dist
    FROM foot
    WHERE poly2d IS NOT NULL AND NOT ST_IsEmpty(poly2d)
    {order}
    {limit};
"""


def _row_to_candidate(row, force_dist=None):
    obj_id, feat_id, geom_data_id, wkt, z_min, dist = row
    return {"objectid": obj_id, "feature_id": feat_id, "geom_data_id": geom_data_id,
            "wkt": wkt, "z_min": float(z_min),
            "dist": float(force_dist if force_dist is not None else dist)}


def fetch_by_id(conn, objectid=None, feature_id=None):
    """
    Fetch one citydb building footprint by objectid or feature_id (direct
    lookup). Picks the largest-area solid if a feature has several. Returns a
    candidate dict, or raises ValueError if none found.
    """
    print("\n── STAGE 1: Querying citydb footprint (direct) ─────────────────")

    if feature_id is not None:
        print(f"  [mode]  Direct feature_id lookup: {feature_id}")
        flt, key = "f.id = %(fid)s", {"fid": feature_id}
    else:
        print(f"  [mode]  objectid lookup: {objectid}")
        flt, key = "f.objectid = %(oid)s", {"oid": objectid}

    sql = _FOOTPRINT_SQL.format(
        filter=flt, order="ORDER BY ST_Area(poly2d) DESC", limit="LIMIT 1")
    params = {"e": 0, "n": 0, **key}

    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()

    if not row:
        raise ValueError(
            f"No PolyhedralSurface footprint found in citydb "
            f"(objectid={objectid!r}, feature_id={feature_id})."
        )

    cand = _row_to_candidate(row, force_dist=0.0)   # dist meaningless for direct lookup
    print(f"  objectid:            {cand['objectid']}")
    print(f"  feature_id:          {cand['feature_id']}")
    print(f"  geometry_data_id:    {cand['geom_data_id']}")
    print(f"  z_min (orthometric): {cand['z_min']} m")
    return cand


def find_candidates(conn, easting, northing, radius_m, topn):
    """
    Find the nearest citydb building footprints around a seed (E, N), as the
    2D-dissolved PolyhedralSurface footprint per feature, ordered by centroid
    distance, capped at `topn`. Each item is a candidate dict.
    """
    print("\n── STAGE 1: Auto-matching citydb footprints ────────────────────")
    print(f"  Seed (EPSG:25832):   E={easting:.3f}, N={northing:.3f}")
    print(f"  Search radius:       {radius_m} m   (top {topn} by distance)")

    sql = _FOOTPRINT_SQL.format(
        filter="gd.geometry && ST_Expand((SELECT g FROM pt), %(r)s)",
        order="ORDER BY dist ASC",
        limit="LIMIT %(lim)s")
    # Over-fetch then dedupe per feature in Python (a feature may have several
    # PolyhedralSurface rows, e.g. building parts); keep the nearest per objectid.
    params = {"e": easting, "n": northing, "r": radius_m, "lim": topn * 4}

    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

    candidates, seen = [], set()
    for row in rows:
        cand = _row_to_candidate(row)
        if cand["feature_id"] in seen:
            continue
        seen.add(cand["feature_id"])
        candidates.append(cand)
        if len(candidates) >= topn:
            break

    print(f"  Candidates found:    {len(candidates)}")
    for c in candidates:
        print(f"    • {c['objectid']:<20} dist={c['dist']:6.1f} m  z_min={c['z_min']:.2f}")
    return candidates


def auto_match(local_poly, candidates, min_iou, prior_deg=None):
    """
    Footprint-fit the (cached) IFC local outline against every candidate and
    keep the highest-IoU match. This decides BOTH which building and whether
    the match is trustworthy. `prior_deg` (TrueNorth) disambiguates each fit's
    rotation. Returns the winning candidate dict (augmented with the fit), or
    None if there were no candidates.
    """
    print("\n── STAGE 2: Fitting IFC footprint to candidates ────────────────")
    best = None
    for c in candidates:
        try:
            world_poly = _polygon_from_wkt(c["wkt"])
            fit = fit_footprint(local_poly, world_poly, prior_deg=prior_deg, verbose=False)
        except Exception as e:
            print(f"    • {c['objectid']:<20} fit failed: {e}")
            continue
        gate = "✓" if fit["iou"] >= min_iou else " "
        print(f"  {gate} {c['objectid']:<20} dist={c['dist']:6.1f} m  "
              f"IoU={fit['iou']:.3f}  rot={fit['theta_deg']:7.2f}°  "
              f"ratio={fit['ratio']:.3f}")
        merged = {**c, **fit}
        if best is None or fit["iou"] > best["iou"]:
            best = merged

    if best is not None:
        print(f"  ► Best match: {best['objectid']}  IoU={best['iou']:.3f}  "
              f"rot={best['theta_deg']:.3f}°")
    return best


# ─────────────────────────────────────────────────────────────────────────────
# Geometry helpers
# ─────────────────────────────────────────────────────────────────────────────
def _polygon_from_wkt(footprint_wkt):
    """
    Load a citydb footprint WKT into a 2D (Multi)Polygon.

    The footprint comes from unioning the PolyhedralSurface faces in 2D
    (Polygon / MultiPolygon), but we still tolerate ring-only geometries
    (MultiLineString / LineString) for backward compatibility.
    """
    g = shapely_wkt.loads(footprint_wkt)
    gt = g.geom_type

    if gt in ("Polygon", "MultiPolygon"):
        poly = g
    elif gt in ("MultiLineString", "LineString"):
        coords = []
        if gt == "MultiLineString":
            for line in g.geoms:
                coords.extend([(c[0], c[1]) for c in line.coords])
        else:
            coords = [(c[0], c[1]) for c in g.coords]
        if coords[0] == coords[-1]:
            coords = coords[:-1]
        poly = Polygon(coords)
    elif gt == "GeometryCollection":
        polys = [p for p in g.geoms if p.geom_type in ("Polygon", "MultiPolygon")]
        if not polys:
            raise ValueError("Footprint GeometryCollection has no polygonal part.")
        poly = polys[0] if len(polys) == 1 else MultiPolygon(
            [pp for p in polys for pp in (p.geoms if p.geom_type == "MultiPolygon" else [p])])
    else:
        raise ValueError(f"Unexpected footprint geom type: {gt}")

    if not poly.is_valid:
        poly = poly.buffer(0)   # repair self-intersections
    return poly


def _long_axis(poly):
    """Return (bearing_rad, long_len, short_len) of the polygon's MBR long axis."""
    mbr = poly.minimum_rotated_rectangle
    c = list(mbr.exterior.coords)
    e1 = (c[1][0] - c[0][0], c[1][1] - c[0][1])
    e2 = (c[2][0] - c[1][0], c[2][1] - c[1][1])
    l1, l2 = math.hypot(*e1), math.hypot(*e2)
    long_edge = e1 if l1 >= l2 else e2
    return math.atan2(long_edge[1], long_edge[0]), max(l1, l2), min(l1, l2)


def _tessellate(ifc_model, classes):
    """
    Tessellate the given IFC classes in PROJECT (local) coordinates — the exact
    space IfcMapConversion operates on, BEFORE map conversion.
    Returns (verts, tris): a flat vertex list [(x,y,z), …] and triangles as
    index triples [(i,j,k), …].

    NOTE: in ifcopenshell 0.8.x the .verts / .faces buffers are tied to the
    shape object's lifetime, so the shape reference must be held while reading
    them (a chained create_shape(...).geometry.verts returns an empty buffer).
    """
    settings = ifcopenshell.geom.settings()
    settings.set(settings.USE_WORLD_COORDS, True)

    verts, tris = [], []
    for cls in classes:
        for el in ifc_model.by_type(cls):
            try:
                shape = ifcopenshell.geom.create_shape(settings, el)
                v = shape.geometry.verts
                f = shape.geometry.faces
            except Exception:
                continue
            base = len(verts)
            verts.extend((v[i], v[i + 1], v[i + 2]) for i in range(0, len(v), 3))
            tris.extend((base + f[i], base + f[i + 1], base + f[i + 2])
                        for i in range(0, len(f), 3))
    return verts, tris


def _footprint_from_tris(verts, tris, z_frac):
    """
    Dissolve the mesh triangles whose vertices lie in the lowest `z_frac` of the
    Z range into a single 2D footprint (Multi)Polygon — i.e. the floor outline.
    Returns None if nothing usable is produced.
    """
    if not verts or not tris:
        return None
    zs = [p[2] for p in verts]
    cut = min(zs) + z_frac * (max(zs) - min(zs))

    faces = []
    for a, b, c in tris:
        pa, pb, pc = verts[a], verts[b], verts[c]
        if pa[2] > cut or pb[2] > cut or pc[2] > cut:
            continue
        tri = Polygon([(pa[0], pa[1]), (pb[0], pb[1]), (pc[0], pc[1])])
        if tri.is_valid and tri.area > 1e-6:
            faces.append(tri)
    if not faces:
        return None

    poly = unary_union(faces)
    # Close hairline gaps between adjacent triangle edges, then heal slivers.
    poly = poly.buffer(FOOTPRINT_CLOSE_M).buffer(-FOOTPRINT_CLOSE_M)
    if poly.geom_type == "GeometryCollection":
        polys = [g for g in poly.geoms if g.geom_type in ("Polygon", "MultiPolygon")]
        poly = unary_union(polys) if polys else None
    if poly is None or poly.is_empty or poly.area <= 0:
        return None
    return poly


def _ifc_local_footprint(ifc_model):
    """
    Derive the IFC's OWN floor footprint in local/project coordinates.

    Primary: dissolve the lowest IfcSlab triangles (the ground-floor slab) into
    a true outline — this matches the cadastral/LoD2 footprint closely (high
    IoU) and preserves L-shapes for an honest overlap score. Falls back to the
    convex hull of structural points if slab tessellation is unavailable.
    """
    verts, tris = _tessellate(ifc_model, ("IfcSlab",))
    poly = _footprint_from_tris(verts, tris, FOOTPRINT_Z_FRAC)
    if poly is not None:
        return poly

    # Fallback — convex hull of any structural geometry we can reach.
    print("  ⚠  Slab footprint unavailable — falling back to convex hull.")
    verts, _ = _tessellate(ifc_model, ("IfcSlab", "IfcWall", "IfcColumn", "IfcRoof"))
    if len(verts) < 3:
        raise RuntimeError(
            "Could not tessellate any IFC geometry to derive the local footprint. "
            "Check the IFC contains IfcSlab/IfcWall geometry."
        )
    return MultiPoint([(p[0], p[1]) for p in verts]).convex_hull


def true_north_map_angle(ifc_model):
    """
    Read the model's TrueNorth and return the IfcMapConversion rotation angle
    (degrees, CCW from East) it implies — i.e. the angle that rotates local +X
    onto world East. TrueNorth points to world +Y (north, bearing 90°), so:
        theta = 90° - atan2(TrueNorth.y, TrueNorth.x)
    Returns None if no TrueNorth is set. This is unambiguous over the full 360°
    and is used to resolve the footprint fit's 90°/180° symmetry.
    """
    for ctx in ifc_model.by_type("IfcGeometricRepresentationContext"):
        if ctx.ContextType == "Model" and ctx.TrueNorth:
            tx, ty = ctx.TrueNorth.DirectionRatios[:2]
            return (90.0 - math.degrees(math.atan2(ty, tx))) % 360.0
    return None


def _ang_dist(a, b):
    """Smallest absolute angular difference in degrees."""
    return abs((a - b + 180.0) % 360.0 - 180.0)


# ─────────────────────────────────────────────────────────────────────────────
# STAGE 2 — Derive full 2D rigid transform (rotation + origin)
# ─────────────────────────────────────────────────────────────────────────────
def fit_footprint(local_poly, world_poly, prior_deg=None, verbose=True):
    """
    Core rigid-fit: map the IFC local footprint onto a world footprint.
        world = (E, N) + R(theta) * local
    Tests all 4 quarter turns. If `prior_deg` (from TrueNorth) is given, the
    rotation closest to it is chosen — this resolves the 90°/180° symmetry that
    IoU alone can't break for rectangular/L footprints. Otherwise the highest
    IoU wins. Returns a dict: {E, N, cos, sin, theta_deg, iou, ratio}.
    """
    world_cx, world_cy = world_poly.centroid.x, world_poly.centroid.y
    world_bearing, world_len, world_short = _long_axis(world_poly)

    local_cx, local_cy = local_poly.centroid.x, local_poly.centroid.y
    local_bearing, local_len, local_short = _long_axis(local_poly)

    ratio = world_len / local_len if local_len else float("nan")

    if verbose:
        print(f"  World footprint centroid: E={world_cx:.3f}, N={world_cy:.3f}")
        print(f"  World long-axis bearing:  {math.degrees(world_bearing):8.3f}°  "
              f"(long {world_len:.1f} m, short {world_short:.1f} m)")
        print(f"  IFC local centroid:       x={local_cx:.3f}, y={local_cy:.3f}")
        print(f"  IFC local long-axis:      {math.degrees(local_bearing):8.3f}°  "
              f"(long {local_len:.1f} m, short {local_short:.1f} m)")
        print(f"  Edge-length ratio (world/local): {ratio:.4f}  (expect ≈ 1.0)")
        if not (0.9 < ratio < 1.1):
            print("  ⚠  Ratio far from 1.0 — possible unit mismatch (IFC in mm?) "
                  "or the citydb footprint isn't this building.")
        if prior_deg is not None:
            print(f"  TrueNorth prior:          {prior_deg:.3f}°  (disambiguates rotation)")
        print(f"  Testing candidate rotations (overlap test):")

    base = world_bearing - local_bearing
    cands = []
    for k in range(4):
        theta = base + k * (math.pi / 2)
        cos_t, sin_t = math.cos(theta), math.sin(theta)
        # Translation so local centroid maps onto world centroid:
        #   world = (E,N) + R*local  ⇒  (E,N) = world_c - R*local_c
        E = world_cx - (cos_t * local_cx - sin_t * local_cy)
        N = world_cy - (sin_t * local_cx + cos_t * local_cy)
        # shapely affine matrix: [a, b, d, e, xoff, yoff] for R = [[cos,-sin],[sin,cos]]
        moved = affine_transform(local_poly, [cos_t, -sin_t, sin_t, cos_t, E, N])
        inter = moved.intersection(world_poly).area
        union = moved.union(world_poly).area
        iou = inter / union if union else 0.0
        theta_deg = math.degrees(theta) % 360
        cands.append({"theta": theta, "theta_deg": theta_deg, "E": E, "N": N,
                      "iou": iou, "cos": cos_t, "sin": sin_t})
        if verbose:
            mark = ""
            if prior_deg is not None and _ang_dist(theta_deg, prior_deg) < 45:
                mark = "  ← TrueNorth"
            print(f"    θ = {theta_deg:7.2f}°   IoU = {iou:.3f}{mark}")

    if prior_deg is not None:
        best = min(cands, key=lambda c: _ang_dist(c["theta_deg"], prior_deg))
    else:
        best = max(cands, key=lambda c: c["iou"])

    best["ratio"] = ratio
    return best


def derive_transform(ifc_model, footprint_wkt, prior_deg=None):
    """
    Fit the rigid transform mapping the IFC local footprint onto a single citydb
    world footprint (verbose — used for the direct --objectid path).
    Returns (E, N, xaxis_abscissa, xaxis_ordinate, bearing_deg, iou).
    """
    print("\n── STAGE 2: Deriving transform (rotation + origin) ─────────────")

    world_poly = _polygon_from_wkt(footprint_wkt)
    local_poly = _ifc_local_footprint(ifc_model)   # IFC's OWN footprint, local coords

    best = fit_footprint(local_poly, world_poly, prior_deg=prior_deg, verbose=True)

    print(f"  ► Selected rotation: {best['theta_deg']:.3f}°   IoU = {best['iou']:.3f}")
    print(f"    XAxisAbscissa: {best['cos']:.6f}")
    print(f"    XAxisOrdinate: {best['sin']:.6f}")
    print(f"    Origin (E,N):  {best['E']:.3f}, {best['N']:.3f}")

    if best["iou"] < 0.6:
        print("  ⚠  LOW OVERLAP (<0.6) — the result is unreliable. Likely causes:")
        print("     • wrong citydb footprint matched to this IFC")
        print("     • IFC footprint shape differs substantially from citydb")
        print("     • unit mismatch (see ratio above)")

    return best["E"], best["N"], best["cos"], best["sin"], best["theta_deg"], best["iou"]


# ─────────────────────────────────────────────────────────────────────────────
# STAGE 3 — Store georef params in PostGIS
# ─────────────────────────────────────────────────────────────────────────────
def store_georef_params(conn, objectid, feature_id, geom_data_id,
                        eastings, northings, orthogonal_height,
                        xaxis_abscissa, xaxis_ordinate, bearing_deg, iou,
                        ifc_filename):
    print("\n── STAGE 3: Storing georef params in PostGIS ───────────────────")

    # Schema matches what the Node backend reads (long_axis_bearing); fit_iou is
    # added for the confidence audit. The ALTERs migrate any pre-existing table.
    create_sql = """
        CREATE TABLE IF NOT EXISTS nexus3d.building_georef (
            id                SERIAL PRIMARY KEY,
            objectid          TEXT NOT NULL UNIQUE,
            feature_id        BIGINT,
            geometry_data_id  BIGINT,
            eastings          DOUBLE PRECISION,
            northings         DOUBLE PRECISION,
            orthogonal_height DOUBLE PRECISION,
            xaxis_abscissa    DOUBLE PRECISION,
            xaxis_ordinate    DOUBLE PRECISION,
            long_axis_bearing DOUBLE PRECISION,
            fit_iou           DOUBLE PRECISION,
            ifc_source_file   TEXT,
            georef_ifc_file   TEXT,
            derived_at        TIMESTAMPTZ DEFAULT now()
        );
        ALTER TABLE nexus3d.building_georef
            ADD COLUMN IF NOT EXISTS long_axis_bearing DOUBLE PRECISION;
        ALTER TABLE nexus3d.building_georef
            ADD COLUMN IF NOT EXISTS fit_iou DOUBLE PRECISION;
    """
    upsert_sql = """
        INSERT INTO nexus3d.building_georef
            (objectid, feature_id, geometry_data_id, eastings, northings,
             orthogonal_height, xaxis_abscissa, xaxis_ordinate, long_axis_bearing,
             fit_iou, ifc_source_file, georef_ifc_file, derived_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
        ON CONFLICT (objectid) DO UPDATE SET
            feature_id=EXCLUDED.feature_id, geometry_data_id=EXCLUDED.geometry_data_id,
            eastings=EXCLUDED.eastings, northings=EXCLUDED.northings,
            orthogonal_height=EXCLUDED.orthogonal_height,
            xaxis_abscissa=EXCLUDED.xaxis_abscissa, xaxis_ordinate=EXCLUDED.xaxis_ordinate,
            long_axis_bearing=EXCLUDED.long_axis_bearing, fit_iou=EXCLUDED.fit_iou,
            ifc_source_file=EXCLUDED.ifc_source_file, georef_ifc_file=EXCLUDED.georef_ifc_file,
            derived_at=now();
    """
    base_name   = os.path.splitext(os.path.basename(ifc_filename))[0]
    georef_file = base_name + "_georef.ifc"
    with conn.cursor() as cur:
        cur.execute(create_sql)
        cur.execute(upsert_sql, (
            objectid, feature_id, geom_data_id, eastings, northings,
            orthogonal_height, xaxis_abscissa, xaxis_ordinate, bearing_deg,
            iou, os.path.basename(ifc_filename), georef_file
        ))
    conn.commit()
    print(f"  ✅ Upserted nexus3d.building_georef for objectid={objectid}")
    return georef_file


# ─────────────────────────────────────────────────────────────────────────────
# STAGE 4 — Inject IfcMapConversion
# ─────────────────────────────────────────────────────────────────────────────
def inject_map_conversion(ifc_model, ifc_path, objectid,
                          eastings, northings, orthogonal_height,
                          xaxis_abscissa, xaxis_ordinate):
    print("\n── STAGE 4: Injecting IfcMapConversion ─────────────────────────")

    for etype in ("IfcMapConversion", "IfcProjectedCRS"):
        for e in ifc_model.by_type(etype):
            ifc_model.remove(e)
            print(f"  [removed] existing {etype}")

    crs = ifc_model.createIfcProjectedCRS(
        Name="EPSG:25832", Description="ETRS89 / UTM Zone 32N",
        GeodeticDatum="ETRS89", VerticalDatum="DHHN2016",
        MapProjection="UTM", MapZone="32N",
        MapUnit=ifc_model.createIfcSIUnit(None, "LENGTHUNIT", None, "METRE"),
    )

    contexts = [c for c in ifc_model.by_type("IfcGeometricRepresentationContext")
                if c.ContextType == "Model"] or ifc_model.by_type("IfcGeometricRepresentationContext")
    if not contexts:
        raise ValueError("No IfcGeometricRepresentationContext found.")

    ifc_model.createIfcMapConversion(
        SourceCRS=contexts[0], TargetCRS=crs,
        Eastings=eastings, Northings=northings, OrthogonalHeight=orthogonal_height,
        XAxisAbscissa=xaxis_abscissa, XAxisOrdinate=xaxis_ordinate, Scale=1.0,
    )
    print(f"  Eastings:         {eastings:.3f}")
    print(f"  Northings:        {northings:.3f}")
    print(f"  OrthogonalHeight: {orthogonal_height:.3f}")
    print(f"  XAxisAbscissa:    {xaxis_abscissa:.6f}")
    print(f"  XAxisOrdinate:    {xaxis_ordinate:.6f}")

    sites = ifc_model.by_type("IfcSite")
    if sites:
        tag = f"[citydb_objectid:{objectid}]"
        desc = sites[0].Description or ""
        if tag not in desc:
            sites[0].Description = (desc + " " + tag).strip()
        print(f"  ✅ Embedded objectid in IfcSite.Description: {tag}")

    base_name   = os.path.splitext(os.path.basename(ifc_path))[0]
    output_path = os.path.join(OUTPUT_DIR, base_name + "_georef.ifc")
    ifc_model.write(output_path)
    print(f"  ✅ Saved: {output_path}")
    return output_path


# ─────────────────────────────────────────────────────────────────────────────
# Match resolution (seed → candidates → fit → confidence gate)
# ─────────────────────────────────────────────────────────────────────────────
def _prompt_for_hint():
    """
    Ask for a manual correction after a low-confidence fit.
    Returns one of:
        ("objectid", value) | ("latlon", (lat, lon)) | ("abort", None)
    """
    print("\n  ── Manual hint needed ─────────────────────────────────────")
    print("  Enter a correction and press Enter:")
    print("    • an objectid           e.g.  DEBWL52210005DwE")
    print("    • a rough 'lat,lon'     e.g.  48.7803, 9.1716")
    print("    • blank to abort")
    raw = input("  > ").strip()
    if not raw:
        return ("abort", None)
    if "," in raw:
        parts = [s.strip() for s in raw.split(",")]
        try:
            lat, lon = float(parts[0]), float(parts[1])
            return ("latlon", (lat, lon))
        except (ValueError, IndexError):
            print("  ⚠  Could not parse as lat,lon — treating as objectid.")
    return ("objectid", raw)


def resolve_match(conn, local_poly, args, prior_deg=None):
    """
    Resolve the seed (objectid / feature_id / lat,lon) to a best-fitting citydb
    footprint. On a low-IoU fit, fall back to an interactive hint and retry
    (unless --no-prompt). `prior_deg` (TrueNorth) disambiguates rotation.
    Returns the winning candidate dict (with the fit merged in) or None if the
    user aborts / no match clears the gate.
    """
    objectid    = args.objectid
    feature_id  = args.feature_id
    lat, lon    = args.lat, args.lon

    while True:
        # ── Build candidate set from the current seed ────────────────────────
        if objectid or feature_id:
            try:
                cand = fetch_by_id(conn, objectid=objectid, feature_id=feature_id)
            except ValueError as e:
                print(f"  ❌ {e}")
                candidates = []
            else:
                print("\n── STAGE 2: Deriving transform (rotation + origin) ─────────────")
                world_poly = _polygon_from_wkt(cand["wkt"])
                fit = fit_footprint(local_poly, world_poly,
                                    prior_deg=prior_deg, verbose=True)
                print(f"  ► Selected rotation: {fit['theta_deg']:.3f}°   "
                      f"IoU = {fit['iou']:.3f}")
                print(f"    XAxisAbscissa: {fit['cos']:.6f}")
                print(f"    XAxisOrdinate: {fit['sin']:.6f}")
                print(f"    Origin (E,N):  {fit['E']:.3f}, {fit['N']:.3f}")
                best = {**cand, **fit}
                candidates = [best]
        else:
            e_seed, n_seed = wgs84_to_utm32n(lat, lon)
            candidates = find_candidates(conn, e_seed, n_seed,
                                         args.radius, args.topn)
            best = (auto_match(local_poly, candidates, args.min_iou, prior_deg=prior_deg)
                    if candidates else None)

        # ── Confidence gate ──────────────────────────────────────────────────
        if candidates and best is not None and best["iou"] >= args.min_iou:
            return best

        if not candidates:
            print(f"\n  ⚠  No footprint candidates for the current seed "
                  f"(radius {args.radius} m).")
        else:
            print(f"\n  ⚠  Best IoU {best['iou']:.3f} is below the gate "
                  f"({args.min_iou:.2f}) — match not trusted.")

        if args.no_prompt:
            print("  --no-prompt set → giving up on this building.")
            return None

        kind, value = _prompt_for_hint()
        if kind == "abort":
            print("  Aborted by user.")
            return None
        # reset seed, apply the correction, loop again
        objectid, feature_id, lat, lon = None, None, None, None
        if kind == "objectid":
            objectid = value
        else:
            lat, lon = value


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────
def main():
    p = argparse.ArgumentParser(description="Nexus3D — Automated IFC Georeferencing (v4)")
    p.add_argument("--ifc", required=True)
    p.add_argument("--objectid", default=None,
                   help="Direct citydb objectid (skips auto-match).")
    p.add_argument("--feature-id", type=int, default=None, dest="feature_id",
                   help="Direct citydb feature_id (skips auto-match).")
    p.add_argument("--lat", type=float, default=None,
                   help="Rough WGS84 latitude seed for auto-match.")
    p.add_argument("--lon", type=float, default=None,
                   help="Rough WGS84 longitude seed for auto-match.")
    p.add_argument("--radius", type=float, default=DEFAULT_RADIUS_M,
                   help=f"Candidate search radius in m (default {DEFAULT_RADIUS_M}).")
    p.add_argument("--topn", type=int, default=DEFAULT_TOPN,
                   help=f"Nearest footprints to fit (default {DEFAULT_TOPN}).")
    p.add_argument("--min-iou", type=float, default=DEFAULT_MIN_IOU, dest="min_iou",
                   help=f"Confidence gate on fit IoU (default {DEFAULT_MIN_IOU}).")
    p.add_argument("--no-prompt", action="store_true", dest="no_prompt",
                   help="Never ask for a manual hint — exit non-zero on low fit "
                        "(for unattended batch runs).")
    p.add_argument("--height", type=float, default=None,
                   help="Override orthometric height (m). Defaults to citydb z_min.")
    p.add_argument("--dry-run", action="store_true",
                   help="Run match + fit only — no writes.")
    args = p.parse_args()

    has_direct = bool(args.objectid or args.feature_id)
    has_seed   = args.lat is not None and args.lon is not None
    if not has_direct and not has_seed:
        p.error("Provide --objectid, --feature-id, or both --lat and --lon.")
    if not os.path.exists(args.ifc):
        print(f"ERROR: IFC file not found: {args.ifc}")
        sys.exit(1)

    print("=" * 62)
    print("  Nexus3D — Automated IFC Georeferencing (v4 auto-match)")
    print(f"  IFC:        {args.ifc}")
    if has_direct:
        print(f"  seed:       direct (objectid={args.objectid}, "
              f"feature_id={args.feature_id})")
    else:
        print(f"  seed:       lat/lon ({args.lat}, {args.lon})  "
              f"radius={args.radius} m  top{args.topn}  min_iou={args.min_iou}")
    if args.dry_run:
        print("  mode:       DRY RUN")
    print("=" * 62)

    print("\nLoading IFC model…")
    ifc_model = ifcopenshell.open(args.ifc)
    print(f"  Schema: {ifc_model.schema}")
    print(f"  IfcProduct count: {len(ifc_model.by_type('IfcProduct'))}")

    inspect_ifc_local_axis(ifc_model)

    print("\nConnecting to PostGIS…")
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        print("  ✅ Connected")
    except Exception as e:
        print(f"  ❌ Connection failed: {e}")
        sys.exit(1)

    try:
        # IFC local footprint is tessellated once and reused across all candidates.
        print("\nTessellating IFC local footprint…")
        local_poly = _ifc_local_footprint(ifc_model)

        prior_deg = true_north_map_angle(ifc_model)
        if prior_deg is not None:
            print(f"  TrueNorth rotation prior: {prior_deg:.3f}° (CCW from East)")
        else:
            print("  No TrueNorth in IFC — rotation will rely on footprint IoU alone.")

        best = resolve_match(conn, local_poly, args, prior_deg=prior_deg)
        if best is None:
            print("\n  ❌ No trusted match — nothing written.")
            sys.exit(2)

        objectid   = best["objectid"] or f"feature_id_{best['feature_id']}"
        E, N       = best["E"], best["N"]
        xabs, xord = best["cos"], best["sin"]
        bearing_deg, iou = best["theta_deg"], best["iou"]

        ortho_h = args.height if args.height is not None else best["z_min"]
        print(f"\n  Matched objectid: {objectid}")
        print(f"  OrthogonalHeight: {ortho_h:.3f} m (orthometric) "
              f"→ {ortho_h + GEOID_UNDULATION:.3f} m ellipsoidal")

        if args.dry_run:
            print("\n── DRY RUN — no writes ─────────────────────────────────────")
            print(f"  objectid={objectid}")
            print(f"  E={E:.3f}  N={N:.3f}  H={ortho_h:.3f}")
            print(f"  XAxisAbscissa={xabs:.6f}  XAxisOrdinate={xord:.6f}")
            print(f"  rotation={bearing_deg:.3f}°  IoU={iou:.3f}")
            return

        store_georef_params(conn, objectid, best["feature_id"], best["geom_data_id"],
                            E, N, ortho_h, xabs, xord, bearing_deg, iou, args.ifc)
        out = inject_map_conversion(ifc_model, args.ifc, objectid,
                                    E, N, ortho_h, xabs, xord)
    finally:
        conn.close()

    print("\n" + "=" * 62)
    print("  ✅ Georeferencing complete.")
    print(f"  Output IFC: {out}")
    print("  Next: run ifc_to_postgis.py → regenerate tiles → check Cesium")
    print("=" * 62)


if __name__ == "__main__":
    main()