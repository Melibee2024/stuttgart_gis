import * as Cesium from 'cesium';
import "cesium/Build/Cesium/Widgets/widgets.css";

// Configure asset base URL for Vite
window.CESIUM_BASE_URL = '/node_modules/cesium/Build/Cesium/';

// Ensure you replace this string with your actual copied token from Cesium Ion!
Cesium.Ion.defaultAccessToken = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJqdGkiOiI4YjZmMzNlYS1jMzMxLTQ0OGYtYTg4Yy0yNmRhZGFlZGNmMmEiLCJpZCI6NDEwNTE0LCJzdWIiOiJhYmhpcmFtZGV2aWRhc2FuIiwiaXNzIjoiaHR0cHM6Ly9pb24uY2VzaXVtLmNvbSIsImF1ZCI6IlVudGl0bGVkIiwiaWF0IjoxNzc5MjE2NzQ1fQ.SKbpCnZOz2x7ioINqC6KrhqUaSFS_RaH7zy18nZs8sQ';

// 1. Initialize the 3D Viewer using the modern terrain creation syntax
const viewer = new Cesium.Viewer('app', {
    terrainProvider: await Cesium.createWorldTerrainAsync(),
    animation: false,     // Hides the clock widget
    timeline: false       // Hides the timeline widget
});

// 2. Add placeholder OpenStreetMap 3D Buildings for Stuttgart (Modern Syntax)
// Asset ID 96188 is the global OpenStreetMap Buildings asset on Cesium Ion
const buildings = await Cesium.Cesium3DTileset.fromIonAssetId(96188);
viewer.scene.primitives.add(buildings);

// 3. Fly the camera straight to the HFT Stuttgart Campus area
viewer.camera.flyTo({
    destination: Cesium.Cartesian3.fromDegrees(9.1727, 48.7801, 600), // Longitude, Latitude, Height in meters
    orientation: {
        heading: Cesium.Math.toRadians(0.0),
        pitch: Cesium.Math.toRadians(-45.0), // Tilt looking down
        roll: 0.0
    }
});
// ==========================================
// 4. CLICK INTERACTION & LIVE DATABASE ROUTING
// ==========================================

const handler = new Cesium.ScreenSpaceEventHandler(viewer.scene.canvas);

handler.setInputAction(async function (click) {
    const pickedFeature = viewer.scene.pick(click.position);
    
    if (Cesium.defined(pickedFeature)) {
        // 1. Extract the actual elementId parameter shown in your Cesium table data
        const buildingGmlId = pickedFeature.getProperty('elementId'); 
        
        console.log("Interactive Twin -> Requesting Key ID:", buildingGmlId);
        
        if (buildingGmlId) {
            try {
                // 2. Query your live Node.js database bridge running on port 5000
                const response = await fetch(`http://localhost:5000/api/buildings/${encodeURIComponent(buildingGmlId)}`);
                
                if (!response.ok) {
                    throw new Error(`Database server responded with status: ${response.status}`);
                }
                
                const dbRecord = await response.json();
                
                // 3. Inject the live PostGIS / ALKIS / QField rows directly into the UI panel
                viewer.selectedEntity = new Cesium.Entity({
                    name: dbRecord.name || `Building ID: ${dbRecord.gmlid}`,
                    description: `
                          <div style="font-family: sans-serif; padding: 5px; max-width: 300px;">
                              <h4 style="margin-top: 0; color: #44aaFF;">Live PostGIS Attribute Fusion</h4>
                              <table class="cesium-infoBox-defaultTable">
                                  <tbody>
                                      <tr><th>GMLID Key</th><td><b>${dbRecord.gmlid}</b></td></tr>
                                      <tr><th>ALKIS Usage</th><td>${dbRecord.alkis_usage}</td></tr>
                                      <tr><th>Year Built</th><td>${dbRecord.alkis_year_built}</td></tr>
                                      <tr style="background: rgba(50,200,50,0.1); font-weight: bold;">
                                          <th>QField Status</th><td>${dbRecord.qfield_condition}</td></tr>
                                  </tbody>
                              </table>
                              
                              <div style="margin-top: 12px; border-top: 1px solid #555; padding-top: 10px;">
                                  <span style="font-size: 10px; color: #44aaFF; display: block; margin-bottom: 4px; font-weight: bold; letter-spacing: 0.5px;">
                                      📸 FIELD SURVEY ATTACHMENT
                                  </span>
                                  <img src="http://localhost:5000/media/photos/bau2_library.jpg" 
                                      alt="Field Photo" 
                                      style="width: 100%; height: auto; border-radius: 4px; border: 1px solid #666; display: block; margin-bottom: 4px;" 
                                      onerror="this.parentElement.style.display='none';" />
                              </div>
                          </div>
                      `
                });
            } catch (error) {
                console.warn("Could not match or retrieve live row records:", error);
                
                // Keep the custom placeholder active so it overrides the default grey table layout
                viewer.selectedEntity = new Cesium.Entity({
                    name: `Selected Element: Bau 2`,
                    description: `
                        <div style="font-family: sans-serif; padding: 5px;">
                            <p><b>Mapped Identifier ID:</b> <code style="background: #333; padding: 2px 4px; border-radius: 3px; color: #fff;">${buildingGmlId}</code></p>
                            <hr style="border: 0; border-top: 1px solid #444; margin: 10px 0;">
                            <p style="color: #ffaa00; font-size: 13px; margin-bottom: 0;">
                                ⚠️ Connecting loop active. Insert a row matching key <b>'${buildingGmlId}'</b> into your PostgreSQL view to display full asset telemetry.
                            </p>
                        </div>
                    `
                });
            }
        }
    }
}, Cesium.ScreenSpaceEventType.LEFT_CLICK);