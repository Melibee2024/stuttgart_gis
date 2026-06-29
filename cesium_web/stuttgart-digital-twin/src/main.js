// src/main.js
// Nexus3D — CesiumJS frontend
// IFC model loaded as 3D Tiles (pg2b3dm output), not GeoJsonDataSource.

// =====================================================================
// 1. INITIALIZE CESIUM VIEWER
// =====================================================================

// Use your own Cesium ion token if provided (VITE_ION_TOKEN in .env),
// otherwise fall back to Cesium's shared default (rate-limited; shows a banner).
// Must be set BEFORE the Viewer is created (any ion asset call needs it).
if (import.meta.env.VITE_ION_TOKEN) {
    Cesium.Ion.defaultAccessToken = import.meta.env.VITE_ION_TOKEN;
}

const viewer = new Cesium.Viewer("cesiumContainer", {
    baseLayerPicker: true,
    geocoder:        true,
    timeline:        false,
    animation:       false,
    baseLayer: new Cesium.ImageryLayer(
        new Cesium.OpenStreetMapImageryProvider({ url: "https://tile.openstreetmap.org/" })
    ),
    // PERF: render on demand instead of a constant 60fps loop. With a 40k-building
    // city tileset, redrawing every idle frame is the biggest GPU drain. Cesium
    // auto-renders on camera move / input / tile load; we explicitly call
    // viewer.scene.requestRender() (see `render()` below) after programmatic
    // changes to primitives (style, show, clip, feature colour, sun time).
    requestRenderMode:      true,
    maximumRenderTimeChange: Infinity,   // clock is static (no shadow animation)
});

viewer.cesiumWidget.screenSpaceEventHandler.removeInputAction(
    Cesium.ScreenSpaceEventType.LEFT_DOUBLE_CLICK
);

// Request a single frame after a programmatic scene mutation (no-op cost when
// requestRenderMode is off, so it's always safe to call).
const render = () => viewer.scene.requestRender();

// Dev-only handle for debugging in the browser console (stripped from prod builds).
if (import.meta.env.DEV) window.__viewer = viewer;

// PERF: cheap scene-level wins for a large city scene.
viewer.scene.globe.showGroundAtmosphere = false;  // subtle, not needed at city scale
viewer.scene.moon = undefined;                     // never visible at this altitude
viewer.scene.fog.enabled = true;                   // keep: fog culls distant terrain detail

// Apply load/throughput tuning to a 3D Tileset. `aggressive` is for the big
// city base (trade a little distant crispness for far fewer tiles on screen);
// the detailed IFC building stays sharp.
function tunePerformance(ts, { aggressive = false } = {}) {
    if (!ts) return;
    // Distance-based detail falloff — distant city blocks render much coarser.
    ts.dynamicScreenSpaceError       = true;
    ts.dynamicScreenSpaceErrorDensity = 0.00278;
    ts.dynamicScreenSpaceErrorFactor  = aggressive ? 24 : 4;
    // Skip intermediate LODs → reach target detail in fewer requests.
    ts.skipLevelOfDetail        = true;
    ts.baseScreenSpaceError     = 1024;
    ts.skipScreenSpaceErrorFactor = 16;
    ts.skipLevels               = 1;
    ts.preferLeaves             = true;
    ts.preloadWhenHidden        = false;   // don't fetch tiles for hidden tilesets
    ts.cullRequestsWhileMoving  = true;
    if (aggressive) ts.maximumScreenSpaceError = 24;  // coarser city base (was 16)
}

// Backend base URL. Defaults to localhost for dev; override at build time with
// VITE_API_BASE in .env (e.g. when the frontend is served from another host).
const API = import.meta.env.VITE_API_BASE ?? 'http://localhost:5000';

// =====================================================================
// 2. PER-CLASS COLOUR DEFINITIONS
//    IfcStair removed — excluded from ingestion (null geometry).
// =====================================================================

// Real-world palette: HFT "Bau 4 / Block 4" is a reclaimed-TIMBER pavilion with
// a "friendly beige-red" appearance (salvaged wood frame + steel connectors,
// circular-building project, shell completed 2025-03 — matches the IFC). So the
// classes are coloured as warm weathered timber / beige-red, not grey.
const CLASS_COLORS = {
    IfcWall:   '#c5a276',   // beige-red timber facade (dominant)
    IfcSlab:   '#a8865c',   // darker structural timber (floors/decks)
    IfcWindow: '#acc4cf',   // glass
    IfcDoor:   '#8a5836',   // dark timber
    IfcColumn: '#b58c5d',   // timber frame posts
    IfcRoof:   '#b06a4a',   // weathered beige-red roof
    IfcSpace:  '#86efac',
};

// Solid elements are fully OPAQUE so the building reads as a solid mass (no
// see-through "hollow shell" look); only glass (IfcWindow) stays translucent.
const CLASS_STYLE_CONDITIONS = [
    ["${ifc_class} === 'IfcWall'",   "color('#c5a276')"],
    ["${ifc_class} === 'IfcSlab'",   "color('#a8865c')"],
    ["${ifc_class} === 'IfcWindow'", "color('#acc4cf', 0.78)"],
    ["${ifc_class} === 'IfcDoor'",   "color('#8a5836')"],
    ["${ifc_class} === 'IfcColumn'", "color('#b58c5d')"],
    ["${ifc_class} === 'IfcRoof'",   "color('#b06a4a')"],
    ["${ifc_class} === 'IfcSpace'",  "color('#86efac', 0.25)"],
    ["true",                          "color('#c2a07a')"],
];

// Central IFC display state — filters (class/storey) + the storey-slicer cut.
// All three combine into one `show` expression so they stack cleanly.
const ifcState = { ifcClass: null, storey: null, maxZ: null };

function applyIfcStyle() {
    if (!ifcTileset) return;
    const conds = [];
    if (ifcState.ifcClass) conds.push(`\${ifc_class} === '${ifcState.ifcClass}'`);
    if (ifcState.storey)   conds.push(`\${storey} === '${ifcState.storey}'`);
    if (ifcState.maxZ != null) conds.push(`\${z_min_ellipsoidal} <= ${ifcState.maxZ}`);
    const showExpr = conds.length ? conds.join(' && ') : undefined;
    ifcTileset.style = new Cesium.Cesium3DTileStyle({
        color: { conditions: CLASS_STYLE_CONDITIONS },
        ...(showExpr ? { show: showExpr } : {}),
    });
    render();
}

// =====================================================================
// 3. LOAD 3D TILESET (pg2b3dm OUTPUT)
// =====================================================================

let ifcTileset  = null;
let baseTileset = null;   // citydb LoD2 (ion 96188) — module-level for shadows/modes
let bau4Tileset = null;   // citydb LoD2 box for Bau4 only — the BIM-swap counterpart

