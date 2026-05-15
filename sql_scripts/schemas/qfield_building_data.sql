-- 1. Create the dedicated schema for field work
CREATE SCHEMA IF NOT EXISTS qfield_data;

-- 2. Create the table for field observations (The "Physical" side)
-- We use a UUID or a unique string for better QField synchronization
CREATE TABLE IF NOT EXISTS qfield_data.building_photos (
    photo_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    alkis_id character(16) REFERENCES stuttgart_2d.ax_gebaeude(gml_id),
    file_path TEXT NOT NULL,           -- Relative path for QField (e.g., 'DCIM/photo1.jpg')
    direction TEXT,                    -- e.g., 'North Facade'
    notes TEXT,                        -- Student observations
    captured_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    geom_camera geometry(Point, 25832) -- The GPS location of the student
);

-- 3. Create the QField View (The "Virtual" side)
-- This is what you actually load into your QGIS project
CREATE OR REPLACE VIEW qfield_data.v_field_survey_layer AS
SELECT
    a.ogc_fid,
    a.gml_id,
    -- Using the 'bezeichner' from the view you just investigated!
    v.bezeichner AS gebaeudefunktion,
    -- Wrapping in ST_MakeValid ensures QField never crashes on messy geometry
    ST_MakeValid(a.wkb_geometry)::geometry(MultiPolygon, 25832) AS geom,
    -- Count photos to show progress in the field
    COUNT(p.photo_id) AS photo_count
FROM stuttgart_2d.ax_gebaeude a
LEFT JOIN stuttgart_2d.v_geb_funktion v ON a.gebaeudefunktion = v.wert
LEFT JOIN qfield_data.building_photos p ON a.gml_id = p.alkis_id
GROUP BY a.ogc_fid, a.gml_id, v.bezeichner, a.wkb_geometry;

-- 4. Set Permissions for the Team
GRANT USAGE ON SCHEMA qfield_data TO public;
GRANT SELECT, INSERT, UPDATE ON qfield_data.building_photos TO public;
GRANT SELECT ON qfield_data.v_field_survey_layer TO public;