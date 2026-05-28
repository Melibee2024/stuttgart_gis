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
        // REPLACE THESE WITH YOUR QGIS/SURVEY COORDINATES
        const lon = 9.1760; // Longitude
        const lat = 48.7750; // Latitude
        const height = 250;  // Height (Adjust for DHHN2016 vertical offset)

        // Create the Transformation Matrix
        // This anchors the "0,0,0" of your IFC file to the real-world location
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

// Highlighting state container
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

    // If clicked on the sky or ground, clear everything and close sidebar
    if (!Cesium.defined(pickedFeature) || !Cesium.defined(pickedFeature.getProperty)) {
        clearHighlight();
        rightSidebar.classList.remove('active');
        return;
    }

    // If clicked the exact same building again, do nothing
    if (selected.feature === pickedFeature) {
        return;
    }

    // 4A. HANDLE VISUAL HIGHLIGHTING
    clearHighlight();
    selected.feature = pickedFeature;
    Cesium.Color.clone(pickedFeature.color, selected.originalColor);
    // Highlight with a transparent bright blue
    pickedFeature.color = Cesium.Color.fromCssColorString('#38bdf8').withAlpha(0.8);

    // 4B. EXTRACT IDENTIFIER
    // Tries to find the ID using common property names. Adjust if your tileset uses a different key.
    const gmlid = pickedFeature.getProperty("OBJECTID") || pickedFeature.getProperty("gml_id");

    // Open the sidebar UI
    rightSidebar.classList.add('active');

    if (!gmlid) {
        alkisTableBody.innerHTML = '<tr><td colspan="2" style="color:#ef4444;">No ID found in 3D Tile</td></tr>';
        qfieldDataContainer.innerHTML = '<p style="color:#ef4444;">Cannot query database without a valid building ID.</p>';
        return;
    }

    // 4C. UPDATE ALKIS DOM (STATIC TILE DATA)
    populateAlkisTable(pickedFeature);

    // 4D. UPDATE QFIELD DOM (LIVE DATABASE QUERY)
    await fetchDatabaseRecord(gmlid);

}, Cesium.ScreenSpaceEventType.LEFT_CLICK);

// =====================================================================
// 5. HELPER FUNCTIONS
// =====================================================================

/**
 * Extracts raw metadata embedded directly inside the 3D Tile and populates the table.
 */
function populateAlkisTable(feature) {
    const propertyIds = feature.getPropertyIds();
    alkisTableBody.innerHTML = ''; // Clear previous data

    if (propertyIds.length === 0) {
        alkisTableBody.innerHTML = '<tr><td colspan="2">No local attributes available.</td></tr>';
        return;
    }

    // Generate table rows for each property
    propertyIds.forEach(propId => {
        const value = feature.getProperty(propId);
        const row = document.createElement('tr');
        row.innerHTML = `<th>${propId}</th><td>${value !== null ? value : '-'}</td>`;
        alkisTableBody.appendChild(row);
    });
}

/**
 * Queries the Node.js API to retrieve QField survey data and photos.
 */
async function fetchDatabaseRecord(gmlid) {
    // Show a loading state while waiting for the database
    qfieldDataContainer.innerHTML = '<p style="color: #fbbf24; text-align: center;">⏳ Querying PostGIS Database...</p>';

    try {
        // Pointing to your Node.js backend route
        const response = await fetch(`http://localhost:5000/api/buildings/${gmlid}`);
        
        if (!response.ok) {
            throw new Error(`Server returned status ${response.status}`);
        }

        const data = await response.json();
        
        // Build the HTML layout mapping directly to your Node.js SQL payload
        let html = `
            <div style="background: rgba(0,0,0,0.3); padding: 12px; border-radius: 6px; margin-bottom: 10px; border-left: 3px solid #38bdf8;">
                <p style="margin: 0 0 8px 0;"><strong>Condition:</strong> <span style="color: #4ade80;">${data.qfield_condition || 'N/A'}</span></p>
                <p style="margin: 0 0 8px 0;"><strong>Usage:</strong> ${data.alkis_usage || 'N/A'}</p>
                <p style="margin: 0 0 8px 0;"><strong>Year Built:</strong> ${data.alkis_year_built || 'N/A'}</p>
                <p style="margin: 0; padding-top: 8px; border-top: 1px solid #475569;"><strong>Field Notes:</strong><br/> ${data.photo_notes || 'No notes recorded.'}</p>
            </div>
        `;

        // Process attached survey photos if they exist
        if (data.file_path) {
            // Extracts just the filename from QField's path (e.g., DCIM/photos/image.jpg -> image.jpg)
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
                <div class="photo-container" style="text-align: center; padding: 20px 0;">
                    <span class="photo-label">Media</span>
                    <p style="color: #64748b; font-style: italic; font-size: 0.85rem; margin-top: 5px;">No photos attached to this survey.</p>
                </div>
            `;
        }

        qfieldDataContainer.innerHTML = html;

    } catch (error) {
        console.error("API Fetch Error:", error);
        qfieldDataContainer.innerHTML = `
            <div style="background: rgba(239, 68, 68, 0.1); border: 1px solid #ef4444; padding: 10px; border-radius: 4px;">
                <p style="color: #ef4444; margin: 0 0 5px 0;"><strong>Database connection failed.</strong></p>
                <p style="font-size: 0.8rem; color: #cbd5e1; margin: 0;">Ensure your Node.js backend (http://localhost:5000) is running and CORS is enabled.</p>
            </div>
        `;
    }
}