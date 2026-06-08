import 'dotenv/config';
import express from 'express';
import pg from 'pg';
import cors from 'cors';
import { exec } from 'child_process';
import { promisify } from 'util';
import path from 'path';
import { fileURLToPath } from 'url';

const execAsync  = promisify(exec);
const __filename = fileURLToPath(import.meta.url);
const __dirname  = path.dirname(__filename);

const app = express();
app.use(cors());
app.use(express.json());
app.use('/media', express.static('media'));
app.use('/tiles', express.static('public/tiles'));
app.use('/tiles_citydb', express.static('public/tiles_citydb'));


// ─── DB CONNECTION ────────────────────────────────────────────────────────────
if (!process.env.DB_PASSWORD) {
    console.error('');
    console.error('❌  DB_PASSWORD is not set.');
    console.error('    Create a .env file in this directory containing:');
    console.error('');
    console.error('      DB_USER=postgres');
    console.error('      DB_HOST=localhost');
    console.error('      DB_NAME=3DCity');
    console.error('      DB_PASSWORD=YourPassword');
    console.error('      DB_PORT=5432');
    console.error('');
    process.exit(1);
}

const pool = new pg.Pool({
    user:     process.env.DB_USER     ?? 'postgres',
    host:     process.env.DB_HOST     ?? 'localhost',
    database: process.env.DB_NAME     ?? '3DCity',
    password: process.env.DB_PASSWORD,
    port:     Number(process.env.DB_PORT) || 5432,
});


// ─── GET /api/georef ──────────────────────────────────────────────────────────
// Returns the latest georeferenced building plus the heights the Cesium
// frontend needs to vertically snap the IFC onto its citydb twin's base —
// all data-driven, no hardcoded geoid:
//   citydb_base_z / citydb_roof_z  — the matched citydb feature's z range
//                                    (raw EPSG:25832); their difference is the
//                                    building's height, subtracted from the
//                                    ray-cast rendered roof to get the base.
//   ifc_z_min                      — the IFC tiles' lowest raw z (= their
//                                    current rendered bottom, since the view
//                                    no longer applies any geoid shift).
app.get('/api/georef', async (_req, res) => {
    try {
        const { rows } = await pool.query(`
            SELECT
                bg.objectid,
                bg.feature_id,
                bg.eastings,
                bg.northings,
                bg.orthogonal_height,
                bg.long_axis_bearing,
                bg.fit_iou,
                cb.citydb_base_z,
                cb.citydb_roof_z,
                ig.ifc_z_min,
                ig.ifc_z_max,
                fp.footprint_geojson
            FROM nexus3d.building_georef bg
            LEFT JOIN LATERAL (
                SELECT MIN(ST_ZMin(solid_geometry)) AS citydb_base_z,
                       MAX(ST_ZMax(solid_geometry)) AS citydb_roof_z
                FROM citydb.surface_geometry
                WHERE cityobject_id = bg.feature_id AND solid_geometry IS NOT NULL
            ) cb ON true
            -- 2D footprint (WGS84) of the matched citydb building, for clipping
            -- the base tileset where the IFC sits.
            LEFT JOIN LATERAL (
                SELECT ST_AsGeoJSON(ST_Transform(
                         ST_CollectionExtract(
                             ST_UnaryUnion(ST_Collect(ST_MakeValid(d.geom))), 3), 4326)
                       ) AS footprint_geojson
                FROM citydb.geometry_data g2
                CROSS JOIN LATERAL ST_Dump(ST_Force2D(g2.geometry)) d
                WHERE g2.feature_id = bg.feature_id
                  AND ST_GeometryType(g2.geometry) = 'ST_PolyhedralSurface'
            ) fp ON true
            LEFT JOIN LATERAL (
                SELECT MIN(ST_ZMin(geometry)) AS ifc_z_min,
                       MAX(ST_ZMax(geometry)) AS ifc_z_max
                FROM nexus3d.ifc_elements
                WHERE geometry IS NOT NULL
            ) ig ON true
            ORDER BY bg.derived_at DESC
            LIMIT 1;
        `);

        if (rows.length === 0) {
            return res.status(404).json({
                error: 'No georef record found. Run georeferenceifc.py first.'
            });
        }
        return res.json(rows[0]);
    } catch (err) {
        console.error('[DB ERROR] /api/georef', err.message);
        return res.status(500).json({ error: err.message });
    }
});


