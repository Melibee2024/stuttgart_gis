// build_solar_roof.mjs
// ROOF-RESOLVED rooftop-PV yield. Upgrades the area-only estimate to use the
// real LoD2 roof geometry already in the database: each building's actual roof
// FACES (citydb BuildingRoofSurface, objectclass 33) give true surface area,
// tilt, and orientation (azimuth) per plane. Re-runnable; run LAST in the chain:
//   1) psql -f build_building_themes.sql      (footprint area + use + base kwh)
//   2) node build_solar_shadow.mjs            (shadow_factor from neighbours)
//   3) node build_solar_roof.mjs              (this — roof-resolved kwh)   ≈30–60 s
//
// METHOD
//   · Roof faces: thematic_surface(objectclass 33) → lod2_multi_surface_id →
//     surface_geometry polygons. Each face's normal is computed with NEWELL's
//     method (robust to the slightly non-planar LoD2 quads that break ST_3DArea),
//     giving tilt β and compass aspect γ; true area = 2D area / cos β.
//   · Orientation factor OF(β,γ): the real Stuttgart sun path (272 positions) is
//     integrated against each face — beam via cos(incidence), plus isotropic
//     diffuse (1+cosβ)/2 — split 55% beam / 45% diffuse (central-Europe annual),
//     then normalised to the optimal plane (35° tilt, due south) so OF≈1 is a
//     perfect roof, ≈0.88 a flat roof, and a north slope is low.
//   · Per building: effective area = Σ(face_area × OF); then
//       kwh_roof = effective_area × 0.70 usable × 0.17 kWp/m² × 1080 kWh/kWp(optimal)
//     and kwh_shaded = kwh_roof × shadow_factor (from step 2).
//   Buildings with no LoD2 roof faces fall back to the footprint estimate.
//
// This makes the model roof-resolved (orientation + true pitched area) rather
// than treating every roof as a flat footprint.

import 'dotenv/config';
import pg from 'pg';

const LAT = 48.7800, DEG = Math.PI / 180;
const USABLE = 0.70;          // panel coverage of the real roof surface
const KWP_PER_M2 = 0.17;      // module power density
const SY_OPTIMAL = 1080;      // kWh/kWp for an optimal (35°, south) Stuttgart roof
const F_BEAM = 0.55, F_DIFF = 0.45;   // central-Europe annual beam/diffuse split

// ── Sun path (same sampling as build_solar_shadow.mjs) ────────────────────────
function sunPath() {
    const doy = [21, 52, 80, 111, 141, 172, 202, 233, 264, 294, 325, 355];
    const φ = LAT * DEG, sun = [];
    for (const N of doy) {
        const δ = 23.45 * DEG * Math.sin(2 * Math.PI * (284 + N) / 365);
        for (let t = 4; t <= 20; t += 0.5) {
            const h = 15 * (t - 12) * DEG;
            const e = Math.asin(Math.max(-1, Math.min(1,
                Math.sin(φ) * Math.sin(δ) + Math.cos(φ) * Math.cos(δ) * Math.cos(h))));
            const elev = e / DEG;
            if (elev <= 3) continue;
            let cosA = (Math.sin(δ) - Math.sin(e) * Math.sin(φ)) / (Math.cos(e) * Math.cos(φ));
            cosA = Math.max(-1, Math.min(1, cosA));
            let A = Math.acos(cosA) / DEG;
            if (h > 0) A = 360 - A;
            sun.push({ azRad: A * DEG, elevRad: e, sinE: Math.sin(e), cosE: Math.cos(e) });
        }
    }
    return sun;
}

