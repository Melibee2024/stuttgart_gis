CREATE OR REPLACE VIEW qfield_data.v_building_field_survey AS
SELECT
    b.ogc_fid,
    b.gml_id,
    b.gebaeudefunktion AS funktion_code,

    -- Pulls the human-readable German text directly from your lookup table
    f.beschreibung AS gebaeudefunktion_text,

    -- Dynamic area calculations
    public.ST_Area(b.wkb_geometry) AS grund_flaeche,
    public.ST_Area(b.wkb_geometry) AS area,

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

GROUP BY
    b.ogc_fid,
    b.gml_id,
    b.gebaeudefunktion,
    f.beschreibung,
    b.wkb_geometry
ORDER BY b.gml_id;

GRANT SELECT ON qfield_data.v_building_field_survey TO public;