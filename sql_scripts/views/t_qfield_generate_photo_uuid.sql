-- Create a function that assigns a UUID if it is missing
CREATE OR REPLACE FUNCTION qfield_data.ensure_photo_uuid()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.photo_id IS NULL THEN
        NEW.photo_id := gen_random_uuid();
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Attach the trigger to your data collection table
CREATE OR REPLACE TRIGGER trg_ensure_photo_uuid
BEFORE INSERT ON qfield_data.building_photos
FOR EACH ROW
EXECUTE FUNCTION qfield_data.ensure_photo_uuid();