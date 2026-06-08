CREATE OR REPLACE VIEW qfield_data.v_field_progress_monitor AS
SELECT
    b.gml_id AS alkis_id,
    b.gebaeudefunktion,
    COUNT(p.photo_id) AS total_photos_captured,
    MAX(p.captured_at) AS last_survey_date,
    CASE
        WHEN COUNT(p.photo_id) > 0 THEN 'Completed'
        ELSE 'Pending Field Survey'
    END AS survey_status
FROM stuttgart_processed.ax_gebaeude_clean b
LEFT JOIN qfield_data.building_photos p ON b.gml_id = p.alkis_id
GROUP BY b.gml_id, b.gebaeudefunktion;