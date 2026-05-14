CREATE TABLE qfield_data.photos (
    id SERIAL PRIMARY KEY,
    gml_id TEXT,
    photo_url TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);