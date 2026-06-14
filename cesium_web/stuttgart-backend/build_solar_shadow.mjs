// build_solar_shadow.mjs
// Adds an inter-building SHADOW FACTOR (0–1) to nexus3d.building_themes and a
// shadow-adjusted rooftop-PV yield (kwh_shaded). Re-runnable; run after
// build_building_themes.sql (it depends on the kwh column there):
//   node build_solar_shadow.mjs            (≈20–40 s for ~32k buildings)
//
// METHOD — horizon-based shading (the standard tractable urban-solar approach):
//   1. Each building is reduced to its footprint centroid at its absolute roof
//      elevation (terrain + building height; the citydb geom carries true Z, so
//      a hill-top neighbour correctly out-shadows a valley one).
//   2. For every taller neighbour within 100 m we compute the elevation angle it
//      subtends, and keep the MAX per 10° azimuth sector → a 36-bin horizon
//      profile (the local skyline as seen from that roof).
//   3. We sweep the real Stuttgart sun path across the year (21st of each month,
//      every 30 min of daylight) and weight each sun position by sin(elevation)
//      — a clear-sky horizontal-irradiance proxy, consistent with the flat-roof
//      assumption in the base model.
//   4. shadow_factor = (irradiance-weighted fraction of sun positions whose
//      elevation clears the horizon angle in that azimuth sector).
//        1.0 = never shadowed by neighbours · 0.5 = half the annual sun lost.
//   5. kwh_shaded = round(kwh × shadow_factor).
//
// This is inter-building shadowing only; the 0.65 usable-roof fraction in
// build_building_themes.sql still covers intrinsic roof losses (setbacks,
// obstructions, orientation), so the two do not double-count.

import 'dotenv/config';
import pg from 'pg';

const LAT = 48.7800;                       // Stuttgart (HFT)
const DEG = Math.PI / 180;

// ── Sun path: 21st of each month, sampled every 30 min of solar time ──────────
function buildSunPositions() {
    const doy = [21, 52, 80, 111, 141, 172, 202, 233, 264, 294, 325, 355]; // 21st of each month
    const φ = LAT * DEG;
    const sun = [];
    for (const N of doy) {
        const δ = 23.45 * DEG * Math.sin(2 * Math.PI * (284 + N) / 365);
        for (let t = 4; t <= 20; t += 0.5) {
            const h = 15 * (t - 12) * DEG;                       // hour angle
            const sinE = Math.sin(φ) * Math.sin(δ) + Math.cos(φ) * Math.cos(δ) * Math.cos(h);
            const e = Math.asin(Math.max(-1, Math.min(1, sinE)));
            const elev = e / DEG;
            if (elev <= 3) continue;                            // ignore near-horizon noise
            let cosA = (Math.sin(δ) - Math.sin(e) * Math.sin(φ)) / (Math.cos(e) * Math.cos(φ));
            cosA = Math.max(-1, Math.min(1, cosA));
            let A = Math.acos(cosA) / DEG;                       // 0..180 from North
            if (h > 0) A = 360 - A;                              // afternoon → west
            sun.push({ az: +A.toFixed(2), elev: +elev.toFixed(2), w: +Math.sin(e).toFixed(4) });
        }
    }
    return sun;
}

const sun = buildSunPositions();
const sunValues = sun.map(s => `(${s.az},${s.elev},${s.w})`).join(',');
const noonJun = sun.filter(s => Math.abs(s.az - 180) < 8).reduce((m, s) => Math.max(m, s.elev), 0);
console.log(`[sun] ${sun.length} daylight positions · peak elevation ≈ ${noonJun.toFixed(1)}° (Stuttgart summer ≈ 64.7°)`);

const pool = new pg.Pool({
    user:     process.env.DB_USER     ?? 'postgres',
    host:     process.env.DB_HOST     ?? 'localhost',
    database: process.env.DB_NAME     ?? 'hft_db',
    password: process.env.DB_PASSWORD,
    port:     Number(process.env.DB_PORT) || 5432,
});

const client = await pool.connect();
const time = async (label, sql) => {
    const t0 = Date.now();
    const r = await client.query(sql);
    console.log(`[sql] ${label} — ${((Date.now() - t0) / 1000).toFixed(1)} s`);
    return r;
};

