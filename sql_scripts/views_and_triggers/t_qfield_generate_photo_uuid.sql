-- 1. Create or update the unified automation function
CREATE OR REPLACE FUNCTION qfield_data.ensure_photo_uuid()
RETURNS TRIGGER AS $$
BEGIN
    -- Step A: Handle missing UUIDs
    IF NEW.photo_id IS NULL THEN
        NEW.photo_id := gen_random_uuid();
    END IF;

    -- Step B: Handle missing or blank photo names
    IF NEW.photo_name IS NULL OR TRIM(NEW.photo_name) = '' THEN
        NEW.photo_name := NEW.alkis_id || '_' || to_char(CURRENT_TIMESTAMP, 'YYYYMMDD_HH24MISS');
    END IF;

    -- Return the fully updated record to be saved
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- 2. Securely bind the single trigger to your data collection table
DROP TRIGGER IF EXISTS trg_ensure_photo_uuid ON qfield_data.building_photos;
DROP TRIGGER IF EXISTS t_qfield_generate_photo_uuid ON qfield_data.building_photos;

CREATE TRIGGER trg_ensure_photo_uuid
    BEFORE INSERT ON qfield_data.building_photos
    FOR EACH ROW
    EXECUTE FUNCTION qfield_data.ensure_photo_uuid();