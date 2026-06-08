-- nexus3d_setup.sql
-- Creates the nexus3d schema, tables, and compatibility views for the
-- hft_db (3DCityDB v4).  Run once: psql -f nexus3d_setup.sql

-- ── Schema ────────────────────────────────────────────────────────────────────
CREATE SCHEMA IF NOT EXISTS nexus3d;

-- ── nexus3d.building_georef ───────────────────────────────────────────────────
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

-- Insert the HFT Bau4 georeferencing record (extracted from georef IFC).
-- Eastings/Northings from IfcMapConversion; feature_id = cityobject.id for
-- DEBW_52210005DwE; geometry_data_id = surface_geometry.id (solid, root).
INSERT INTO nexus3d.building_georef
    (objectid, feature_id, geometry_data_id,
     eastings, northings, orthogonal_height,
     xaxis_abscissa, xaxis_ordinate, long_axis_bearing, fit_iou,
     ifc_source_file, georef_ifc_file)
VALUES
    ('DEBWL52210005DwE', 27898, 134627,
     512614.70, 5403013.80, 245.0,
     1.0, 0.0, 0.0, 1.0,
     'HFT_Bau4_2025.04.22.ifc', 'HFT_Bau4_2025.04.22_georef.ifc')
ON CONFLICT (objectid) DO UPDATE SET
    feature_id        = EXCLUDED.feature_id,
    geometry_data_id  = EXCLUDED.geometry_data_id,
    eastings          = EXCLUDED.eastings,
    northings         = EXCLUDED.northings,
    orthogonal_height = EXCLUDED.orthogonal_height,
    xaxis_abscissa    = EXCLUDED.xaxis_abscissa,
    xaxis_ordinate    = EXCLUDED.xaxis_ordinate,
    long_axis_bearing = EXCLUDED.long_axis_bearing,
    fit_iou           = EXCLUDED.fit_iou,
    ifc_source_file   = EXCLUDED.ifc_source_file,
    georef_ifc_file   = EXCLUDED.georef_ifc_file,
    derived_at        = now();

-- ── nexus3d.ifc_elements ─────────────────────────────────────────────────────
-- Populated by ifc_to_postgis.py.
CREATE TABLE IF NOT EXISTS nexus3d.ifc_elements (
    id                  SERIAL PRIMARY KEY,
    global_id           TEXT NOT NULL,
    ifc_class           TEXT,
    name                TEXT,
    storey              TEXT,
    z_min_ellipsoidal   DOUBLE PRECISION,
    z_max_ellipsoidal   DOUBLE PRECISION,
    element_height_m    DOUBLE PRECISION,
    geometry            GEOMETRY(GeometryCollectionZ, 25832),
    attributes          JSONB
);

CREATE INDEX IF NOT EXISTS ifc_elements_global_id_idx ON nexus3d.ifc_elements (global_id);
CREATE INDEX IF NOT EXISTS ifc_elements_geom_idx      ON nexus3d.ifc_elements USING GIST (geometry);

-- ── nexus3d.v_ifc_tiles ──────────────────────────────────────────────────────
-- Used by pg2b3dm to generate the 3D tileset.
CREATE OR REPLACE VIEW nexus3d.v_ifc_tiles AS
SELECT
    global_id,
    ifc_class,
    name,
    storey,
    z_min_ellipsoidal,
    z_max_ellipsoidal,
    element_height_m,
    -- pg2b3dm requires MultiPolygon, not GeometryCollection. The +66.24 m
    -- ST_Translate = +54.0 (orthometric → the height Cesium World Terrain
    -- actually sits at over HFT, sampled; ~6m above the textbook geoid because
    -- Cesium WT runs high here) + 12.24 to lift the IFC base (z_min 239.96) onto
    -- the citydb twin's base (252.2). Result: IFC base = 306.2 = terrain, no sink.
    ST_Translate(ST_CollectionExtract(geometry, 3), 0, 0, 66.24)
        ::geometry(MultiPolygonZ, 25832) AS geom
FROM nexus3d.ifc_elements
WHERE geometry IS NOT NULL
  AND ifc_class NOT IN ('IfcSpace');   -- exclude transparent/invisible classes

-- ── nexus3d.citydb_base_tiles ────────────────────────────────────────────────
-- Full-city LoD2 base buildings, tiled by pg2b3dm into public/tiles_citydb.
-- Materialised (not a view) so it can carry a spatial index for fast tiling.
-- Z is shifted (+54.0 m, calibrated to Cesium World Terrain over HFT) to match
-- the IFC and sit on terrain.
--   DROP TABLE IF EXISTS nexus3d.citydb_base_tiles;
--   CREATE TABLE nexus3d.citydb_base_tiles AS
--   SELECT sg.cityobject_id::bigint AS id, co.gmlid,
--          ST_Translate(sg.solid_geometry, 0, 0, 54.0) AS geom
--   FROM citydb.surface_geometry sg
--   JOIN citydb.cityobject co ON co.id = sg.cityobject_id
--   WHERE sg.solid_geometry IS NOT NULL;
--   ALTER TABLE nexus3d.citydb_base_tiles ADD COLUMN gid serial PRIMARY KEY;
--   CREATE INDEX citydb_base_tiles_geom_idx ON nexus3d.citydb_base_tiles
--          USING gist(ST_Centroid(ST_Envelope(geom)));
--   -- Drop the LoD2 box for any building that has a detailed IFC (the IFC
--   -- tileset already shows it; keeping the box causes a z-fighting duplicate):
--   DELETE FROM nexus3d.citydb_base_tiles
--   WHERE alkis_id IN (SELECT objectid FROM nexus3d.building_georef);
-- Then tile it:
--   pg2b3dm --connection "..." -t nexus3d.citydb_base_tiles -c geom -a gmlid
--           -o ./public/tiles_citydb

-- ── public.data_fusion_view ───────────────────────────────────────────────────
-- Joins ALKIS cadastral data with field-survey info, keyed by ALKIS gmlid.
CREATE OR REPLACE VIEW public.data_fusion_view AS
SELECT
    b.gml_id::text                                                  AS gmlid,
    COALESCE(f.beschreibung, b.gebaeudefunktion::text, 'Unknown')   AS alkis_usage,
    COALESCE(
        to_char(cb.year_of_construction, 'YYYY'),
        'Unknown'
    )                                                               AS alkis_year_built,
    CASE
        WHEN count(p.photo_id) > 0 THEN 'Surveyed (' || count(p.photo_id)::text || ' photos)'
        ELSE 'Not yet surveyed'
    END                                                             AS qfield_condition
FROM stuttgart_processed.ax_gebaeude_clean b
LEFT JOIN stuttgart_processed.ax_gebaeudefunktion_clean f
    ON b.gebaeudefunktion = f.wert
LEFT JOIN citydb.external_reference er
    ON b.gml_id::text = er.name
LEFT JOIN citydb.building cb
    ON cb.id = er.cityobject_id
LEFT JOIN qfield_data.building_photos p
    ON b.gml_id::text = p.alkis_id
GROUP BY b.gml_id, f.beschreibung, b.gebaeudefunktion, cb.year_of_construction;