async function loadIfcTileset(ifcClass = null, storey = null) {
    try {
        if (ifcTileset) {
            viewer.scene.primitives.remove(ifcTileset);
            ifcTileset = null;
        }

        ifcTileset = await Cesium.Cesium3DTileset.fromUrl(
            `${API}/tiles/tileset.json`,
            { maximumScreenSpaceError: 16 }
        );
        tunePerformance(ifcTileset);   // detailed building stays sharp (SSE 16)

        ifcState.ifcClass = ifcClass;
        ifcState.storey   = storey;
        applyIfcStyle();
        viewer.scene.primitives.add(ifcTileset);
        render();

        console.log('✅ IFC 3D Tileset loaded');
    } catch (e) {
        console.error('❌ Failed to load IFC 3D Tileset:', e);
    }
}

// =====================================================================
// 4. HEIGHT ALIGNMENT — snap IFC bottom onto the matching citydb base
//    ─────────────────────────────────────────────────────────────────
//    No hardcoded geoid. The tiles carry the IFC at its RAW EPSG:25832
//    height, so its current rendered bottom == ifc_z_min (from /api/georef).
//
//    We can't ray-cast a building's BASE (a downward ray hits the roof), so
//    instead we ray-cast the matching citydb building's rendered ROOF and
//    subtract that building's own height (citydb_roof_z − citydb_base_z, from
//    the DB) to get its rendered base. Then we shift the IFC tileset so its
//    bottom lands on that base.
//
//    Because the anchor is the citydb BUILDING (not the terrain), the result
//    is terrain-independent — it stays glued whether Cesium terrain is on or
//    off, and works regardless of the ion tileset's vertical datum.
// =====================================================================

// Vertical placement is now baked into the tiles: both the IFC view
// (nexus3d.v_ifc_tiles) and the citydb base (nexus3d.citydb_base_tiles) add the
// Stuttgart geoid undulation (+47.2 m) to convert DHHN2016 orthometric heights
// to WGS84 ellipsoidal, so the tiles sit directly on Cesium World Terrain — no
// runtime modelMatrix shift needed.

// =====================================================================
// 5. TILE REGENERATION
//    Calls POST /api/regenerate-tiles on the backend, which runs
//    pg2b3dm and rewrites ./public/tiles.  After completion the
//    tileset is reloaded so the viewer shows the updated geometry.
// =====================================================================

const regenBtn        = document.getElementById('regenTilesBtn');
const regenStatus     = document.getElementById('regenStatus');

async function regenerateTiles() {
    if (!regenBtn) return;

    regenBtn.disabled    = true;
    regenBtn.textContent = '⏳ Regenerating…';
    if (regenStatus) {
        regenStatus.textContent = 'Running pg2b3dm…';
        regenStatus.style.color = '#b45309';
    }

    try {
        const res = await fetch(`${API}/api/regenerate-tiles`, { method: 'POST' });
        const data = await res.json();

        if (data.success) {
            if (regenStatus) {
                regenStatus.textContent = '✅ Tiles regenerated';
                regenStatus.style.color = '#15803d';
            }
            console.log('[tiles] Regenerated. Reloading tileset…');
            // Tiles carry their own (geoid-shifted) height — just reload.
            const cls    = filterClass?.value  || null;
            const storey = filterStorey?.value || null;
            await loadIfcTileset(cls, storey);
        } else {
            throw new Error(data.error ?? 'Unknown error');
        }
    } catch (err) {
        console.error('[tiles] Regeneration failed:', err);
        if (regenStatus) {
            regenStatus.textContent = `❌ Failed: ${err.message}`;
            regenStatus.style.color = '#dc2626';
        }
    } finally {
        regenBtn.disabled    = false;
        regenBtn.textContent = '🔄 Regenerate Tiles';
    }
}

if (regenBtn) regenBtn.addEventListener('click', regenerateTiles);

// =====================================================================
// 6. BASE TILESET + STARTUP
// =====================================================================

// HFT Bau4 building centre (EPSG:4326). Tiles sit at true ellipsoidal height
// (~287–311 m) on Cesium World Terrain, so the camera target is at that altitude.
const IFC_CENTER_LON = 9.171643;
const IFC_CENTER_LAT = 48.780050;

function flyToIfc(duration = 1.5) {
    return viewer.camera.flyTo({
        destination: Cesium.Cartesian3.fromDegrees(
            IFC_CENTER_LON - 0.0008, IFC_CENTER_LAT - 0.0007, 440
        ),
        orientation: {
            heading: Cesium.Math.toRadians(30),
            pitch:   Cesium.Math.toRadians(-42),
            roll:    0.0,
        },
        duration,
    });
}

(async () => {
    // Cesium World Terrain (ion). With a token in VITE_ION_TOKEN it loads
    // un-throttled; otherwise the shared default token is used (rate-limited).
    try {
        viewer.scene.setTerrain(Cesium.Terrain.fromWorldTerrain());
    } catch (e) {
        console.warn('⚠ World Terrain unavailable — staying on the ellipsoid:', e.message ?? e);
    }

    // Local full-city LoD2 base, tiled from citydb in hft_db (40k buildings).
    // Replaces the partial ion asset so every building shows. Geoid-shifted in
    // the DB view, so it rests on the terrain.
    try {
        baseTileset = await Cesium.Cesium3DTileset.fromUrl(
            `${API}/tiles_citydb/tileset.json`, { maximumScreenSpaceError: 16 }
        );
        tunePerformance(baseTileset, { aggressive: true });  // 40k-building city base
        viewer.scene.primitives.add(baseTileset);
        // City Time Machine: filter each base tile's features by height as they
        // stream in / become visible (see section 6d). No-op until activated.
        baseTileset.tileVisible.addEventListener(tmApplyTile);
        // Color Buildings By: paint each base tile's features for the active
        // thematic layer (see section 6e). No-op until a layer is chosen.
        baseTileset.tileVisible.addEventListener(colorApplyTile);
        render();
        console.log('✅ citydb base tileset loaded');
    } catch (e) {
        console.warn('⚠ citydb base tileset not available:', e.message ?? e);
    }

    // Bau4's citydb LoD2 box as its own small tileset (the full city base no
    // longer contains Bau4 — it was removed so the IFC wouldn't z-fight). Shown
    // by default and clickable for QField photos; hidden when the BIM swap
    // toggle reveals the detailed IFC instead.
    try {
        bau4Tileset = await Cesium.Cesium3DTileset.fromUrl(
            `${API}/tiles_citydb_bau4/tileset.json`, { maximumScreenSpaceError: 16 }
        );
        tunePerformance(bau4Tileset);
        viewer.scene.primitives.add(bau4Tileset);
        console.log('✅ Bau4 citydb box tileset loaded');
    } catch (e) {
        console.warn('⚠ Bau4 citydb box tileset not available:', e.message ?? e);
    }

    try {
        await loadFilters();
        populateLegend();
        await loadIfcTileset();

        await flyToIfc();

        await initModeTools();   // slicer range + shadow defaults
        await setupBaseClip();   // legacy footprint clip on the full-city base (harmless no-op now)
        setBimSwap(false);       // default: show citydb Bau4 box (IFC hidden) → clickable for QField photos
        setMode('ifc');          // IFC-mode panel/tools; reveal the BIM via the Visibility toggle

        console.log('✅ Nexus3D ready.');
    } catch (e) {
        console.error('❌ Startup error:', e);
    }
})();