// ─── GET /api/georef/:objectid ────────────────────────────────────────────────
// Returns the derived georeferencing parameters for a building from
// nexus3d.building_georef (written by georeference_ifc.py).
// main.js calls this on startup to get IFC_BASE_ELLIPSOIDAL and the
// building centre coordinates — no hardcoding needed in the frontend.
//
// Response:
//   objectid            TEXT      — citydb ALKIS objectid
//   eastings            FLOAT     — EPSG:25832 easting of IFC origin
//   northings           FLOAT     — EPSG:25832 northing of IFC origin
//   orthogonal_height   FLOAT     — DHHN2016 orthometric height (metres)
//   xaxis_abscissa      FLOAT     — cos(rotation angle)
//   xaxis_ordinate      FLOAT     — sin(rotation angle)
//   long_axis_bearing   FLOAT     — degrees from East
//   derived_at          TIMESTAMPTZ
app.get('/api/georef/:objectid', async (req, res) => {
    const { objectid } = req.params;

    try {
        const { rows } = await pool.query(`
            SELECT
                objectid,
                eastings,
                northings,
                orthogonal_height,
                xaxis_abscissa,
                xaxis_ordinate,
                long_axis_bearing,
                derived_at
            FROM nexus3d.building_georef
            WHERE objectid = $1
            LIMIT 1;
        `, [objectid]);

        if (rows.length === 0) {
            return res.status(404).json({
                error: `No georef record found for objectid '${objectid}'. ` +
                       `Run georeference_ifc.py first.`
            });
        }

        console.log(`[georef] Served params for ${objectid}`);
        return res.json(rows[0]);

    } catch (err) {
        console.error('[DB ERROR] /api/georef/:objectid', err.message);
        return res.status(500).json({ error: err.message });
    }
});


// ─── POST /api/regenerate-tiles ───────────────────────────────────────────────
// Runs pg2b3dm to rebuild the 3D tile output from nexus3d.v_ifc_tiles.
// Call this after re-running ifc_to_postgis.py for any building.
// Guarded by a lock flag so concurrent calls don't corrupt the tile directory.

let tileRegenRunning = false;

app.post('/api/regenerate-tiles', async (_req, res) => {
    if (tileRegenRunning) {
        return res.status(409).json({
            success: false,
            error:   'Tile regeneration already in progress — try again shortly.',
        });
    }

    tileRegenRunning = true;
    console.log('[tiles] Regeneration triggered...');

    const connStr = [
        `Host=${process.env.DB_HOST     ?? 'localhost'}`,
        `Database=${process.env.DB_NAME ?? '3DCity'}`,
        `Username=${process.env.DB_USER ?? 'postgres'}`,
        `Password=${process.env.DB_PASSWORD}`,
        `Port=${process.env.DB_PORT     ?? 5432}`,
    ].join(';');

    const cmd = [
        'pg2b3dm',
        `--connection "${connStr}"`,
        '-t nexus3d.v_ifc_tiles',
        '-c geom',
        '-a "global_id,ifc_class,name,storey,z_min_ellipsoidal,z_max_ellipsoidal,element_height_m"',
        '-o ./public/tiles',
    ].join(' ');

    try {
        const { stdout, stderr } = await execAsync(cmd, { cwd: __dirname });
        console.log('[tiles] Done.');
        if (stdout) console.log('[tiles] stdout:', stdout.trim());
        if (stderr) console.warn('[tiles] stderr:', stderr.trim());
        return res.json({ success: true, output: stdout.trim() });
    } catch (err) {
        console.error('[tiles] Failed:', err.message);
        return res.status(500).json({ success: false, error: err.message });
    } finally {
        tileRegenRunning = false;
    }
});


// ─── GET /api/tiles/status ────────────────────────────────────────────────────
app.get('/api/tiles/status', (_req, res) => {
    res.json({ running: tileRegenRunning });
});


