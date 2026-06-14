-- Recreated by qfield_service after every ogr2ogr reload of building_photos.
-- The reload uses OVERWRITE=YES, which DROP ... CASCADEs this view away, so it
-- must be rebuilt each cycle or the Cesium backend fails with
-- 'relation "public.data_fusion_view" does not exist'.
CREATE OR REPLACE VIEW public.data_fusion_view AS
SELECT
    b.gml_id::text                                                  AS gmlid,
    COALESCE(f.beschreibung, b.gebaeudefunktion::text, 'Unknown')   AS alkis_usage,
    COALESCE(to_char(cb.year_of_construction, 'YYYY'), 'Unknown')   AS alkis_year_built,
    CASE
        WHEN count(p.photo_id) > 0 THEN 'Surveyed (' || count(p.photo_id)::text || ' photos)'
        ELSE 'Not yet surveyed'
    END                                                             AS qfield_condition
FROM stuttgart_processed.ax_gebaeude_clean b
LEFT JOIN stuttgart_processed.ax_gebaeudefunktion_clean f ON b.gebaeudefunktion = f.wert
LEFT JOIN citydb.external_reference er ON b.gml_id::text = er.name
LEFT JOIN citydb.building cb ON cb.id = er.cityobject_id
LEFT JOIN qfield_data.building_photos p ON b.gml_id::text = p.alkis_id
WHERE EXISTS (SELECT 1 FROM citydb.external_reference er2 WHERE er2.name = b.gml_id::text)
GROUP BY b.gml_id, f.beschreibung, b.gebaeudefunktion, cb.year_of_construction;