try {
    await time('building centroids + roof elevation', `
        CREATE TEMP TABLE _bpts AS
        WITH faces AS (
            SELECT gmlid, ST_Force2D((ST_Dump(geom)).geom) AS f2d, ST_ZMax(geom) AS zmax
            FROM nexus3d.citydb_base_tiles WHERE geom IS NOT NULL
        )
        SELECT gmlid,
               ST_Centroid(ST_Collect(f2d))::geometry(Point,25832) AS pt,
               MAX(zmax) AS roof_z
        FROM faces GROUP BY gmlid;
        CREATE INDEX _bpts_gix ON _bpts USING gist(pt);
        ANALYZE _bpts;`);

    await time('horizon profile (36 sectors, taller neighbours ≤100 m)', `
        CREATE TEMP TABLE _horizon AS
        SELECT a.gmlid,
               (floor(((degrees(atan2(ST_X(b.pt)-ST_X(a.pt), ST_Y(b.pt)-ST_Y(a.pt)))+360)::numeric % 360)/10))::int AS sector,
               max(degrees(atan2(b.roof_z - a.roof_z, ST_Distance(a.pt,b.pt)))) AS horizon_ang
        FROM _bpts a
        JOIN _bpts b
          ON a.gmlid <> b.gmlid
         AND b.roof_z > a.roof_z + 0.5
         AND ST_DWithin(a.pt, b.pt, 100)
        GROUP BY a.gmlid, sector;
        CREATE INDEX _horizon_ix ON _horizon (gmlid, sector);
        ANALYZE _horizon;`);

    await client.query(`CREATE TEMP TABLE _sun (az float, elev float, w float);`);
    await time(`insert ${sun.length} sun positions`,
        `INSERT INTO _sun (az,elev,w) VALUES ${sunValues};`);

    await time('integrate sun path against horizon → shadow_factor', `
        CREATE TEMP TABLE _sf AS
        SELECT p.gmlid,
               COALESCE(SUM(s.w) FILTER (WHERE s.elev >= COALESCE(h.horizon_ang,0)),0)
                   / NULLIF(SUM(s.w),0) AS sf
        FROM _bpts p
        CROSS JOIN _sun s
        LEFT JOIN _horizon h ON h.gmlid = p.gmlid AND h.sector = (floor(s.az/10))::int
        GROUP BY p.gmlid;
        CREATE INDEX _sf_ix ON _sf (gmlid);`);

    await time('add columns + write shadow_factor / kwh_shaded', `
        ALTER TABLE nexus3d.building_themes
            ADD COLUMN IF NOT EXISTS shadow_factor numeric(4,3),
            ADD COLUMN IF NOT EXISTS kwh_shaded   int;
        UPDATE nexus3d.building_themes bt
            SET shadow_factor = round(sf.sf::numeric, 3),
                kwh_shaded    = round(bt.kwh * sf.sf)::int
            FROM _sf sf WHERE sf.gmlid = bt.gmlid;
        UPDATE nexus3d.building_themes
            SET shadow_factor = 1.0, kwh_shaded = kwh
            WHERE shadow_factor IS NULL;`);

    const { rows } = await client.query(`
        SELECT count(*) AS n,
               round(avg(shadow_factor), 3)            AS avg_shadow_factor,
               round(sum(kwh)::numeric        / 1e6, 1) AS unshaded_gwh,
               round(sum(kwh_shaded)::numeric / 1e6, 1) AS shaded_gwh
        FROM nexus3d.building_themes;`);
    const r = rows[0];
    const lossPct = (100 * (1 - r.shaded_gwh / r.unshaded_gwh)).toFixed(1);
    console.log('──────────────────────────────────────────────');
    console.log(`  buildings        : ${r.n}`);
    console.log(`  avg shadow factor: ${r.avg_shadow_factor}  (1.0 = unshaded)`);
    console.log(`  unshaded potential: ${r.unshaded_gwh} GWh/yr`);
    console.log(`  shaded potential  : ${r.shaded_gwh} GWh/yr   (−${lossPct}% from neighbour shading)`);
    console.log('──────────────────────────────────────────────');
} finally {
    client.release();
    await pool.end();
}
