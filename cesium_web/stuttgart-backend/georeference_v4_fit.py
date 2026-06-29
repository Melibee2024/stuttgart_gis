"""
georeference_v4_fit.py
======================
Fix the IFC rotation/position for hft_db (3DCityDB v4).

The georef IFC file shipped with an *identity* IfcMapConversion (a=1, b=0), so
ifc_to_postgis.py placed the building with no rotation and a wrong origin —
rotated and ~31 m off from its cadastral footprint.

This script reuses the proven footprint-fit from georeferenceifc.py:
  1. Tessellate the IFC's OWN ground-floor (lowest IfcSlab) outline in local
     (project) coordinates.
  2. Pull the citydb cadastral footprint (v4: surface_geometry.solid_geometry).
  3. Fit the rigid transform (rotation θ + origin E,N) by IoU over 4 quarter
     turns, disambiguated by IFC TrueNorth when present.
  4. Apply the correction to the EXISTING nexus3d.ifc_elements geometry with a
     single PostGIS ST_Affine (the current geometry was stored as project+origin0
     with origin0 = the identity IfcMapConversion's E/N, so we can recover the
     local frame and re-place it correctly without re-tessellating everything).
  5. Update nexus3d.building_georef with the fitted params.

Usage:
    python georeference_v4_fit.py            # dry run — prints the fit only
    python georeference_v4_fit.py --apply    # apply ST_Affine + update georef

Requirements: pip install ifcopenshell psycopg2-binary shapely
"""

import os
import argparse
import math
import sys
import psycopg2
import ifcopenshell
import ifcopenshell.geom
from shapely import wkt as shapely_wkt
from shapely.geometry import MultiPoint, MultiPolygon, Polygon
from shapely.affinity import affine_transform
from shapely.ops import unary_union

# Load DB credentials from a local .env (next to this script) so secrets are
# never hardcoded/committed. python-dotenv is optional; if absent, real
# environment variables are used directly.
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
except ImportError:
    pass

IFC_PATH = r"C:\3dcitydb-4.4.2\stuttgart_gis\IFCGeoreferencing\HFT_Bau4_2025.04.22_georef.ifc"

DB_CONFIG = {
    "host":     os.environ.get("DB_HOST", "localhost"),
    "port":     int(os.environ.get("DB_PORT", "5432")),
    "dbname":   os.environ.get("DB_NAME", "hft_db"),
    "user":     os.environ.get("DB_USER", "postgres"),
    "password": os.environ.get("DB_PASSWORD"),
}
if not DB_CONFIG["password"]:
    sys.exit("DB_PASSWORD is not set. Add it to a .env file in this directory.")

# The identity-IfcMapConversion origin that ifc_to_postgis.py used to place the
# current geometry (current_world_xy = origin0 + local_xy).
ORIGIN0_E = 512614.70
ORIGIN0_N = 5403013.80

CITYDB_FEATURE_ID = 27898       # DEBW_52210005DwE — HFT Bau4 cadastral footprint
FOOTPRINT_Z_FRAC  = 0.15
FOOTPRINT_CLOSE_M = 0.05


# ─── IFC local footprint (verbatim logic from georeferenceifc.py) ──────────────
def _tessellate(ifc_model, classes):
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
    poly = poly.buffer(FOOTPRINT_CLOSE_M).buffer(-FOOTPRINT_CLOSE_M)
    if poly.geom_type == "GeometryCollection":
        polys = [g for g in poly.geoms if g.geom_type in ("Polygon", "MultiPolygon")]
        poly = unary_union(polys) if polys else None
    if poly is None or poly.is_empty or poly.area <= 0:
        return None
    return poly


def _ifc_local_footprint(ifc_model):
    verts, tris = _tessellate(ifc_model, ("IfcSlab",))
    poly = _footprint_from_tris(verts, tris, FOOTPRINT_Z_FRAC)
    if poly is not None:
        return poly
    print("  WARN slab footprint unavailable — convex hull fallback")
    verts, _ = _tessellate(ifc_model, ("IfcSlab", "IfcWall", "IfcColumn", "IfcRoof"))
    if len(verts) < 3:
        raise RuntimeError("Could not tessellate IFC geometry.")
    return MultiPoint([(p[0], p[1]) for p in verts]).convex_hull


def true_north_map_angle(ifc_model):
    for ctx in ifc_model.by_type("IfcGeometricRepresentationContext"):
        if ctx.ContextType == "Model" and ctx.TrueNorth:
            tx, ty = ctx.TrueNorth.DirectionRatios[:2]
            return (90.0 - math.degrees(math.atan2(ty, tx))) % 360.0
    return None


def _ang_dist(a, b):
    return abs((a - b + 180.0) % 360.0 - 180.0)


def _long_axis(poly):
    mbr = poly.minimum_rotated_rectangle
    c = list(mbr.exterior.coords)
    e1 = (c[1][0] - c[0][0], c[1][1] - c[0][1])
    e2 = (c[2][0] - c[1][0], c[2][1] - c[1][1])
    l1, l2 = math.hypot(*e1), math.hypot(*e2)
    long_edge = e1 if l1 >= l2 else e2
    return math.atan2(long_edge[1], long_edge[0]), max(l1, l2), min(l1, l2)


