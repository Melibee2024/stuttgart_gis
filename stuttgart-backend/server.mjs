import express from 'express';
import pg from 'pg';
import cors from 'cors';

const app = express();
app.use(cors());
app.use(express.json());

// Serve static photo attachments from your QField sync media directory
app.use('/media', express.static('media'));

// 1. Configure Connection Pool using your clean .env file configurations
const pool = new pg.Pool({
    user: process.env.DB_USER,
    host: process.env.DB_HOST,
    database: process.env.DB_NAME,
    password: process.env.DB_PASSWORD,
    port: parseInt(process.env.DB_PORT || '5432', 10),
});

// 2. Create the Live Detail API Endpoint using your main CityDB layout
app.get('/api/buildings/:id', async (req, res) => {
    const { id } = req.params;
    console.log(`[Request Received] Map clicked building Database ID: ${id}`);

    // SQL Statement designed to extract actual citydb parameters
    const queryText = `
        SELECT 
            id,
            measured_height,
            function AS gebaudefunktion,
            objectclass_id
        FROM citydb.building
        WHERE id = $1;
    `;

    try {
        // Convert the incoming ID parameter into a clean BigInt number format for PostgreSQL
        const targetId = parseInt(id, 10);
        if (isNaN(targetId)) {
            return res.status(400).json({ error: "Invalid building database ID format." });
        }

        const result = await pool.query(queryText, [targetId]);

        if (result.rows.length > 0) {
            console.log(`[Database Success] Real CityDB metrics retrieved for ID: ${id}`);
            return res.json(result.rows[0]);
        } else {
            console.log(`[Database Fallback] No explicit CityDB row for ID: ${id}. Sending template.`);
            return res.json({
                id: id,
                measured_height: 10.0,
                gebaudefunktion: 'Historical / Unknown',
                objectclass_id: 26
            });
        }
    } catch (err) {
        console.error("!!! DATABASE CRITICAL ERROR DETAILS !!! ->", err.message);
        return res.status(500).json({
            id: id,
            measured_height: 10.0,
            gebaudefunktion: 'Database Fallback Connection Dynamic View',
            objectclass_id: 26,
            error_log: err.message
        });
    }
});

const BACKEND_PORT = process.env.PORT || 5000;
app.listen(BACKEND_PORT, () => {
    console.log('========================================================');
    console.log(`🚀 Live CityDB Database bridge active on: http://localhost:${BACKEND_PORT}`);
    console.log('========================================================');
});