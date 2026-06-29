CREATE OR REPLACE VIEW qfield_data.v_cesium_payload AS
SELECT
    twin.alkis_id,
    twin.geom_2d AS wkb_geometry,
    json_build_object(
        'alkis_id', twin.alkis_id,
        'gebaeudefunktion', twin.gebaeudefunktion,
        'citydb_building_id', twin.citydb_building_id,
        'objectclass_id', twin.objectclass_id,
        'photo_count', twin.photo_count,
        'photo_gallery', twin.photo_gallery_paths
    ) AS building_properties_json
FROM qfield_data.v_building_digital_twin twin
-- Using fully qualified functions and standard WKT polygon to avoid operator errors!
WHERE public.ST_Intersects(
    twin.geom_2d,
    public.ST_GeomFromText(
        'POLYGON((511660.92 5402038.54, 511660.92 5404069.90, 513784.62 5404069.90, 513784.62 5402038.54, 511660.92 5402038.54))',
        25832
    )
);