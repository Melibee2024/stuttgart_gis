// src/main.js
// =====================================================================
// 1. INITIALIZE CESIUM VIEWER
// =====================================================================
const viewer = new Cesium.Viewer("cesiumContainer", {
    baseLayerPicker: true,
    geocoder: true,
    timeline: true,
    animation: true,
    baseLayer: new Cesium.ImageryLayer(
        new Cesium.OpenStreetMapImageryProvider({ url: "https://tile.openstreetmap.org/" })
    ),
});

viewer.cesiumWidget.screenSpaceEventHandler.removeInputAction(Cesium.ScreenSpaceEventType.LEFT_DOUBLE_CLICK);

// =====================================================================
// 2. LOAD 3D TILESETS (BASE + IFC)
// =====================================================================
(async () => {
    try {
        // --- A. Load your Base Stuttgart Tileset ---
        const baseTileset = await Cesium.Cesium3DTileset.fromIonAssetId(96188);
        viewer.scene.primitives.add(baseTileset);
        await viewer.zoomTo(baseTileset);

        // --- B. Load your HFT Bau4 IFC Model ---
        // Change this URL to point to your converted 3D Tileset (e.g., /assets/hft_bau4/tileset.json)
        await loadIfcModel('/path/to/your/hft_bau4_tileset/tileset.json');

        console.log("✅ All tilesets loaded successfully.");
    } catch (e) {
        console.error("❌ Failed to load Tilesets:", e);
    }
})();

/**
 * Loads the IFC Tileset and applies the Georeferencing Matrix
 */
async function loadIfcModel(url) {
    try {
        const tileset = await Cesium.Cesium3DTileset.fromUrl(url);

        // --- COORDINATE CONFIGURATION ---
        const lon = 9.1760; // Longitude
        const lat = 48.7750; // Latitude
        const height = 250;  // Height (Adjust for DHHN2016 vertical offset)

        const position = Cesium.Cartesian3.fromDegrees(lon, lat, height);
        tileset.modelMatrix = Cesium.Transforms.eastNorthUpToFixedFrame(position);

        viewer.scene.primitives.add(tileset);
        console.log("📍 HFT Bau4 placed at:", lon, lat);
    } catch (e) {
        console.error("❌ Error loading IFC tileset:", e);
    }
}

// =====================================================================
// 3. UI STATE MANAGEMENT
// =====================================================================
const rightSidebar = document.getElementById('right-sidebar');
const closeSidebarBtn = document.getElementById('closeSidebar');
const alkisTableBody = document.getElementById('alkisTableBody');
const qfieldDataContainer = document.getElementById('qfieldDataContainer');

// Close sidebar button logic
closeSidebarBtn.addEventListener('click', () => {
    rightSidebar.classList.remove('active');
    clearHighlight();
});

const selected = {
    feature: undefined,
    originalColor: new Cesium.Color(),
};

function clearHighlight() {
    if (Cesium.defined(selected.feature)) {
        selected.feature.color = selected.originalColor;
        selected.feature = undefined;
    }
}

// =====================================================================
// 4. FEATURE SELECTION & BACKEND SYNC (LEFT CLICK)
// =====================================================================
viewer.screenSpaceEventHandler.setInputAction(async function (movement) {
    const pickedFeature = viewer.scene.pick(movement.position);

    if (!Cesium.defined(pickedFeature) || !Cesium.defined(pickedFeature.getProperty)) {
        clearHighlight();
        rightSidebar.classList.remove('active');
        return;
    }

    if (selected.feature === pickedFeature) {
        return;
    }

    clearHighlight();
    selected.feature = pickedFeature;
    Cesium.Color.clone(pickedFeature.color, selected.originalColor);
    pickedFeature.color = Cesium.Color.fromCssColorString('#38bdf8').withAlpha(0.8);

    // ---------------------------------------------------------------
    // 🔍 DEBUG: Log every property baked into this tile feature.
    //    Open browser DevTools (F12) → Console tab, click a building,
    //    and look for the "ALL TILE PROPERTIES" line to find the real key name.
    //    Once identified, remove this block and hard-code the correct key below.
    // ---------------------------------------------------------------
    const propertyIds = pickedFeature.getPropertyIds();
    console.log("🔍 ALL TILE PROPERTIES for clicked feature:");
    if (propertyIds.length === 0) {
        console.warn("   ⚠️  No properties found at all — tile may have no metadata.");
    } else {
        propertyIds.forEach(id => {
            console.log(`   ${id} =`, pickedFeature.getProperty(id));
        });
    }
    // ---------------------------------------------------------------

    // Tries the most common 3DCityDB export key names — the console log above
    // will tell you the exact correct name if none of these match.
    const activeId = pickedFeature.getProperty("gml_id")
                  || pickedFeature.getProperty("id")
                  || pickedFeature.getProperty("cityobject_id")
                  || pickedFeature.getProperty("OBJECTID")
                  || pickedFeature.getProperty("building_id");

    rightSidebar.classList.add('active');

    if (!activeId) {
        alkisTableBody.innerHTML = '<tr><td colspan="2" style="color:#ef4444;">No metadata key identifier found in 3D Tile.</td></tr>';
        qfieldDataContainer.innerHTML = '<p style="color:#ef4444;">Cannot perform database queries without a target feature ID key. Check the browser console (F12) for a list of available property names.</p>';
        return;
    }

    populateAlkisTable(pickedFeature);

    // Stream records via the unified identifier parameter
    await fetchDatabaseRecord(activeId);

}, Cesium.ScreenSpaceEventType.LEFT_CLICK);