// =====================================================================
// 7. FILTER PANEL
// =====================================================================

const filterClass    = document.getElementById('filterClass');
const filterStorey   = document.getElementById('filterStorey');
const applyFilterBtn = document.getElementById('applyFilter');
const resetFilterBtn = document.getElementById('resetFilter');

async function loadFilters() {
    try {
        const res  = await fetch(`${API}/api/buildings/filters`);
        const data = await res.json();

        (data.ifc_classes ?? []).forEach(cls => {
            const opt       = document.createElement('option');
            opt.value       = cls;
            opt.textContent = cls;
            filterClass.appendChild(opt);
        });

        (data.storeys ?? []).forEach(s => {
            const opt       = document.createElement('option');
            opt.value       = s;
            opt.textContent = s;
            filterStorey.appendChild(opt);
        });
    } catch (e) {
        console.error('❌ Failed to load filter options:', e);
    }
}

applyFilterBtn.addEventListener('click', () => {
    ifcState.ifcClass = filterClass.value  || null;
    ifcState.storey   = filterStorey.value || null;
    clearHighlight();
    applyIfcStyle();
});

resetFilterBtn.addEventListener('click', () => {
    filterClass.value  = '';
    filterStorey.value = '';
    ifcState.ifcClass  = null;
    ifcState.storey    = null;
    clearHighlight();
    applyIfcStyle();
});

// =====================================================================
// 7b. POPULATE LEGEND (IFC mode)
// =====================================================================

function populateLegend() {
    const el = document.getElementById('legendContainer');
    if (!el) return;
    el.innerHTML = Object.entries(CLASS_COLORS).map(([cls, color]) => `
        <div class="legend-row">
            <span class="legend-dot" style="background:${color};"></span>
            <span>${cls.replace('Ifc', '')}</span>
        </div>
    `).join('');
}

// =====================================================================
// 8. UI STATE — RIGHT SIDEBAR
// =====================================================================

const rightSidebar        = document.getElementById('right-sidebar');
const closeSidebarBtn     = document.getElementById('closeSidebar');
const alkisTableBody      = document.getElementById('alkisTableBody');
const qfieldDataContainer = document.getElementById('qfieldDataContainer');

closeSidebarBtn.addEventListener('click', () => {
    rightSidebar.classList.remove('active');
    clearHighlight();
});

// =====================================================================
// 9. FEATURE SELECTION (LEFT CLICK)
// =====================================================================

const selected = { feature: null };

function clearHighlight() {
    if (!selected.feature) return;
    selected.feature.color = Cesium.Color.WHITE;
    selected.feature = null;
    render();
}

viewer.screenSpaceEventHandler.setInputAction(async function (movement) {
    // Measurement clicks take priority over feature selection.
    if (handleMeasureClick(movement.position)) return;

    const picked = viewer.scene.pick(movement.position);

    if (!Cesium.defined(picked) || !(picked instanceof Cesium.Cesium3DTileFeature)) {
        clearHighlight();
        rightSidebar.classList.remove('active');
        return;
    }

    if (selected.feature === picked) return;

    clearHighlight();
    selected.feature = picked;
    picked.color = Cesium.Color.fromCssColorString('#0d9488').withAlpha(0.9);
    render();

    rightSidebar.classList.add('active');

    // The detailed IFC tileset carries per-element metadata (global_id). The
    // citydb LoD2 base buildings instead carry a `gmlid` — clicking one shows
    // its QField field-survey data (photos keyed by the building's ALKIS id).
    const isIfc = picked.primitive === ifcTileset;

    if (!isIfc) {
        // Base tiles carry the building's ALKIS id (resolved to the root building,
        // so any BuildingPart maps to the right record). Fall back to gmlid.
        const id = picked.getProperty('alkis_id') || picked.getProperty('gmlid');
        if (!id) {
            alkisTableBody.innerHTML =
                '<tr><td colspan="2" style="color:#69788a;">Base building — no identifier.</td></tr>';
            qfieldDataContainer.innerHTML = '';
            return;
        }
        await fetchQfieldRecord(id);   // citydb base building → QField photos
        return;
    }

    const globalId = picked.getProperty('global_id');
    if (!globalId) {
        alkisTableBody.innerHTML =
            '<tr><td colspan="2" style="color:#dc2626;">This IFC element has no global_id.</td></tr>';
        qfieldDataContainer.innerHTML = '';
        return;
    }

    populateFeatureSummary(picked);          // attributes from the tile metadata
    await fetchDatabaseRecord(globalId);     // DB record: psets + QField photos

}, Cesium.ScreenSpaceEventType.LEFT_CLICK);

// =====================================================================
// 10. HELPER FUNCTIONS
// =====================================================================

function populateFeatureSummary(feature) {
    alkisTableBody.innerHTML = '';

    const fields = {
        'IFC Class': feature.getProperty('ifc_class')             ?? '—',
        'Name':      feature.getProperty('name')                  ?? '—',
        'Storey':    feature.getProperty('storey')                ?? '—',
        'Height':    feature.getProperty('element_height_m') != null
                         ? feature.getProperty('element_height_m') + ' m' : '—',
        'Z Base':    feature.getProperty('z_min_ellipsoidal') != null
                         ? feature.getProperty('z_min_ellipsoidal') + ' m' : '—',
        'Z Top':     feature.getProperty('z_max_ellipsoidal') != null
                         ? feature.getProperty('z_max_ellipsoidal') + ' m' : '—',
        'Global ID': feature.getProperty('global_id')             ?? '—',
    };

    Object.entries(fields).forEach(([key, val]) => {
        const row = document.createElement('tr');
        row.innerHTML = `<th>${key}</th><td>${val}</td>`;
        alkisTableBody.appendChild(row);
    });
}

let lastRecord = null;   // cache so mode switches can re-render without re-fetching