// ─── GET /api/buildings/filters ──────────────────────────────────────────────
app.get('/api/buildings/filters', async (_req, res) => {
    try {
        const { rows } = await pool.query(`
            SELECT
                array_agg(DISTINCT ifc_class ORDER BY ifc_class) AS ifc_classes,
                array_agg(DISTINCT storey     ORDER BY storey)    AS storeys
            FROM nexus3d.ifc_elements;
        `);
        return res.json(rows[0]);
    } catch (err) {
        console.error('[DB ERROR] /api/buildings/filters', err.message);
        return res.status(500).json({ error: err.message });
    }
});


// ─── GET /api/buildings/geojson ───────────────────────────────────────────────
app.get('/api/buildings/geojson', async (req, res) => {
    const { ifc_class, storey } = req.query;

    const conditions = ['ifc.geometry IS NOT NULL'];
    const params     = [];

    if (ifc_class) {
        params.push(ifc_class);
        conditions.push(`ifc.ifc_class = $${params.length}`);
    }
    if (storey) {
        params.push(storey);
        conditions.push(`ifc.storey = $${params.length}`);
    }

    try {
        const { rows } = await pool.query(`
            SELECT jsonb_build_object(
                'type',     'FeatureCollection',
                'features', COALESCE(jsonb_agg(f), '[]'::jsonb)
            ) AS geojson
            FROM (
                SELECT jsonb_build_object(
                    'type',       'Feature',
                    'geometry',   ST_AsGeoJSON(
                                      ST_CollectionExtract(
                                          ST_Transform(ifc.geometry, 4326),
                                          3
                                      )
                                  )::jsonb,
                    'properties', jsonb_build_object(
                        'global_id',         ifc.global_id,
                        'ifc_class',         ifc.ifc_class,
                        'name',              ifc.name,
                        'storey',            ifc.storey,
                        'z_min_ellipsoidal', ifc.z_min_ellipsoidal,
                        'z_max_ellipsoidal', ifc.z_max_ellipsoidal
                    )
                ) AS f
                FROM nexus3d.ifc_elements ifc
                WHERE ${conditions.join(' AND ')}
                ORDER BY ifc.global_id
            ) sub;
        `, params);

        return res.json(rows[0].geojson);
    } catch (err) {
        console.error('[DB ERROR] /api/buildings/geojson', err.message);
        return res.status(500).json({ error: err.message });
    }
});


// ─── GET /api/buildings ───────────────────────────────────────────────────────
app.get('/api/buildings', async (req, res) => {
    const { ifc_class, storey } = req.query;
    const conditions = [];
    const params     = [];

    if (ifc_class) {
        params.push(ifc_class);
        conditions.push(`ifc_class = $${params.length}`);
    }
    if (storey) {
        params.push(storey);
        conditions.push(`storey = $${params.length}`);
    }

    const where = conditions.length ? `WHERE ${conditions.join(' AND ')}` : '';

    try {
        const { rows } = await pool.query(`
            SELECT
                global_id,
                ifc_class,
                name,
                storey,
                z_min_ellipsoidal,
                z_max_ellipsoidal
            FROM nexus3d.ifc_elements
            ${where}
            ORDER BY ifc_class, storey;
        `, params);
        return res.json(rows);
    } catch (err) {
        console.error('[DB ERROR] /api/buildings', err.message);
        return res.status(500).json({ error: err.message });
    }
});