// =====================================================================
// 5. HELPER FUNCTIONS
// =====================================================================

function populateAlkisTable(feature) {
    const propertyIds = feature.getPropertyIds();
    alkisTableBody.innerHTML = '';

    if (propertyIds.length === 0) {
        alkisTableBody.innerHTML = '<tr><td colspan="2">No local attributes available.</td></tr>';
        return;
    }

    propertyIds.forEach(propId => {
        const value = feature.getProperty(propId);
        const row = document.createElement('tr');
        row.innerHTML = `<th>${propId}</th><td>${value !== null ? value : '-'}</td>`;
        alkisTableBody.appendChild(row);
    });
}

/**
 * Queries the Node.js API to fetch integrated 2D, 3D, and field data streams.
 */
async function fetchDatabaseRecord(targetKey) {
    qfieldDataContainer.innerHTML = '<p style="color: #fbbf24; text-align: center;">⏳ Processing Integrated PostGIS Stream...</p>';

    try {
        const response = await fetch(`http://localhost:5000/api/buildings/${targetKey}`);

        if (!response.ok) {
            throw new Error(`Server returned status ${response.status}`);
        }

        const data = await response.json();

        // Structured template presenting clean, descriptive classifications
        let html = `
            <div style="background: rgba(0,0,0,0.3); padding: 12px; border-radius: 6px; margin-bottom: 10px; border-left: 3px solid #38bdf8;">
                <p style="margin: 0 0 8px 0;"><strong>GML ID (ALKIS):</strong> <span style="color: #cbd5e1; font-family: monospace; font-size: 0.9rem;">${data.gml_id || 'N/A'}</span></p>
                <p style="margin: 0 0 8px 0;"><strong>Measured Height:</strong> <span style="color: #4ade80; font-weight: bold;">${data.measured_height ? data.measured_height + ' m' : 'N/A'}</span></p>
                <p style="margin: 0 0 8px 0;"><strong>Building Function:</strong> <span style="color: #38bdf8;">${data.gebaeudefunktion || 'Unclassified'}</span></p>
                <p style="margin: 0 0 8px 0;"><strong>Construction Year:</strong> ${data.construction_year || 'Unknown'}</p>
                <p style="margin: 0; padding-top: 8px; border-top: 1px solid #475569; font-size: 0.85rem; color: #94a3b8;"><strong>Database Reference index:</strong> #<span id="db-id-val">${data.database_id || 'N/A'}</span></p>
            </div>
            
            <div style="background: rgba(0,0,0,0.2); padding: 12px; border-radius: 6px; margin-bottom: 10px; border-left: 3px solid #a855f7;">
                 <p style="margin: 0 0 4px 0; font-size: 0.9rem; color: #c084fc; font-weight: bold;">QField Field Survey Sync</p>
                 <p style="margin: 0; font-size: 0.85rem; line-height: 1.4; color: #e2e8f0;">${data.photo_notes || 'No field collection feedback log recorded for this urban structure asset.'}</p>
            </div>
        `;

        if (data.file_path) {
            const filename = data.file_path.split('/').pop();
            const photoUrl = `http://localhost:5000/media/${filename}`;

            html += `
                <div class="photo-container">
                    <span class="photo-label">Field Photograph</span>
                    <img src="${photoUrl}" alt="Survey Photo" onerror="this.onerror=null; this.src=''; this.alt='Image file not found on server'; this.style.display='none';">
                </div>
            `;
        } else {
            html += `
                <div class="photo-container" style="text-align: center; padding: 15px 0;">
                    <span class="photo-label">Media Assets</span>
                    <p style="color: #64748b; font-style: italic; font-size: 0.85rem; margin-top: 5px;">No active site photos synchronized.</p>
                </div>
            `;
        }

        qfieldDataContainer.innerHTML = html;

    } catch (error) {
        console.error("API Fetch Error:", error);
        qfieldDataContainer.innerHTML = `
            <div style="background: rgba(239, 68, 68, 0.1); border: 1px solid #ef4444; padding: 10px; border-radius: 4px;">
                <p style="color: #ef4444; margin: 0 0 5px 0;"><strong>Integrated connection unreachable.</strong></p>
                <p style="font-size: 0.8rem; color: #cbd5e1; margin: 0;">Verify backend server node application is live.</p>
            </div>
        `;
    }
}