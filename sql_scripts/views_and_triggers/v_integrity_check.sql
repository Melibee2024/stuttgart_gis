CREATE OR REPLACE VIEW qfield_data.v_integrity_check AS
SELECT
    COALESCE(b.gml_id, ext.name) AS building_key,
    CASE
        WHEN b.gml_id IS NOT NULL AND ext.name IS NOT NULL THEN 'Perfect Match'
        WHEN b.gml_id IS NOT NULL AND ext.name IS NULL THEN 'Orphan 2D (Missing 3D Reference)'
        WHEN b.gml_id IS NULL AND ext.name IS NOT NULL THEN 'Orphan 3D (Missing 2D Footprint)'
    END AS integrity_status,
    COUNT(p.photo_id) AS photo_count
FROM stuttgart_processed.ax_gebaeude_clean b
FULL OUTER JOIN citydb.external_reference ext ON b.gml_id = ext.name
LEFT JOIN qfield_data.building_photos p ON COALESCE(b.gml_id, ext.name) = p.alkis_id
GROUP BY b.gml_id, ext.name;