// ─── GET /api/buildings/:globalid ────────────────────────────────────────────
app.get('/api/buildings/:globalid', async (req, res) => {
    const { globalid } = req.params;
    console.log(`[Request] globalid: ${globalid}`);

    try {
        // The clicked feature is an IFC ELEMENT (global_id = IFC GUID). But
        // photos / ALKIS / citydb props are BUILDING-level, keyed by the
        // building's objectid (gmlid). We bridge element → building via
        // nexus3d.building_georef. Today that's the single georeferenced
        // building (LIMIT 1); once ifc_elements carries a building_objectid
        // column (multi-building), replace `bg` with a join on that column.
        const { rows } = await pool.query(`
            WITH bg AS (
                SELECT objectid, feature_id
                FROM nexus3d.building_georef
                ORDER BY derived_at DESC
                LIMIT 1
            )
            SELECT
                ifc.global_id,
                ifc.ifc_class,
                ifc.name                    AS ifc_name,
                ifc.storey,
                ifc.z_min_ellipsoidal,
                ifc.z_max_ellipsoidal,
                ifc.attributes              AS ifc_attributes,

                bg.objectid                 AS building_objectid,
                feat.last_modification_date AS last_modified,

                dfv.alkis_usage,
                dfv.alkis_year_built,
                dfv.qfield_condition,

                prop.pset_properties,
                ph.field_photos

            FROM nexus3d.ifc_elements ifc
            LEFT JOIN bg ON TRUE

            -- citydb building feature (for last-modified etc.), by feature_id
            LEFT JOIN citydb.cityobject feat
                ON feat.id = bg.feature_id

            -- ALKIS usage / year / field condition, by building gmlid (= objectid)
            LEFT JOIN public.data_fusion_view dfv
                ON dfv.gmlid = bg.objectid

            -- citydb property sets for the building, aggregated
            LEFT JOIN LATERAL (
                SELECT COALESCE(jsonb_agg(jsonb_build_object(
                    'name', p.attrname, 'val_string', p.strval,
                    'val_int', p.intval, 'val_double', p.realval)), '[]'::jsonb)
                    AS pset_properties
                FROM citydb.cityobject_genericattrib p
                WHERE p.cityobject_id = bg.feature_id
            ) prop ON TRUE

            -- QField field photos for the building, by building gmlid (= objectid)
            LEFT JOIN LATERAL (
                SELECT COALESCE(jsonb_agg(jsonb_build_object(
                    'photo_id', photo_id, 'file_path', file_path,
                    'photo_name', photo_name, 'direction', direction,
                    'notes', notes, 'captured_at', captured_at::text)), '[]'::jsonb)
                    AS field_photos
                FROM qfield_data.building_photos
                WHERE alkis_id = bg.objectid
            ) ph ON TRUE

            WHERE ifc.global_id = $1;
        `, [globalid]);

        if (rows.length > 0) {
            console.log(`[DB] Found: ${globalid}`);
            return res.json(rows[0]);
        }

        console.log(`[DB] No record for ${globalid} — sending fallback`);
        return res.json({
            global_id:         globalid,
            ifc_class:         'Unknown',
            ifc_name:          'Stuttgart Building Layer',
            storey:            null,
            z_min_ellipsoidal: null,
            z_max_ellipsoidal: null,
            ifc_attributes:    null,
            bau4_name:         null,
            bau4_class:        null,
            bau4_metadata:     null,
            geometry:          null,
            objectclass_id:    null,
            identifier:        null,
            creation_date:     null,
            last_modified:     null,
            pset_properties:   [],
            field_photos:      [],
            alkis_usage:       'Official Stuttgart Building Layer',
            alkis_year_built:  'Historical / Unknown',
            qfield_condition:  'No Field Data Recorded',
        });

    } catch (err) {
        console.error('[DB ERROR] /api/buildings/:globalid', err.message);
        return res.status(500).json({ error: err.message });
    }
});


// ─── START ────────────────────────────────────────────────────────────────────
const PORT = Number(process.env.PORT) || 5000;
app.listen(PORT, () => {
    console.log('');
    console.log('══════════════════════════════════════════════');
    console.log(`  Nexus3D backend → http://localhost:${PORT}`);
    console.log('══════════════════════════════════════════════');
    console.log('  GET  /api/georef/:objectid');
    console.log('  GET  /api/buildings/filters');
    console.log('  GET  /api/buildings/geojson[?ifc_class=&storey=]');
    console.log('  GET  /api/buildings[?ifc_class=&storey=]');
    console.log('  GET  /api/buildings/:globalid');
    console.log('  GET  /api/tiles/status');
    console.log('  POST /api/regenerate-tiles');
    console.log('══════════════════════════════════════════════');
    console.log('');
});
