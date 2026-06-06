CREATE OR REPLACE VIEW qfield_data.v_building_digital_twin AS
SELECT
    b.gml_id AS alkis_id,
    b.wkb_geometry AS geom_2d,          -- Using your verified wkb_geometry column name
    b.gebaeudefunktion,
    c.id AS citydb_building_id,
    c.objectclass_id,
    -- Aggregates all distinct picture file pathways associated with the building into an array
    ARRAY_REMOVE(ARRAY_AGG(DISTINCT p.file_path), NULL) AS photo_gallery_paths,
    COUNT(DISTINCT p.photo_id) AS photo_count
FROM stuttgart_processed.ax_gebaeude_clean b
-- Bridge across to the external reference table using the ALKIS string match
LEFT JOIN citydb.external_reference ext ON b.gml_id = ext.name
-- Pull the core 3D citydb object using the cityobject identifier
LEFT JOIN citydb.building c ON ext.cityobject_id = c.id
-- Attach the synchronized image files
LEFT JOIN qfield_data.building_photos p ON b.gml_id = p.alkis_id
GROUP BY b.gml_id, b.wkb_geometry, b.gebaeudefunktion, c.id, c.objectclass_id;