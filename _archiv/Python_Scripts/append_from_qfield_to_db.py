from qgis.core import QgsProject, QgsFeature, QgsFeatureRequest

# 1. Layer names matching your exact panel setup
LOCAL_LAYER = "building_photos_data"  # Your active field GeoPackage layer
POSTGIS_LAYER = "building_photos"  # Your live PostGIS database table layer

local_lyr = QgsProject.instance().mapLayersByName(LOCAL_LAYER)[0]
db_lyr = QgsProject.instance().mapLayersByName(POSTGIS_LAYER)[0]

if local_lyr and db_lyr:
    print(f"Reading from local source: {local_lyr.source()}")
    print(f"Targeting active database: {db_lyr.source()}")

    provider = db_lyr.dataProvider()

    # 2. Build a memory bank of existing entries in PostGIS using file_path
    existing_records = set()
    for db_feat in db_lyr.getFeatures():
        path = db_feat.attribute('file_path')
        if path:
            existing_records.add(path)

    features_to_add = []
    skipped_count = 0

    # 3. Request features from the local layer disk cleanly without memory caching
    request = QgsFeatureRequest().setFlags(QgsFeatureRequest.NoFlags)

    for local_feat in local_lyr.getFeatures(request):
        local_path = local_feat.attribute('file_path')

        # Skip if this image path has already been processed and saved
        if local_path in existing_records:
            skipped_count += 1
            continue

        new_feat = QgsFeature(db_lyr.fields())
        new_feat.setGeometry(local_feat.geometry())

        # Mapping attributes to your 8-column database schema
        new_feat.setAttribute('alkis_id', local_feat.attribute('alkis_id'))
        new_feat.setAttribute('file_path', local_path)
        new_feat.setAttribute('direction', local_feat.attribute('direction'))
        new_feat.setAttribute('notes', local_feat.attribute('notes'))
        new_feat.setAttribute('captured_at', local_feat.attribute('captured_at'))

        features_to_add.append(new_feat)

    # 4. Stream the data live across your localhost connection
    if features_to_add:
        print(f"Skipped {skipped_count} duplicates. Transmitting {len(features_to_add)} fresh records...")
        success, added_features = provider.addFeatures(features_to_add)

        if success:
            print(f"🎉 SUCCESS! Added {len(added_features)} unique points to pgAdmin.")
            db_lyr.triggerRepaint()
        else:
            print(f"❌ Error writing to database: {provider.lastError().text()}")
    else:
        print(f"🙌 Database is fully up-to-date! Skipped {skipped_count} matching entries.")
else:
    print("❌ Error: Verify that both 'building_photos_data' and 'building_photos' are in your Layers panel.")