async function fetchDatabaseRecord(globalId) {
    qfieldDataContainer.innerHTML =
        '<p style="color:#b45309;text-align:center;">⏳ Querying PostGIS…</p>';
    try {
        const res = await fetch(`${API}/api/buildings/${globalId}`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        lastRecord = await res.json();
        renderProfile(lastRecord);
    } catch (err) {
        console.error('API Fetch Error:', err);
        lastRecord = null;
        qfieldDataContainer.innerHTML = `
            <div style="background:rgba(220,38,38,.06);border:1px solid #dc2626;
                        padding:10px;border-radius:4px;">
                <p style="color:#dc2626;margin:0 0 5px;"><strong>Database connection failed.</strong></p>
                <p style="font-size:.8rem;color:#475467;margin:0;">
                    Ensure the Node.js backend (${API}) is running.
                </p>
            </div>`;
    }
}

// Citydb LoD2 base building → fetch + show its QField field-survey record
// (photos keyed by the building's ALKIS id). Reuses renderProfile's QField
// sections; the __base flag makes them show regardless of the active mode.
async function fetchQfieldRecord(gmlid) {
    alkisTableBody.innerHTML =
        '<tr><td colspan="2" style="color:#b45309;">⏳ Loading building…</td></tr>';
    qfieldDataContainer.innerHTML =
        '<p style="color:#b45309;text-align:center;">⏳ Querying QField data…</p>';
    try {
        const res = await fetch(`${API}/api/qfield/${encodeURIComponent(gmlid)}`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        lastRecord = await res.json();
        lastRecord.__base = true;
        populateBaseSummary(lastRecord);
        renderProfile(lastRecord);
    } catch (err) {
        console.error('QField fetch error:', err);
        lastRecord = null;
        alkisTableBody.innerHTML =
            '<tr><td colspan="2" style="color:#dc2626;">Failed to load building data.</td></tr>';
        qfieldDataContainer.innerHTML = `
            <div style="background:rgba(220,38,38,.06);border:1px solid #dc2626;
                        padding:10px;border-radius:4px;">
                <p style="color:#dc2626;margin:0;"><strong>Could not reach the backend (${API}).</strong></p>
            </div>`;
    }
}

// Top summary table for a clicked base building.
function populateBaseSummary(d) {
    const fields = {
        'ALKIS ID':   d.alkis_id        ?? '—',
        'citydb GML': d.gmlid           ?? '—',
        'Usage':      d.alkis_usage     ?? '—',
        'Year Built': d.alkis_year_built ?? '—',
        'Condition':  d.qfield_condition ?? '—',
        'Photos':     d.photo_count     ?? 0,
    };
    alkisTableBody.innerHTML = Object.entries(fields)
        .map(([k, v]) => `<tr><th>${k}</th><td>${v}</td></tr>`).join('');
}

// Render the right-panel sections relevant to the current mode, so the
// information stays containerised instead of one long cluttered list.
function renderProfile(d) {
    if (!d) { qfieldDataContainer.innerHTML = ''; return; }

    const box = (title, body) => `
        <div style="background:#f6f8fa;border:1px solid #e4e9ef;padding:11px;border-radius:10px;margin-bottom:10px;">
            <p style="margin:0 0 8px;font-weight:500;">${title}</p>${body}
        </div>`;

    const kvTable = rows => `<table style="width:100%;font-size:.82rem;border-collapse:collapse;">${rows}</table>`;
    const kvRow   = (k, v) => `<tr>
        <th style="text-align:left;padding:2px 8px 2px 0;color:#69788a;font-weight:400;white-space:nowrap;">${k}</th>
        <td style="padding:2px 0;color:#1b2733;">${v ?? '—'}</td></tr>`;

    const conditionHtml = () => `
        <div style="background:rgba(15,118,110,.06);padding:12px;border-radius:6px;margin-bottom:10px;border-left:3px solid #0f766e;">
            <p style="margin:0 0 8px;"><strong>Condition:</strong>
               <span style="color:#15803d;">${d.qfield_condition ?? 'N/A'}</span></p>
            <p style="margin:0 0 8px;"><strong>Usage:</strong> ${d.alkis_usage ?? 'N/A'}</p>
            <p style="margin:0;"><strong>Year Built:</strong> ${d.alkis_year_built ?? 'N/A'}</p>
        </div>`;

    const photosHtml = () => {
        if (!(d.field_photos?.length > 0)) return `
            <div class="photo-container" style="text-align:center;padding:20px 0;">
                <span class="photo-label">Field Photographs</span>
                <p style="color:#69788a;font-style:italic;font-size:.85rem;margin-top:5px;">
                    No QField photos recorded for this building yet.
                </p></div>`;
        let h = `<div style="margin-top:4px;"><p style="margin:0 0 8px;font-weight:500;">Field Photographs</p>`;
        d.field_photos.forEach(photo => {
            // file_path is stored like "/media/photos/<file>.jpg" (served from the
            // qfield_data store). Serve it as-is; don't strip the subfolder.
            const fp = photo.file_path || '';
            const photoUrl = fp.startsWith('/media') ? `${API}${fp}` : `${API}/media/${fp.split('/').pop()}`;
            const caption  = photo.photo_name || photo.direction || 'Survey photo';
            h += `
                <div class="photo-container" style="margin-bottom:12px;">
                    <span class="photo-label">${caption}</span>
                    ${photo.notes ? `<p style="font-size:.8rem;color:#69788a;margin:4px 0;">${photo.notes}</p>` : ''}
                    <img src="${photoUrl}" alt="${caption}" style="width:100%;border-radius:4px;"
                         onerror="this.style.display='none'">
                    ${photo.captured_at ? `<p style="font-size:.75rem;color:#69788a;margin:4px 0 0;">${photo.captured_at}</p>` : ''}
                </div>`;
        });
        return h + `</div>`;
    };

    const ifcAttrsHtml = () => {
        if (!(d.ifc_attributes && Object.keys(d.ifc_attributes).length > 0)) return '';
        return box('IFC Attributes',
            kvTable(Object.entries(d.ifc_attributes).map(([k, v]) => kvRow(k, v)).join('')));
    };

    const psetsHtml = () => {
        if (!(d.pset_properties?.length > 0)) return '';
        return box('Property Sets', kvTable(d.pset_properties.map(p =>
            kvRow(p.name, p.val_string ?? p.val_double ?? p.val_int)).join('')));
    };

    const summaryHtml = () => box('Geometry', kvTable(
        kvRow('Z Base', d.z_min_ellipsoidal != null ? d.z_min_ellipsoidal.toFixed(2) + ' m' : null) +
        kvRow('Z Top',  d.z_max_ellipsoidal != null ? d.z_max_ellipsoidal.toFixed(2) + ' m' : null) +
        kvRow('Last modified', d.last_modified)));

    // QField field photos stay reachable in BOTH surviving modes (the dedicated
    // QField mode was removed): clicking the detailed IFC building shows its
    // photos next to the IFC data, and any citydb base building shows them too.
    let sections;
    if (d.__base)                      sections = [conditionHtml(), photosHtml()];
    else if (currentMode === '3dcity') sections = [summaryHtml(), photosHtml()];
    else /* ifc */                     sections = [ifcAttrsHtml(), psetsHtml(), photosHtml()];

    qfieldDataContainer.innerHTML =
        sections.filter(Boolean).join('') ||
        '<p class="mode-hint">No data for this element in this view.</p>';
}

// =====================================================================
// 6b. MODE SWITCHER + 3D-CITY TOOLS (slicer + solar/shadow study)
// =====================================================================

let currentMode = 'ifc';

function setMode(mode) {
    currentMode = mode;
    const map = { ifc: 'modeIfc', '3dcity': 'mode3dcity' };
    const panels = { ifc: 'panel-ifc', '3dcity': 'panel-3dcity' };
    Object.entries(map).forEach(([m, id]) =>
        document.getElementById(id)?.classList.toggle('active', m === mode));
    Object.entries(panels).forEach(([m, id]) =>
        document.getElementById(id)?.classList.toggle('active', m === mode));

    // Keep each mode self-contained. Leaving IFC resets the storey slice;
    // leaving 3D-City turns off shadows. (The "hide citydb" clip persists as a
    // global visibility preference; only Reset to Default restores it.)
    if (mode !== 'ifc') {
        ifcState.maxZ = null;
        applyIfcStyle();
        if (sliceSlider && ifcZMax != null) sliceSlider.value = ifcZMax;
        if (sliceLabel) sliceLabel.textContent = 'Cut height: full building';
    }
    if (mode !== '3dcity') {
        enableShadows(false);
        const t = document.getElementById('shadowToggle'); if (t) t.checked = false;
        // Restore the full city when leaving 3D-City so other modes never show a
        // half-revealed skyline.
        if (tmActive) { if (tmToggle) tmToggle.checked = false; tmSetActive(false); }
        // Drop thematic colouring so the IFC view shows the plain base buildings.
        if (colorMode !== 'none') { if (colorBy) colorBy.value = 'none'; setColorMode('none'); }
    }

    // Re-render the open asset profile for the new mode.
    if (selected.feature && lastRecord) renderProfile(lastRecord);
}

document.getElementById('modeIfc')?.addEventListener('click', () => setMode('ifc'));
document.getElementById('mode3dcity')?.addEventListener('click', () => setMode('3dcity'));

// ── Storey slicer ────────────────────────────────────────────────────
let ifcZMin = null, ifcZMax = null;
const sliceSlider = document.getElementById('sliceSlider');
const sliceLabel  = document.getElementById('sliceLabel');
const sliceReset  = document.getElementById('sliceReset');

function updateSliceLabel(cut) {
    if (!sliceLabel) return;
    sliceLabel.textContent = (ifcZMin != null)
        ? `Cut height: ${(cut - ifcZMin).toFixed(1)} m above base`
        : `Cut: ${(+cut).toFixed(1)} m`;
}

if (sliceSlider) sliceSlider.addEventListener('input', () => {
    const cut = parseFloat(sliceSlider.value);
    ifcState.maxZ = (ifcZMax != null && cut >= ifcZMax) ? null : cut;  // at top → show all
    applyIfcStyle();
    updateSliceLabel(cut);
});

if (sliceReset) sliceReset.addEventListener('click', () => {
    if (sliceSlider && ifcZMax != null) sliceSlider.value = ifcZMax;
    ifcState.maxZ = null;
    applyIfcStyle();
    if (sliceLabel) sliceLabel.textContent = 'Cut height: full building';
});

// ── Solar / shadow study ─────────────────────────────────────────────
const shadowToggle = document.getElementById('shadowToggle');
const shadowTime   = document.getElementById('shadowTime');
const shadowDate   = document.getElementById('shadowDate');
const shadowTimeLabel = document.getElementById('shadowTimeLabel');

function enableShadows(on) {
    viewer.shadows = on;
    viewer.scene.globe.enableLighting = on;
    const mode = on ? Cesium.ShadowMode.ENABLED : Cesium.ShadowMode.DISABLED;
    if (baseTileset) baseTileset.shadows = mode;
    if (ifcTileset)  ifcTileset.shadows  = mode;
    if (on) updateSunTime();
    render();
}

function updateSunTime() {
    const dateStr = shadowDate?.value || new Date().toISOString().slice(0, 10);
    const mins = parseInt(shadowTime?.value ?? '720', 10);
    const hh = Math.floor(mins / 60), mm = mins % 60;
    if (shadowTimeLabel)
        shadowTimeLabel.textContent = `Time: ${String(hh).padStart(2,'0')}:${String(mm).padStart(2,'0')}`;
    // Treat slider as Stuttgart local time (CEST ≈ UTC+2) → convert to UTC.
    const iso = `${dateStr}T${String(hh).padStart(2,'0')}:${String(mm).padStart(2,'0')}:00`;
    let jd = Cesium.JulianDate.fromIso8601(iso);
    jd = Cesium.JulianDate.addHours(jd, -2, new Cesium.JulianDate());
    viewer.clock.shouldAnimate = false;
    viewer.clock.currentTime = jd;
    render();
}

if (shadowToggle) shadowToggle.addEventListener('change', () => enableShadows(shadowToggle.checked));
if (shadowTime)   shadowTime.addEventListener('input', () => {
    if (viewer.shadows) updateSunTime();
    else updateSunTime();   // keep the label live even before enabling
});
if (shadowDate)   shadowDate.addEventListener('change', () => { if (viewer.shadows) updateSunTime(); });

// Fetch the IFC height range (for the slicer) + set shadow defaults.
async function initModeTools() {
    try {
        const g = await (await fetch(`${API}/api/georef`)).json();
        ifcZMin = g.ifc_z_min;
        ifcZMax = g.ifc_z_max;
        if (sliceSlider && ifcZMin != null && ifcZMax != null) {
            sliceSlider.min   = ifcZMin;
            sliceSlider.max   = ifcZMax;
            sliceSlider.step  = Math.max(0.1, (ifcZMax - ifcZMin) / 200);
            sliceSlider.value = ifcZMax;
        }
    } catch (e) {
        console.warn('[modeTools] could not fetch IFC height range', e);
    }
    if (shadowDate && !shadowDate.value) shadowDate.value = '2024-06-21';  // summer solstice
    updateSunTime();   // initialise the time label
}

// ── citydb base visibility ────────────────────────────────────────────
// Two independent things:
//  1. baseClip — ALWAYS on. Clips out the one citydb LoD2 building that sits
//     directly under the IFC (its blocky twin) so it doesn't z-fight the
//     detailed IFC. The rest of the city stays.
//  2. hideBaseToggle — hides the ENTIRE citydb base tileset (every building),
//     for a clean IFC-only view.
let baseClip = null;
const hideBaseToggle = document.getElementById('hideBaseToggle');

async function setupBaseClip() {
    try {
        const g = await (await fetch(`${API}/api/georef`)).json();
        if (!g.footprint_geojson || !baseTileset) return;
        const gj = JSON.parse(g.footprint_geojson);
        const rings = gj.type === 'MultiPolygon' ? gj.coordinates : [gj.coordinates];
        const polygons = rings.map(poly => new Cesium.ClippingPolygon({
            positions: Cesium.Cartesian3.fromDegreesArray(poly[0].flat()),
        }));
        // inverse:false (default) → clip regions INSIDE any polygon, i.e. remove
        // only the citydb twin under the IFC, leaving the rest of the city.
        // (inverse:true would clip everything OUTSIDE the footprint — the whole
        // city — which is the opposite of what we want.)
        baseClip = new Cesium.ClippingPolygonCollection({ polygons, inverse: false });
        baseClip.enabled = true;
        baseTileset.clippingPolygons = baseClip;
        render();
        console.log('[clip] base footprint clip ready (IFC twin always hidden)');
    } catch (e) {
        console.warn('[clip] base footprint setup failed — IFC twin may z-fight', e);
    }
}

// Toggle = hide ALL citydb buildings (not just the twin).
if (hideBaseToggle) hideBaseToggle.addEventListener('change', () => {
    if (baseTileset) baseTileset.show = !hideBaseToggle.checked;
    render();
});

// ── BIM swap (Bau4) ───────────────────────────────────────────────────
// Bau4 exists as two separate tilesets: the citydb LoD2 box (bau4Tileset) and
// the detailed IFC (ifcTileset). Exactly one is shown. OFF (default) = citydb
// box, clickable to view/attach QField photos; ON = detailed BIM model.
const bimSwapToggle = document.getElementById('bimSwapToggle');

function setBimSwap(on) {
    if (ifcTileset)  ifcTileset.show  = on;
    if (bau4Tileset) bau4Tileset.show = !on;
    render();
}

if (bimSwapToggle) bimSwapToggle.addEventListener('change', () => setBimSwap(bimSwapToggle.checked));

// =====================================================================
// 6d. CITY TIME MACHINE — reveal the city by building height
//     ─────────────────────────────────────────────────────────────────
//     Building Height Filter. Drives the reveal by measured building
//     height (~90% coverage): drag or play the slider and buildings peel
//     off shortest-first. Height is also a strong free proxy for building
//     type, so the readout names the tallest visible typology band.
//
//     Non-destructive: no re-tiling. The base tiles already expose `gmlid`,
//     so we fetch a {gmlid: height} map once (/api/building-heights) and
//     toggle each feature's `.show` from the tileVisible callback. A
//     `tmEverUsed` guard keeps it zero-cost until the user opens it; after
//     that, every visible tile is reconciled each render pass so panning
//     away and back never leaves a building wrongly hidden.
// =====================================================================

const tmToggle = document.getElementById('tmToggle');
const tmSlider = document.getElementById('tmSlider');
const tmLabel  = document.getElementById('tmLabel');
const tmPlay   = document.getElementById('tmPlay');
const tmReset  = document.getElementById('tmReset');
const tmCount  = document.getElementById('tmCount');

let tmActive    = false;
let tmEverUsed  = false;
let tmThreshold = Infinity;     // show buildings whose height ≤ threshold
let tmHeights   = null;         // Map<gmlid, height-m>
let tmSorted    = null;         // heights sorted asc, for fast live counts
let tmMin = 0, tmMax = 100, tmTotal = 0;
let tmPlaying = false, tmRaf = null;

// Per-tile feature filter. Registered on baseTileset.tileVisible (section 6),
// so it runs for visible tiles each render pass — covering tile streaming,
// camera moves, and slider changes (which call render()).
function tmApplyTile(tile) {
    if (!tmEverUsed) return;            // zero cost until the feature is used
    const apply = (content) => {
        if (!content) return;
        const n = content.featuresLength || 0;
        for (let i = 0; i < n; i++) {
            const f = content.getFeature(i);
            if (!f) continue;
            if (!tmActive) { f.show = true; continue; }
            const h = tmHeights ? tmHeights.get(f.getProperty('gmlid')) : null;
            f.show = (h == null) ? true : (h <= tmThreshold);
        }
    };
    const c = tile.content;
    if (c && c.innerContents) c.innerContents.forEach(apply);
    else apply(c);
}

async function loadTimeMachineData() {
    if (tmHeights) return true;
    try {
        const d = await (await fetch(`${API}/api/building-heights`)).json();
        if (!d || !d.heights) throw new Error('no height data');
        tmHeights = new Map(Object.entries(d.heights).map(([k, v]) => [k, +v]));
        tmSorted  = Float64Array.from(tmHeights.values()).sort();
        tmTotal   = d.count ?? tmHeights.size;
        tmMin     = Math.max(0, Math.floor(d.min ?? 0));
        tmMax     = Math.ceil(d.p99 ?? d.max ?? 100);   // cap at p99; outliers reveal at top
        if (tmSlider) {
            tmSlider.min   = tmMin;
            tmSlider.max   = tmMax;
            tmSlider.step  = Math.max(0.5, +((tmMax - tmMin) / 200).toFixed(2));
            tmSlider.value = tmMax;
        }
        return true;
    } catch (e) {
        console.warn('[timeMachine] could not load building heights', e);
        if (tmCount) tmCount.textContent = 'Height data unavailable (backend offline?)';
        return false;
    }
}

// Buildings with height ≤ t — binary search on the sorted height array.
function tmCountBelow(t) {
    if (!tmSorted) return 0;
    if (t >= tmMax) return tmTotal;
    let lo = 0, hi = tmSorted.length;
    while (lo < hi) { const m = (lo + hi) >> 1; if (tmSorted[m] <= t) lo = m + 1; else hi = m; }
    return lo;
}

// Height is a strong free proxy for building type — name the tallest band
// currently revealed so the count line carries meaning, not just a number.
function tmBand(t) {
    if (t <= 4)  return 'sheds & garages';
    if (t <= 10) return 'houses & low-rise';
    if (t <= 25) return 'apartment & office blocks';
    return 'high-rise & towers';
}

function tmUpdateLabel() {
    if (tmLabel) {
        if (!tmActive) tmLabel.textContent = 'Showing buildings ≤ — m';
        else tmLabel.textContent =
            `Showing buildings ≤ ${tmThreshold >= tmMax ? 'full city' : tmThreshold.toFixed(1) + ' m'}`;
    }
    if (tmCount) {
        if (!tmActive) { tmCount.textContent = ''; return; }
        const shown = tmThreshold >= tmMax ? tmTotal : tmCountBelow(tmThreshold);
        const band  = tmThreshold >= tmMax ? 'all types' : `up to ${tmBand(tmThreshold)}`;
        tmCount.textContent =
            `${shown.toLocaleString()} / ${tmTotal.toLocaleString()} buildings · ${band}`;
    }
}

function tmRefresh() { render(); tmUpdateLabel(); }   // render() re-fires tileVisible

function tmStepTo(v) {
    tmThreshold = v;
    if (tmSlider && Math.abs(parseFloat(tmSlider.value) - v) > 1e-6) tmSlider.value = v;
    tmRefresh();
}

function tmStopPlay() {
    tmPlaying = false;
    if (tmRaf) cancelAnimationFrame(tmRaf);
    tmRaf = null;
    if (tmPlay) tmPlay.innerHTML = '<svg class="icon"><use href="#i-play"/></svg>Play';
}

function tmStartPlay() {
    if (!tmActive) return;
    tmPlaying = true;
    if (tmPlay) tmPlay.innerHTML = '<svg class="icon"><use href="#i-pause"/></svg>Pause';
    const to = tmMax, dur = 7000;
    if (tmThreshold >= tmMax) tmThreshold = tmMin;       // replay from the ground
    const fromVal = tmThreshold;
    let start = null;
    const frame = (ts) => {
        if (!tmPlaying) return;
        if (start == null) start = ts;
        const p = Math.min(1, (ts - start) / dur);
        tmStepTo(fromVal + (to - fromVal) * p);
        if (p < 1) tmRaf = requestAnimationFrame(frame);
        else tmStopPlay();
    };
    tmRaf = requestAnimationFrame(frame);
}

async function tmSetActive(on) {
    if (on && !(await loadTimeMachineData())) { if (tmToggle) tmToggle.checked = false; return; }
    tmActive = on;
    if (on) tmEverUsed = true;
    [tmSlider, tmPlay, tmReset].forEach(el => { if (el) el.disabled = !on; });
    if (on) {
        tmThreshold = tmSlider ? parseFloat(tmSlider.value) : tmMax;
    } else {
        tmStopPlay();
        tmThreshold = Infinity;
    }
    tmRefresh();   // render() reconciles every visible tile to the new state
}

if (tmToggle) tmToggle.addEventListener('change', () => tmSetActive(tmToggle.checked));
if (tmSlider) tmSlider.addEventListener('input', () => { tmStopPlay(); tmStepTo(parseFloat(tmSlider.value)); });
if (tmPlay)   tmPlay.addEventListener('click', () => (tmPlaying ? tmStopPlay() : tmStartPlay()));
if (tmReset)  tmReset.addEventListener('click', () => { tmStopPlay(); if (tmSlider) tmSlider.value = tmMax; tmStepTo(tmMax); });

// =====================================================================
// 6e. COLOR BUILDINGS BY — thematic shading of the citydb base city
//     ─────────────────────────────────────────────────────────────────
//     One engine, three data layers, all painted onto base 3D-tile
//     features via the same baseTileset.tileVisible pass used by the
//     height filter (§6d):
//       · use    — ALKIS use category (7 classes), /api/building-themes
//       · height — measured-height band (4 classes), reuses tmHeights (§6d)
//       · solar  — estimated annual rooftop-PV yield (5 kWh quintiles),
//                  /api/building-themes (model documented in build_building_themes.sql)
//     Non-destructive & lazy: zero cost until a layer is chosen
//     (colorEverUsed guard); the selected-feature highlight is preserved
//     by skipping it while repainting.
// =====================================================================

const colorBy     = document.getElementById('colorBy');
const colorLegend = document.getElementById('colorLegend');
const colorStat   = document.getElementById('colorStat');

// Palettes (hex for the legend; Cesium.Color built once for the render loop).
const USE_HEX    = ['#5b8def', '#9b59b6', '#e67e22', '#7f8c8d', '#e84393', '#b8c2cc', '#dfe4ea'];
const HEIGHT_HEX = ['#c7e9b4', '#7fcdbb', '#2c7fb8', '#253494'];
const SOLAR_HEX  = ['#ffffcc', '#fed976', '#feb24c', '#fd8d3c', '#e31a1c'];
const NODATA_HEX = '#cfd4da';

const HEIGHT_LABELS = [
    '≤ 4 m · sheds & garages',
    '4–10 m · houses & low-rise',
    '10–25 m · apartment & office blocks',
    '> 25 m · high-rise & towers',
];

const toCol      = h => Cesium.Color.fromCssColorString(h);
const CL_USE     = USE_HEX.map(toCol);
const CL_HEIGHT  = HEIGHT_HEX.map(toCol);
const CL_SOLAR   = SOLAR_HEX.map(toCol);
const CL_NODATA  = toCol(NODATA_HEX);
const CL_WHITE   = Cesium.Color.WHITE;

let colorMode    = 'none';   // 'none' | 'use' | 'height' | 'solar'
let colorEverUsed = false;
let themeData    = null;     // { uses:{gmlid:code}, solar:{gmlid:kwh}, solar_breaks, use_labels, solar_total_gwh }

async function loadThemeData() {
    if (themeData) return true;
    try {
        const d = await (await fetch(`${API}/api/building-themes`)).json();
        if (!d || !d.uses) throw new Error('no theme data');
        themeData = d;
        return true;
    } catch (e) {
        console.warn('[colorBy] could not load building themes', e);
        if (colorStat) colorStat.textContent = 'Theme data unavailable (backend offline?)';
        return false;
    }
}

function heightBandIdx(h) { return h <= 4 ? 0 : h <= 10 ? 1 : h <= 25 ? 2 : 3; }

function solarBinIdx(k) {
    const b = themeData?.solar_breaks;
    if (!b || b.length < 4) return 0;
    return k < b[0] ? 0 : k < b[1] ? 1 : k < b[2] ? 2 : k < b[3] ? 3 : 4;
}

// Colour one base feature for the active layer (CL_NODATA when unclassified).
function colorForFeature(f) {
    const gmlid = f.getProperty('gmlid');
    if (colorMode === 'use') {
        const c = themeData?.uses?.[gmlid];
        return (c == null) ? CL_NODATA : (CL_USE[c] || CL_NODATA);
    }
    if (colorMode === 'height') {
        const h = tmHeights ? tmHeights.get(gmlid) : null;
        return (h == null) ? CL_NODATA : CL_HEIGHT[heightBandIdx(h)];
    }
    if (colorMode === 'solar') {
        const k = themeData?.solar?.[gmlid];
        return (k == null) ? CL_NODATA : CL_SOLAR[solarBinIdx(k)];
    }
    return CL_WHITE;
}

// Per-tile painter, registered on baseTileset.tileVisible (section 6).
function colorApplyTile(tile) {
    if (!colorEverUsed) return;                 // zero cost until a layer is chosen
    const apply = (content) => {
        if (!content) return;
        const n = content.featuresLength || 0;
        for (let i = 0; i < n; i++) {
            const f = content.getFeature(i);
            if (!f) continue;
            if (f === selected.feature) continue;   // keep the click highlight
            f.color = (colorMode === 'none') ? CL_WHITE : colorForFeature(f);
        }
    };
    const c = tile.content;
    if (c && c.innerContents) c.innerContents.forEach(apply);
    else apply(c);
}

function legendRow(hex, label) {
    return `<div class="legend-row"><span class="legend-dot" style="background:${hex};"></span><span>${label}</span></div>`;
}

function renderColorLegend() {
    if (!colorLegend) return;
    let rows = '', stat = '';
    if (colorMode === 'use') {
        const labels = themeData?.use_labels || [];
        rows = labels.map((lab, i) => legendRow(USE_HEX[i] || NODATA_HEX, lab)).join('')
             + legendRow(NODATA_HEX, 'Unmapped');
    } else if (colorMode === 'height') {
        rows = CL_HEIGHT.map((_, i) => legendRow(HEIGHT_HEX[i], HEIGHT_LABELS[i])).join('');
    } else if (colorMode === 'solar') {
        const b = themeData?.solar_breaks || [];
        const fmt = v => v >= 1000 ? (v / 1000).toFixed(v >= 10000 ? 0 : 1) + 'k' : String(v);
        const labs = (b.length === 4)
            ? [`< ${fmt(b[0])} kWh/yr`, `${fmt(b[0])}–${fmt(b[1])}`, `${fmt(b[1])}–${fmt(b[2])}`,
               `${fmt(b[2])}–${fmt(b[3])}`, `> ${fmt(b[3])} kWh/yr`]
            : ['Very low', 'Low', 'Moderate', 'High', 'Very high'];
        rows = SOLAR_HEX.map((c, i) => legendRow(c, labs[i])).join('');
        if (themeData?.solar_total_gwh != null) {
            stat = `≈ ${(+themeData.solar_total_gwh).toLocaleString()} GWh/yr citywide rooftop potential`;
            // Roof-resolved (real LoD2 pitch + orientation), shadow-adjusted.
            const un = +themeData.solar_total_unshaded_gwh;
            if (un > 0 && themeData.shadow_avg != null) {
                const loss = (100 * (1 - themeData.solar_total_gwh / un)).toFixed(1);
                stat += ` · roof-resolved, shadow-adjusted (−${loss}% from neighbour shading)`;
            }
        }
    }
    colorLegend.innerHTML = rows;
    if (colorStat) colorStat.textContent = stat;
}

async function setColorMode(mode) {
    // Load whatever the chosen layer needs; bail back to 'none' if it fails.
    if (mode === 'use' || mode === 'solar') {
        if (!(await loadThemeData())) { if (colorBy) colorBy.value = 'none'; mode = 'none'; }
    } else if (mode === 'height') {
        if (!(await loadTimeMachineData())) { if (colorBy) colorBy.value = 'none'; mode = 'none'; }
    }
    colorMode = mode;
    if (mode !== 'none') colorEverUsed = true;
    renderColorLegend();
    render();   // re-fires tileVisible → repaints every visible base tile
}

if (colorBy) colorBy.addEventListener('change', () => setColorMode(colorBy.value));

// ── Reset everything to the default view ──────────────────────────────
document.getElementById('resetAll')?.addEventListener('click', () => {
    if (filterClass)  filterClass.value  = '';
    if (filterStorey) filterStorey.value = '';
    ifcState.ifcClass = null; ifcState.storey = null; ifcState.maxZ = null;
    applyIfcStyle();

    if (sliceSlider && ifcZMax != null) sliceSlider.value = ifcZMax;
    if (sliceLabel) sliceLabel.textContent = 'Cut height: full building';

    enableShadows(false);
    const st = document.getElementById('shadowToggle'); if (st) st.checked = false;

    if (baseTileset) baseTileset.show = true;
    if (hideBaseToggle) hideBaseToggle.checked = false;
    if (bimSwapToggle) bimSwapToggle.checked = false;
    setBimSwap(false);                          // default: citydb Bau4 shown, IFC hidden

    if (colorBy) colorBy.value = 'none';
    setColorMode('none');

    clearMeasurements();
    clearHighlight();
    rightSidebar.classList.remove('active');

    setMode('ifc');
    if (ifcTileset) flyToIfc();
});

// =====================================================================
// 6c. SPATIAL TOOLS — measure distance / height delta
// =====================================================================

let measureMode = null;          // 'distance' | 'height' | null
let measurePts  = [];
const measureEntities = [];
const measureFeedback = document.getElementById('measureFeedback');

function setFeedback(msg) {
    if (!measureFeedback) return;
    measureFeedback.style.display = msg ? 'block' : 'none';
    measureFeedback.textContent = msg || '';
}

function startMeasure(mode) {
    measureMode = mode;
    measurePts = [];
    setFeedback(`Click two points to measure ${mode === 'distance' ? 'distance' : 'height delta'}.`);
}

function clearMeasurements() {
    measureEntities.forEach(e => viewer.entities.remove(e));
    measureEntities.length = 0;
    measureMode = null;
    measurePts = [];
    setFeedback('');
}

function addMeasurePoint(pos) {
    measureEntities.push(viewer.entities.add({
        position: pos,
        point: { pixelSize: 9, color: Cesium.Color.YELLOW,
                 outlineColor: Cesium.Color.BLACK, outlineWidth: 1,
                 disableDepthTestDistance: Number.POSITIVE_INFINITY },
    }));
}

function addMeasureLabel(pos, text, color) {
    measureEntities.push(viewer.entities.add({
        position: pos,
        label: { text, font: '14px sans-serif', fillColor: color,
                 showBackground: true, backgroundColor: new Cesium.Color(0, 0, 0, 0.7),
                 style: Cesium.LabelStyle.FILL, pixelOffset: new Cesium.Cartesian2(0, -12),
                 disableDepthTestDistance: Number.POSITIVE_INFINITY },
    }));
}

// Returns true if the click was consumed by an active measurement.
function handleMeasureClick(windowPos) {
    if (!measureMode) return false;
    const pos = viewer.scene.pickPosition(windowPos);
    if (!Cesium.defined(pos)) {
        setFeedback('Could not read a point there — aim at a surface and retry.');
        return true;
    }
    measurePts.push(pos);
    addMeasurePoint(pos);

    if (measurePts.length < 2) { setFeedback('Click the second point…'); return true; }

    const [a, b] = measurePts;
    const mid = Cesium.Cartesian3.midpoint(a, b, new Cesium.Cartesian3());
    if (measureMode === 'distance') {
        const d = Cesium.Cartesian3.distance(a, b);
        measureEntities.push(viewer.entities.add({
            polyline: { positions: [a, b], width: 3, material: Cesium.Color.YELLOW,
                        clampToGround: false } }));
        addMeasureLabel(mid, `${d.toFixed(2)} m`, Cesium.Color.YELLOW);
        setFeedback(`Distance: ${d.toFixed(2)} m  (click a tool to measure again)`);
    } else {
        const ha = Cesium.Cartographic.fromCartesian(a).height;
        const hb = Cesium.Cartographic.fromCartesian(b).height;
        const dz = Math.abs(ha - hb);
        measureEntities.push(viewer.entities.add({
            polyline: { positions: [a, b], width: 3, material: Cesium.Color.ORANGE } }));
        addMeasureLabel(mid, `Δh ${dz.toFixed(2)} m`, Cesium.Color.ORANGE);
        setFeedback(`Height delta: ${dz.toFixed(2)} m  (click a tool to measure again)`);
    }
    measureMode = null;
    measurePts = [];
    return true;
}

document.getElementById('btnDist')?.addEventListener('click', () => startMeasure('distance'));
document.getElementById('btnHeight')?.addEventListener('click', () => startMeasure('height'));
document.getElementById('btnClearTools')?.addEventListener('click', clearMeasurements);

// =====================================================================
// 11. RESIZABLE SIDEBARS
//     Drag the thin handle on the inner edge of either sidebar to resize.
//     The map fills the remaining space (flex), so we just resize the
//     sidebar and tell Cesium to recompute its canvas.
// =====================================================================

function makeResizable(handle, panel, { side, min, max }) {
    if (!handle || !panel) return;

    handle.addEventListener('mousedown', (e) => {
        e.preventDefault();
        const startX = e.clientX;
        const startWidth = panel.getBoundingClientRect().width;
        handle.classList.add('is-dragging');
        document.body.style.cursor = 'ew-resize';
        document.body.style.userSelect = 'none';

        const onMove = (ev) => {
            const dx = ev.clientX - startX;
            const delta = side === 'left' ? dx : -dx;
            const width = Math.min(max, Math.max(min, startWidth + delta));
            panel.style.width = `${width}px`;
            viewer.resize();
            render();
        };
        const onUp = () => {
            document.removeEventListener('mousemove', onMove);
            document.removeEventListener('mouseup', onUp);
            handle.classList.remove('is-dragging');
            document.body.style.cursor = '';
            document.body.style.userSelect = '';
        };
        document.addEventListener('mousemove', onMove);
        document.addEventListener('mouseup', onUp);
    });
}

makeResizable(document.getElementById('resize-left'),  document.getElementById('left-sidebar'),  { side: 'left',  min: 230, max: 520 });
makeResizable(document.getElementById('resize-right'), document.getElementById('right-sidebar'), { side: 'right', min: 300, max: 680 });