const sun = sunPath();
// Scalar normalisers (computed in JS so SQL only needs per-face beam sums).
const BH = sun.reduce((a, s) => a + Math.max(0, s.sinE), 0);     // beam on horizontal
const bOpt = (() => {                                            // beam on optimal plane
    const b = 35 * DEG, cb = Math.cos(b), sb = Math.sin(b), gOpt = 180 * DEG;
    return sun.reduce((a, s) => a + Math.max(0, cb * s.sinE + sb * s.cosE * Math.cos(s.azRad - gOpt)), 0);
})();
const RH_OPT = F_BEAM * (bOpt / BH) + F_DIFF * (1 + Math.cos(35 * DEG)) / 2;
console.log(`[sun] ${sun.length} positions · BH=${BH.toFixed(1)} · optimal-plane RH=${RH_OPT.toFixed(3)}`);

const sunValues = sun.map(s => `(${s.azRad.toFixed(6)},${s.sinE.toFixed(6)},${s.cosE.toFixed(6)})`).join(',');

const pool = new pg.Pool({
    user: process.env.DB_USER ?? 'postgres', host: process.env.DB_HOST ?? 'localhost',
    database: process.env.DB_NAME ?? 'hft_db', password: process.env.DB_PASSWORD,
    port: Number(process.env.DB_PORT) || 5432,
});
const client = await pool.connect();
const time = async (label, sql) => {
    const t0 = Date.now(); const r = await client.query(sql);
    console.log(`[sql] ${label} — ${((Date.now() - t0) / 1000).toFixed(1)} s`); return r;
};

