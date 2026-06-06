-- 1. Create the schema for your optimized data
CREATE SCHEMA IF NOT EXISTS stuttgart_processed;

-- 2. Add a comment so your team knows what this is for
COMMENT ON SCHEMA stuttgart_processed IS 'Contains cleaned, pruned, and validated ALKIS data for the Digital Twin project.';

-- 3. Set permissions (Important if your team is connecting via LAN)
-- This allows your teammates to see the schema
GRANT USAGE ON SCHEMA stuttgart_processed TO public;
GRANT SELECT ON ALL TABLES IN SCHEMA stuttgart_processed TO public;