def fit_footprint(local_poly, world_poly, prior_deg=None):
    world_cx, world_cy = world_poly.centroid.x, world_poly.centroid.y
    world_bearing, world_len, world_short = _long_axis(world_poly)
    local_cx, local_cy = local_poly.centroid.x, local_poly.centroid.y
    local_bearing, local_len, local_short = _long_axis(local_poly)
    ratio = world_len / local_len if local_len else float("nan")

    print(f"  world footprint: centroid=({world_cx:.2f},{world_cy:.2f}) "
          f"bearing={math.degrees(world_bearing):.2f} long={world_len:.1f} short={world_short:.1f}")
    print(f"  IFC local foot : centroid=({local_cx:.2f},{local_cy:.2f}) "
          f"bearing={math.degrees(local_bearing):.2f} long={local_len:.1f} short={local_short:.1f}")
    print(f"  edge ratio world/local = {ratio:.3f} (expect ~1.0)")
    if prior_deg is not None:
        print(f"  TrueNorth prior: {prior_deg:.2f}")

    base = world_bearing - local_bearing
    cands = []
    for k in range(4):
        theta = base + k * (math.pi / 2)
        cos_t, sin_t = math.cos(theta), math.sin(theta)
        E = world_cx - (cos_t * local_cx - sin_t * local_cy)
        N = world_cy - (sin_t * local_cx + cos_t * local_cy)
        moved = affine_transform(local_poly, [cos_t, -sin_t, sin_t, cos_t, E, N])
        inter = moved.intersection(world_poly).area
        union = moved.union(world_poly).area
        iou = inter / union if union else 0.0
        theta_deg = math.degrees(theta) % 360
        mark = " <-TrueNorth" if (prior_deg is not None and _ang_dist(theta_deg, prior_deg) < 45) else ""
        print(f"    theta={theta_deg:7.2f}  IoU={iou:.3f}{mark}")
        cands.append({"theta_deg": theta_deg, "E": E, "N": N, "iou": iou,
                      "cos": cos_t, "sin": sin_t})

    if prior_deg is not None:
        best = min(cands, key=lambda c: _ang_dist(c["theta_deg"], prior_deg))
    else:
        best = max(cands, key=lambda c: c["iou"])
    best["ratio"] = ratio
    return best


def get_citydb_footprint(conn, feature_id):
    sql = """
        SELECT ST_AsText(ST_CollectionExtract(
                 ST_UnaryUnion(ST_Collect(ST_MakeValid(ST_Force2D(d.geom)))), 3))
        FROM citydb.surface_geometry sg
        CROSS JOIN LATERAL ST_Dump(sg.solid_geometry) d
        WHERE sg.cityobject_id = %s AND sg.solid_geometry IS NOT NULL;
    """
    with conn.cursor() as cur:
        cur.execute(sql, (feature_id,))
        row = cur.fetchone()
    if not row or not row[0]:
        raise ValueError(f"No citydb footprint for feature_id={feature_id}")
    g = shapely_wkt.loads(row[0])
    if not g.is_valid:
        g = g.buffer(0)
    return g


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="apply ST_Affine + update georef")
    args = ap.parse_args()

    print("Loading IFC:", IFC_PATH)
    ifc = ifcopenshell.open(IFC_PATH)
    print("  schema:", ifc.schema)

    print("Tessellating IFC local floor footprint…")
    local_poly = _ifc_local_footprint(ifc)
    prior_deg = true_north_map_angle(ifc)

    conn = psycopg2.connect(**DB_CONFIG)
    try:
        print(f"Querying citydb footprint (feature_id={CITYDB_FEATURE_ID})…")
        world_poly = get_citydb_footprint(conn, CITYDB_FEATURE_ID)

        print("\n── Fitting transform ───────────────────────────────")
        best = fit_footprint(local_poly, world_poly, prior_deg=prior_deg)
        theta_deg, cos_t, sin_t = best["theta_deg"], best["cos"], best["sin"]
        E_new, N_new, iou = best["E"], best["N"], best["iou"]
        print(f"\n  ► theta={theta_deg:.3f}  IoU={iou:.3f}  ratio={best['ratio']:.3f}")
        print(f"    cos={cos_t:.6f} sin={sin_t:.6f}  origin=({E_new:.3f},{N_new:.3f})")

        if iou < 0.5:
            print("  ⚠ LOW IoU — fit unreliable. Inspect before applying.")

        # ── Affine that re-places the EXISTING stored geometry ────────────────
        # stored = origin0 + project ; want new = origin_new + R*project
        #   new = R*stored + (origin_new - R*origin0)
        xoff = E_new - (cos_t * ORIGIN0_E - sin_t * ORIGIN0_N)
        yoff = N_new - (sin_t * ORIGIN0_E + cos_t * ORIGIN0_N)
        print("\n  ST_Affine coefficients (3D):")
        print(f"    a={cos_t:.8f} b={-sin_t:.8f} d={sin_t:.8f} e={cos_t:.8f}")
        print(f"    xoff={xoff:.4f} yoff={yoff:.4f}")

        if not args.apply:
            print("\n(dry run — pass --apply to write changes)")
            return

        with conn.cursor() as cur:
            cur.execute("""
                UPDATE nexus3d.ifc_elements
                SET geometry = ST_SetSRID(
                    ST_Affine(geometry,
                        %s, %s, 0,
                        %s, %s, 0,
                        0,  0,  1,
                        %s, %s, 0), 25832);
            """, (cos_t, -sin_t, sin_t, cos_t, xoff, yoff))
            n = cur.rowcount
            cur.execute("""
                UPDATE nexus3d.building_georef
                SET eastings=%s, northings=%s,
                    xaxis_abscissa=%s, xaxis_ordinate=%s,
                    long_axis_bearing=%s, fit_iou=%s, derived_at=now()
                WHERE feature_id=%s;
            """, (E_new, N_new, cos_t, sin_t, theta_deg, iou, CITYDB_FEATURE_ID))
        conn.commit()
        print(f"\n  ✅ Applied ST_Affine to {n} ifc_elements rows + updated building_georef.")
        print("  Next: regenerate tiles (POST /api/regenerate-tiles).")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
