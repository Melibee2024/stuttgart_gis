
-- ==============================
-- FIELD DATA SCHEMA
-- ==============================

-- Ensure schema for QField field data exists
CREATE SCHEMA IF NOT EXISTS qfield_data;


-- ==============================
-- PHOTOS TABLE
-- ==============================

-- Stores field photos linked to 2D building dataset
CREATE TABLE IF NOT EXISTS qfield_data.photos (
    id SERIAL PRIMARY KEY,

    -- Global identifier from 2D cadastral dataset (stuttgart_2d.ax_gebaeude)
    gml_id TEXT NOT NULL,

    -- Image storage location (URL or file path, e.g. Nextcloud or media server)
    photo_url TEXT NOT NULL,

    -- Timestamp of when the field record was created
    created_at TIMESTAMP DEFAULT NOW()
);


-- ==============================
-- PERFORMANCE INDEX
-- ==============================

-- Speeds up queries filtering photos by building ID
CREATE INDEX IF NOT EXISTS idx_photos_gml_id
ON qfield_data.photos (gml_id);


-- ==============================
-- OPTIONAL BUT RECOMMENDED: FK
-- ==============================

-- Ensures every photo is linked to an existing building in the 2D dataset
-- NOTE: This requires stuttgart_2d.ax_gebaeude.gml_id to be PRIMARY KEY or UNIQUE
ALTER TABLE qfield_data.photos
ADD CONSTRAINT fk_photos_gml_id
FOREIGN KEY (gml_id)
REFERENCES stuttgart_2d.ax_gebaeude(gml_id);