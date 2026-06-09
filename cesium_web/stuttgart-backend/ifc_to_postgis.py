"""
ifc_to_postgis.py
=================
Reads the georeferenced IFC file, extracts every IFC element's 3D geometry,
applies the IfcMapConversion to get EPSG:25832 coordinates, and inserts each
element into nexus3d.ifc_elements.

Run once after nexus3d_setup.sql:
    python ifc_to_postgis.py

Requirements:
    pip install ifcopenshell psycopg2-binary
"""

import os
import sys
import math
import json
import psycopg2
import ifcopenshell
import ifcopenshell.geom

IFC_PATH = r"C:\3dcitydb-4.4.2\stuttgart_gis\IFCGeoreferencing\HFT_Bau4_2025.04.22_georef.ifc"

DB_CONFIG = {
    "host":     "localhost",
    "port":     5432,
    "dbname":   "hft_db",
    "user":     "postgres",
    "password": "gis2026",
}

# IFC classes to extract.  IfcStair and IfcRailing omitted (null/hairline geom).
TARGET_CLASSES = (
    "IfcWall", "IfcSlab", "IfcWindow", "IfcDoor",
    "IfcColumn", "IfcRoof", "IfcSpace",
)


# ─── IFC helpers ──────────────────────────────────────────────────────────────

def get_map_conversion(ifc_model):
    mcs = ifc_model.by_type("IfcMapConversion")
    if not mcs:
        raise RuntimeError("No IfcMapConversion found in the IFC file.")
    mc = mcs[0]
    return {
        "E": mc.Eastings,
        "N": mc.Northings,
        "H": mc.OrthogonalHeight,
        "a": mc.XAxisAbscissa if mc.XAxisAbscissa is not None else 1.0,
        "b": mc.XAxisOrdinate if mc.XAxisOrdinate is not None else 0.0,
    }


def apply_map_conversion(mc, verts):
    """
    Transform IFC project coordinates → EPSG:25832.
    Rotation is in XY only; Z is a pure translation.
        E' = E + a*x - b*y
        N' = N + b*x + a*y
        Z' = H + z
    """
    E, N, H, a, b = mc["E"], mc["N"], mc["H"], mc["a"], mc["b"]
    return [(E + a*x - b*y, N + b*x + a*y, H + z) for x, y, z in verts]


def get_storey(ifc_model, element):
    """Walk up containment relationships to find the storey name."""
    for rel in getattr(ifc_model, "by_type", lambda x: [])(
            "IfcRelContainedInSpatialStructure"):
        if element in rel.RelatedElements:
            container = rel.RelatingStructure
            if container.is_a("IfcBuildingStorey"):
                return container.Name or container.LongName or str(container.Elevation)
    return None


def get_attributes(element):
    """Collect IFC property sets as a flat dict."""
    attrs = {}
    for rel in getattr(element, "IsDefinedBy", []):
        if not rel.is_a("IfcRelDefinesByProperties"):
            continue
        pset = rel.RelatingPropertyDefinition
        if not pset.is_a("IfcPropertySet"):
            continue
        for prop in pset.HasProperties:
            if prop.is_a("IfcPropertySingleValue") and prop.NominalValue:
                attrs[f"{pset.Name}.{prop.Name}"] = str(prop.NominalValue.wrappedValue)
    return attrs


def tessellate_element(element):
    """
    Return (verts, faces) in IFC project coordinates, or (None, None) on failure.
    verts: list of (x, y, z); faces: list of (i, j, k) index triples.
    """
    settings = ifcopenshell.geom.settings()
    settings.set(settings.USE_WORLD_COORDS, True)
    try:
        shape = ifcopenshell.geom.create_shape(settings, element)
        v = shape.geometry.verts
        f = shape.geometry.faces
        if not v or not f:
            return None, None
        verts = [(v[i], v[i+1], v[i+2]) for i in range(0, len(v), 3)]
        faces = [(f[i], f[i+1], f[i+2]) for i in range(0, len(f), 3)]
        return verts, faces
    except Exception:
        return None, None


def faces_to_wkt(verts_world):
    """Build a WKT GEOMETRYCOLLECTIONZ from a flat list of triangles (groups of 3 verts)."""
    if not verts_world or len(verts_world) % 3 != 0:
        return None
    polys = []
    for i in range(0, len(verts_world), 3):
        a, b, c = verts_world[i], verts_world[i+1], verts_world[i+2]
        # close the ring
        coords = f"{a[0]:.4f} {a[1]:.4f} {a[2]:.4f}," \
                 f"{b[0]:.4f} {b[1]:.4f} {b[2]:.4f}," \
                 f"{c[0]:.4f} {c[1]:.4f} {c[2]:.4f}," \
                 f"{a[0]:.4f} {a[1]:.4f} {a[2]:.4f}"
        polys.append(f"POLYGON Z(({coords}))")
    if not polys:
        return None
    return "GEOMETRYCOLLECTION Z(" + ",".join(polys) + ")"


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    if not os.path.exists(IFC_PATH):
        print(f"ERROR: IFC file not found: {IFC_PATH}")
        sys.exit(1)

    print(f"Loading IFC: {IFC_PATH}")
    ifc_model = ifcopenshell.open(IFC_PATH)
    print(f"  Schema: {ifc_model.schema}")

    mc = get_map_conversion(ifc_model)
    print(f"  MapConversion: E={mc['E']:.2f} N={mc['N']:.2f} H={mc['H']:.2f} "
          f"a={mc['a']:.4f} b={mc['b']:.4f}")

    print("Connecting to PostGIS…")
    conn = psycopg2.connect(**DB_CONFIG)
    cur = conn.cursor()

    # Truncate so a re-run is idempotent.
    cur.execute("TRUNCATE nexus3d.ifc_elements RESTART IDENTITY;")

    inserted = skipped = 0
    for cls in TARGET_CLASSES:
        elements = ifc_model.by_type(cls)
        print(f"  {cls}: {len(elements)} elements")
        for el in elements:
            global_id = el.GlobalId
            name      = getattr(el, "Name", None)
            storey    = get_storey(ifc_model, el)
            attrs     = get_attributes(el)

            verts_local, faces = tessellate_element(el)
            if verts_local is None or not faces:
                skipped += 1
                continue

            # Flatten faces into consecutive vertex triples
            flat_world = []
            for fi, fj, fk in faces:
                flat_world.extend([verts_local[fi], verts_local[fj], verts_local[fk]])

            world = apply_map_conversion(mc, flat_world)
            wkt = faces_to_wkt(world)
            if wkt is None:
                skipped += 1
                continue

            zs = [v[2] for v in world]
            z_min = min(zs)
            z_max = max(zs)
            height_m = round(z_max - z_min, 4)

            cur.execute("""
                INSERT INTO nexus3d.ifc_elements
                    (global_id, ifc_class, name, storey,
                     z_min_ellipsoidal, z_max_ellipsoidal, element_height_m,
                     geometry, attributes)
                VALUES
                    (%s, %s, %s, %s,
                     %s, %s, %s,
                     ST_SetSRID(ST_GeomFromText(%s), 25832),
                     %s)
            """, (
                global_id, cls, name, storey,
                z_min, z_max, height_m,
                wkt,
                json.dumps(attrs) if attrs else None,
            ))
            inserted += 1

    conn.commit()
    cur.close()
    conn.close()

    total = inserted + skipped
    print(f"\nDone: {inserted}/{total} elements inserted, {skipped} skipped (no geometry).")
    print("nexus3d.ifc_elements is ready — run pg2b3dm to generate tiles.")


if __name__ == "__main__":
    main()
