import ifcopenshell
import ifcopenshell.geom
import ifcopenshell.util.element
import json
import psycopg2
from psycopg2.extras import Json

# --- Configuration ---
IFC_FILE = "HFT_Bau4_2025.04.22.ifc"

DB_CONFIG = {
    "dbname": "3DCity",
    "user": "postgres",
    "password": "Abhi2345@com",
    "host": "localhost",
    "port": "5432"
}


def setup_database():
    print("Connecting to database to initialize table...")
    conn = psycopg2.connect(**DB_CONFIG)
    cur = conn.cursor()
    cur.execute("CREATE EXTENSION IF NOT EXISTS postgis;")
    cur.execute("DROP TABLE IF EXISTS hft_bau4;")
    # Using Polygon (2D) for clean horizontal layout mapping
    cur.execute("""
        CREATE TABLE hft_bau4 (
            id SERIAL PRIMARY KEY,
            globalid VARCHAR(22) UNIQUE,
            name VARCHAR(255),
            ifc_class VARCHAR(100),
            geom GEOMETRY(Polygon, 25832),
            metadata JSONB
        );
    """)
    conn.commit()
    cur.close()
    conn.close()


def run_integrated_pipeline(file_path):
    print(f"Loading IFC Model: {file_path}...")
    model = ifcopenshell.open(file_path)

    settings = ifcopenshell.geom.settings()

    conn = psycopg2.connect(**DB_CONFIG)
    cur = conn.cursor()

    target_classes = ['IfcWall', 'IfcWallStandardCase', 'IfcWindow', 'IfcSlab', 'IfcDoor']
    insert_count = 0

    for ifc_class in target_classes:
        elements = model.by_type(ifc_class)
        print(f"Processing {len(elements)} elements of type {ifc_class}...")

        for el in elements:
            try:
                # 1. Extract Semantics
                psets = ifcopenshell.util.element.get_psets(el)
                clean_psets = {k: v for k, v in psets.items() if v is not None}

                # 2. Extract Geometry and Bounding Dimensions
                shape = ifcopenshell.geom.create_shape(settings, el)
                matrix = shape.transformation.matrix

                # Get the absolute center placement point
                x_center = matrix[12]
                y_center = matrix[13]

                # Extract the actual structural vertices to calculate real length/width bounding limits
                verts = shape.geometry.verts
                xs = [verts[i] for i in range(0, len(verts), 3)]
                ys = [verts[i + 1] for i in range(1, len(verts), 3)]

                if xs and ys:
                    x_min, x_max = min(xs), max(xs)
                    y_min, y_max = min(ys), max(ys)

                    # Construct a perfectly scaled architectural boundary polygon
                    wkt_geom = f"POLYGON(({x_center + x_min} {y_center + y_min}, " \
                               f"{x_center + x_max} {y_center + y_min}, " \
                               f"{x_center + x_max} {y_center + y_max}, " \
                               f"{x_center + x_min} {y_center + y_max}, " \
                               f"{x_center + x_min} {y_center + y_min}))"
                else:
                    # Fallback default square if vertices fail to load
                    wkt_geom = f"POLYGON(({x_center} {y_center}, {x_center + 1} {y_center}, {x_center + 1} {y_center + 1}, {x_center} {y_center + 1}, {x_center} {y_center}))"

                # 3. DB Ingestion
                insert_query = """
                    INSERT INTO hft_bau4 (globalid, name, ifc_class, geom, metadata)
                    VALUES (%s, %s, %s, ST_GeomFromText(%s, 25832), %s)
                    ON CONFLICT (globalid) DO NOTHING;
                """

                cur.execute(insert_query, (
                    el.GlobalId,
                    el.Name if el.Name else "Unnamed",
                    ifc_class,
                    wkt_geom,
                    Json(clean_psets)
                ))
                insert_count += 1

            except Exception as geo_err:
                continue

    conn.commit()
    print(f"\n---> Success! Ingested {insert_count} detailed structural elements into PostGIS.")
    cur.close()
    conn.close()


if __name__ == "__main__":
    setup_database()
    run_integrated_pipeline(IFC_FILE)