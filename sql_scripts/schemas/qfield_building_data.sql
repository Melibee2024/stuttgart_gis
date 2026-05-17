CREATE SCHEMA IF NOT EXISTS qfield_data;
-- I forgot to add the photo_name field!! but i did it in pgadmin :)

-- Create the physical table for photos and GPS camera points
CREATE TABLE IF NOT EXISTS qfield_data.building_photos (
    photo_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    alkis_id TEXT NOT NULL,            -- Links to stuttgart_processed.ax_gebaeude_clean(gml_id)
    file_path TEXT NOT NULL,           -- Relative path configuration for your device camera
    direction TEXT,
    notes TEXT,
    captured_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    geom_camera geometry(Point, 25832)
);

-- Grant permissions so you don't run into lock/sync issues in the field
GRANT USAGE ON SCHEMA qfield_data TO public;
GRANT SELECT, INSERT, UPDATE, DELETE ON qfield_data.building_photos TO public;