try {
    // 1. Roof faces → gmlid, tilt, aspect (compass), true area, with Newell normal.
    await time('extract roof faces + Newell normals', `
        CREATE TEMP TABLE _rf AS
        WITH rs AS (
            SELECT ts.building_id, ts.lod2_multi_surface_id AS root
            FROM citydb.thematic_surface ts
            WHERE ts.objectclass_id = 33 AND ts.lod2_multi_surface_id IS NOT NULL
        ),
        faces AS (
            SELECT sg.id AS fid, co.gmlid,
                   sg.geometry AS geom,
                   ST_Area(ST_Force2D(sg.geometry)) AS a2d
            FROM rs
            JOIN citydb.cityobject co       ON co.id = rs.building_id
            JOIN citydb.surface_geometry sg ON sg.root_id = rs.root AND sg.geometry IS NOT NULL
            WHERE ST_Area(ST_Force2D(sg.geometry)) > 0.5
        ),
        pts AS (
            SELECT f.fid, (dp).path[2] AS i,
                   ST_X((dp).geom) x, ST_Y((dp).geom) y, ST_Z((dp).geom) z
            FROM faces f, LATERAL ST_DumpPoints(f.geom) dp
            WHERE (dp).path[1] = 1
        ),
        seq AS (
            SELECT fid, x, y, z,
                   lead(x) OVER (PARTITION BY fid ORDER BY i) x2,
                   lead(y) OVER (PARTITION BY fid ORDER BY i) y2,
                   lead(z) OVER (PARTITION BY fid ORDER BY i) z2
            FROM pts
        ),
        newell AS (
            SELECT fid,
                   sum((y-y2)*(z+z2)) AS nx,
                   sum((z-z2)*(x+x2)) AS ny,
                   sum((x-x2)*(y+y2)) AS nz
            FROM seq WHERE x2 IS NOT NULL GROUP BY fid
        )
        SELECT f.fid, f.gmlid, f.a2d,
               -- upward-oriented normal
               (CASE WHEN n.nz < 0 THEN -n.nx ELSE n.nx END) AS nx,
               (CASE WHEN n.nz < 0 THEN -n.ny ELSE n.ny END) AS ny,
               abs(n.nz) AS nz,
               sqrt(n.nx*n.nx + n.ny*n.ny + n.nz*n.nz) AS mag
        FROM newell n JOIN faces f USING (fid);

        ALTER TABLE _rf ADD COLUMN cosb double precision,
                        ADD COLUMN sinb double precision,
                        ADD COLUMN aspect_rad double precision,
                        ADD COLUMN area3d double precision;
        UPDATE _rf SET
            cosb = nz/NULLIF(mag,0),
            sinb = sqrt(GREATEST(0, 1 - (nz/NULLIF(mag,0))^2)),
            aspect_rad = atan2(nx, ny),                 -- compass azimuth (0=N,90=E)
            area3d = a2d / NULLIF(nz/NULLIF(mag,0), 0);
        CREATE INDEX _rf_fix ON _rf (fid);
        CREATE INDEX _rf_gix ON _rf (gmlid);`);

    await time('sun positions', `CREATE TEMP TABLE _sun (az_rad float, sinE float, cosE float);
        INSERT INTO _sun VALUES ${sunValues};`);

    // 2. Per-face beam-in-plane (Σ over sun path of max(0, cos incidence)).
    await time('integrate sun path per roof face (orientation factor)', `
        CREATE TEMP TABLE _facebeam AS
        SELECT rf.fid,
               SUM(GREATEST(0, rf.cosb*s.sinE + rf.sinb*s.cosE*cos(s.az_rad - rf.aspect_rad))) AS beam_inplane
        FROM _rf rf CROSS JOIN _sun s
        GROUP BY rf.fid;
        CREATE INDEX _fb_fix ON _facebeam (fid);`);

    // 3. Per-building: effective (orientation-weighted) roof area → kwh_roof.
    await time('aggregate to buildings + write kwh_roof', `
        CREATE TEMP TABLE _roof AS
        SELECT rf.gmlid,
               SUM(rf.area3d) AS roof_area,
               SUM(rf.area3d * LEAST(1.15,
                     ( ${F_BEAM} * (fb.beam_inplane / ${BH}) + ${F_DIFF} * (1+rf.cosb)/2 ) / ${RH_OPT}
                   )) AS eff_area
        FROM _rf rf JOIN _facebeam fb USING (fid)
        GROUP BY rf.gmlid;

        ALTER TABLE nexus3d.building_themes
            ADD COLUMN IF NOT EXISTS roof_area_m2 numeric,
            ADD COLUMN IF NOT EXISTS kwh_roof     int;
        UPDATE nexus3d.building_themes bt
            SET roof_area_m2 = round(r.roof_area::numeric, 1),
                kwh_roof     = round(r.eff_area * ${USABLE} * ${KWP_PER_M2} * ${SY_OPTIMAL})::int
            FROM _roof r WHERE r.gmlid = bt.gmlid;

        -- final served value: roof-resolved when available, else footprint estimate,
        -- always × neighbour shadow factor.
        UPDATE nexus3d.building_themes
            SET kwh_shaded = round(COALESCE(kwh_roof, kwh) * COALESCE(shadow_factor, 1.0))::int;`);

    const { rows } = await client.query(`
        SELECT count(*) n,
               count(kwh_roof) AS with_roof,
               round(100.0*count(kwh_roof)/count(*),1) AS roof_pct,
               round(sum(COALESCE(kwh_roof, kwh))::numeric/1e6,1)  AS roof_resolved_gwh,
               round(sum(kwh)::numeric/1e6,1)                      AS footprint_gwh,
               round(sum(kwh_shaded)::numeric/1e6,1)               AS final_shaded_gwh
        FROM nexus3d.building_themes;`);
    const r = rows[0];
    console.log('──────────────────────────────────────────────');
    console.log(`  buildings              : ${r.n}`);
    console.log(`  with LoD2 roof faces   : ${r.with_roof} (${r.roof_pct}%)`);
    console.log(`  footprint-only estimate: ${r.footprint_gwh} GWh/yr`);
    console.log(`  roof-resolved estimate : ${r.roof_resolved_gwh} GWh/yr (orientation + true pitched area)`);
    console.log(`  final (× shadow)       : ${r.final_shaded_gwh} GWh/yr  ← served by the API`);
    console.log('──────────────────────────────────────────────');
} finally {
    client.release();
    await pool.end();
}
