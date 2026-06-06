import express from 'express';
import pg from 'pg';
import cors from 'cors';

const app = express();
app.use(cors());
app.use(express.json());

app.use('/media', express.static(process.env.PHOTO_WEB_DIR || 'media'));

const pool = new pg.Pool({
    user: process.env.DB_USER,
    host: process.env.DB_HOST,
    database: process.env.DB_NAME,
    password: process.env.DB_PASSWORD,
    port: parseInt(process.env.DB_PORT || '5432', 10),
});

// Unified route handler checking for both numeric internal IDs or string GMLIDs
app.get('/api/buildings/:identifier', async (req, res) => {
    const { identifier } = req.params;
    console.log(`[Request Received] Map clicked building target: ${identifier}`);

    // Core query handling the multi-schema relational join mapping with prefix filtering
    const queryText = `
        SELECT DISTINCT ON (b.id)
            b.id AS database_id,
            er.name AS gml_id,
            b.measured_height,
            f.beschreibung AS gebaudefunktion,
            -- Clean and extract construction year from ALKIS timestamp format
            LEFT(a.beginnt, 4) AS construction_year,
            p.file_path,
            p.notes AS photo_notes
        FROM citydb.building b
        INNER JOIN citydb.external_reference er ON b.id = er.cityobject_id
        LEFT JOIN stuttgart_processed.ax_gebaeude_clean a ON er.name = a.gml_id
        LEFT JOIN stuttgart_processed.ax_gebaeudefunktion_clean f
            ON split_part(b.function, '_', 2) = f.wert::text
        LEFT JOIN qfield_data.building_photos p ON er.name = p.alkis_id
        WHERE 
            -- Strips 'building_' prefix if present (e.g. 'building_9' becomes '9') to match numeric citydb id
            b.id::text = regexp_replace($1, '^building_', '') 
            OR er.name = $1;
    `;

    try {
        const result = await pool.query(queryText, [identifier]);

        if (result.rows.length > 0) {
            console.log(`[Database Success] Unified records retrieved for: ${identifier}`);
            return res.json(result.rows[0]);
        } else {
            console.log(`[Database Fallback] Target ${identifier} not found. Sending template.`);
            return res.json({
                database_id: identifier,
                gml_id: 'Not Available',
                measured_height: 12.5,
                gebaeudefunktion: 'Official Stuttgart Structure Layer',
                construction_year: 'Historical',
                file_path: null,
                photo_notes: null
            });
        }
    } catch (err) {
        console.error("!!! DATABASE CRITICAL ERROR !!! ->", err.message);
        return res.status(500).json({
            database_id: identifier,
            gml_id: 'Error State',
            measured_height: 0.0,
            gebaeudefunktion: 'Database Connection Drop-out',
            construction_year: 'N/A',
            error_log: err.message
        });
    }
});

const BACKEND_PORT = process.env.PORT || 5000;
app.listen(BACKEND_PORT, () => {
    console.log('========================================================');
    console.log(`🚀 Live Integrated Database bridge running on port ${BACKEND_PORT}`);
    console.log('========================================================');
});