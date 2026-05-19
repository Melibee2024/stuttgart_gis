CREATE OR REPLACE VIEW qfield_data.v_parcel_building_context AS
SELECT
    b.gml_id AS building_gml_id,
    b.gebaeudefunktion,
    f.gml_id AS parcel_gml_id,
    -- Calculates the overlapping area using the correct wkb_geometry column name
    ST_Area(ST_Intersection(b.wkb_geometry, f.wkb_geometry)) AS overlap_area
FROM stuttgart_processed.ax_gebaeude_clean b
INNER JOIN stuttgart_processed.ax_flurstueck_clean f
    ON ST_Intersects(b.wkb_geometry, f.wkb_geometry)
-- Filters out tiny edge touches (less than 1 square meter) to keep the data clean
WHERE ST_Area(ST_Intersection(b.wkb_geometry, f.wkb_geometry)) > 1.0;