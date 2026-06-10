DROP VIEW IF EXISTS qfield_data.v_building_field_survey;

CREATE OR REPLACE VIEW qfield_data.v_building_field_survey AS
SELECT
    b.ogc_fid,
    b.gml_id,
    b.gebaeudefunktion AS funktion_code,

    -- Pulls the human-readable German text directly from your lookup table
    f.beschreibung AS gebaeudefunktion_text,

    -- Dynamic area calculation (footprint in square metres, EPSG:25832)
    public.ST_Area(b.wkb_geometry) AS grund_flaeche,

    -- Status indicators tracking progress on campus
    CASE
        WHEN COUNT(p.photo_id) > 0 THEN TRUE
        ELSE FALSE
    END AS untersucht,
    CASE
        WHEN COUNT(p.photo_id) > 0 THEN '1'::text
        ELSE '0'::text
    END AS inside_project_area,

    COUNT(p.photo_id) AS photo_count,

    -- Valid geometry passing directly into QGIS
    b.wkb_geometry::public.geometry(MultiPolygon, 25832) AS geom

FROM stuttgart_processed.ax_gebaeude_clean b
LEFT JOIN stuttgart_processed.ax_gebaeudefunktion_clean f
    ON b.gebaeudefunktion = f.wert
LEFT JOIN qfield_data.building_photos p
    ON b.gml_id = p.alkis_id

-- Only offer buildings for survey that ALSO exist in the citydb 3D model, so a
-- surveyor can never attach photos to a building that isn't viewable in Cesium.
-- Keeps this layer consistent with public.data_fusion_view (same building set).
WHERE EXISTS (
    SELECT 1 FROM citydb.external_reference er
    WHERE er.name = b.gml_id
)

GROUP BY
    b.ogc_fid,
    b.gml_id,
    b.gebaeudefunktion,
    f.beschreibung,
    b.wkb_geometry
ORDER BY b.gml_id;

GRANT SELECT ON qfield_data.v_building_field_survey TO public;