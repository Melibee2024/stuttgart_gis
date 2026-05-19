CREATE OR REPLACE VIEW qfield_data.v_cesium_payload AS
SELECT
    alkis_id,
    geom_2d AS wkb_geometry, -- Keeps the geometry separate so GeoServer can map it spatially
    -- Bundles all metadata and your 10 field photo paths into a structured JSON string
    json_build_object(
        'alkis_id', alkis_id,
        'gebaeudefunktion', gebaeudefunktion,
        'citydb_building_id', citydb_building_id,
        'objectclass_id', objectclass_id,
        'photo_count', photo_count,
        'photo_gallery', photo_gallery_paths
    ) AS building_properties_json
FROM qfield_data.v_building_digital_twin;