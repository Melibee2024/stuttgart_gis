(function(){let e=document.createElement(`link`).relList;if(e&&e.supports&&e.supports(`modulepreload`))return;for(let e of document.querySelectorAll(`link[rel="modulepreload"]`))n(e);new MutationObserver(e=>{for(let t of e)if(t.type===`childList`)for(let e of t.addedNodes)e.tagName===`LINK`&&e.rel===`modulepreload`&&n(e)}).observe(document,{childList:!0,subtree:!0});function t(e){let t={};return e.integrity&&(t.integrity=e.integrity),e.referrerPolicy&&(t.referrerPolicy=e.referrerPolicy),e.crossOrigin===`use-credentials`?t.credentials=`include`:e.crossOrigin===`anonymous`?t.credentials=`omit`:t.credentials=`same-origin`,t}function n(e){if(e.ep)return;e.ep=!0;let n=t(e);fetch(e.href,n)}})();var e=new Cesium.Viewer(`cesiumContainer`,{baseLayerPicker:!0,geocoder:!0,timeline:!1,animation:!1,baseLayer:new Cesium.ImageryLayer(new Cesium.OpenStreetMapImageryProvider({url:`https://tile.openstreetmap.org/`}))});e.cesiumWidget.screenSpaceEventHandler.removeInputAction(Cesium.ScreenSpaceEventType.LEFT_DOUBLE_CLICK);var t=`http://localhost:5000`,n=[["${ifc_class} === 'IfcWall'",`color('#94a3b8', 0.85)`],["${ifc_class} === 'IfcSlab'",`color('#78716c', 0.90)`],["${ifc_class} === 'IfcWindow'",`color('#7dd3fc', 0.55)`],["${ifc_class} === 'IfcDoor'",`color('#a78bfa', 0.85)`],["${ifc_class} === 'IfcColumn'",`color('#fbbf24', 0.90)`],["${ifc_class} === 'IfcRoof'",`color('#f87171', 0.85)`],["${ifc_class} === 'IfcSpace'",`color('#86efac', 0.25)`],["${ifc_class} === 'IfcStair'",`color('#fb923c', 0.85)`],[`true`,`color('#e2e8f0', 0.70)`]];function r(e=null,t=null){let r;return e&&t?r=`\${ifc_class} === '${e}' && \${storey} === '${t}'`:e?r=`\${ifc_class} === '${e}'`:t&&(r=`\${storey} === '${t}'`),new Cesium.Cesium3DTileStyle({color:{conditions:n},...r?{show:r}:{}})}var i=null;async function a(n=null,a=null){try{i&&=(e.scene.primitives.remove(i),null),i=await Cesium.Cesium3DTileset.fromUrl(`${t}/tiles/tileset.json`,{maximumScreenSpaceError:16}),i.style=r(n,a),e.scene.primitives.add(i),await e.zoomTo(i),console.log(`✅ IFC 3D Tileset loaded`)}catch(e){console.error(`❌ Failed to load IFC 3D Tileset:`,e)}}(async()=>{try{let t=await Cesium.Cesium3DTileset.fromIonAssetId(96188);e.scene.primitives.add(t),await u(),await a(),console.log(`✅ Nexus3D ready.`)}catch(e){console.error(`❌ Startup error:`,e)}})();var o=document.getElementById(`filterClass`),s=document.getElementById(`filterStorey`),c=document.getElementById(`applyFilter`),l=document.getElementById(`resetFilter`);async function u(){try{let e=await(await fetch(`${t}/api/buildings/filters`)).json();(e.ifc_classes??[]).forEach(e=>{let t=document.createElement(`option`);t.value=e,t.textContent=e,o.appendChild(t)}),(e.storeys??[]).forEach(e=>{let t=document.createElement(`option`);t.value=e,t.textContent=e,s.appendChild(t)})}catch(e){console.error(`❌ Failed to load filter options:`,e)}}c.addEventListener(`click`,()=>{let e=o.value||null,t=s.value||null;g(),i&&(i.style=r(e,t))}),l.addEventListener(`click`,()=>{o.value=``,s.value=``,g(),i&&(i.style=r())});var d=document.getElementById(`right-sidebar`),f=document.getElementById(`closeSidebar`),p=document.getElementById(`alkisTableBody`),m=document.getElementById(`qfieldDataContainer`);f.addEventListener(`click`,()=>{d.classList.remove(`active`),g()});var h={feature:null};function g(){h.feature&&=(h.feature.color=Cesium.Color.WHITE,null)}e.screenSpaceEventHandler.setInputAction(async function(t){let n=e.scene.pick(t.position);if(!Cesium.defined(n)||!(n instanceof Cesium.Cesium3DTileFeature)){g(),d.classList.remove(`active`);return}if(h.feature===n)return;g(),h.feature=n,n.color=Cesium.Color.fromCssColorString(`#38bdf8`).withAlpha(.9);let r=n.getProperty(`global_id`);if(d.classList.add(`active`),!r){p.innerHTML=`<tr><td colspan="2" style="color:#ef4444;">No global_id in batch table</td></tr>`,m.innerHTML=`<p style="color:#ef4444;">Cannot query database without a valid global_id.</p>`;return}_(n),await v(r)},Cesium.ScreenSpaceEventType.LEFT_CLICK);function _(e){p.innerHTML=``;let t={"IFC Class":e.getProperty(`ifc_class`)??`—`,Name:e.getProperty(`name`)??`—`,Storey:e.getProperty(`storey`)??`—`,Height:e.getProperty(`element_height_m`)==null?`—`:e.getProperty(`element_height_m`)+` m`,"Z Base":e.getProperty(`z_min_ellipsoidal`)==null?`—`:e.getProperty(`z_min_ellipsoidal`)+` m`,"Z Top":e.getProperty(`z_max_ellipsoidal`)==null?`—`:e.getProperty(`z_max_ellipsoidal`)+` m`,"Global ID":e.getProperty(`global_id`)??`—`};Object.entries(t).forEach(([e,t])=>{let n=document.createElement(`tr`);n.innerHTML=`<th>${e}</th><td>${t}</td>`,p.appendChild(n)})}async function v(e){m.innerHTML=`<p style="color:#fbbf24;text-align:center;">⏳ Querying PostGIS…</p>`;try{let n=await fetch(`${t}/api/buildings/${e}`);if(!n.ok)throw Error(`HTTP ${n.status}`);let r=await n.json(),i=`
            <div style="background:rgba(0,0,0,.3);padding:12px;border-radius:6px;
                        margin-bottom:10px;border-left:3px solid #38bdf8;">
                <p style="margin:0 0 8px;"><strong>Condition:</strong>
                   <span style="color:#4ade80;">${r.qfield_condition??`N/A`}</span></p>
                <p style="margin:0 0 8px;"><strong>Usage:</strong> ${r.alkis_usage??`N/A`}</p>
                <p style="margin:0 0 8px;"><strong>Year Built:</strong> ${r.alkis_year_built??`N/A`}</p>
                <p style="margin:0;padding-top:8px;border-top:1px solid #475569;">
                   <strong>Last Modified:</strong> ${r.last_modified??`N/A`}</p>
            </div>
        `;r.ifc_attributes&&Object.keys(r.ifc_attributes).length>0&&(i+=`
                <div style="background:rgba(0,0,0,.2);padding:10px;border-radius:6px;margin-bottom:10px;">
                    <p style="margin:0 0 8px;font-weight:500;">IFC Attributes</p>
                    <table style="width:100%;font-size:.82rem;border-collapse:collapse;">
            `,Object.entries(r.ifc_attributes).forEach(([e,t])=>{i+=`<tr>
                    <th style="text-align:left;padding:2px 8px 2px 0;color:#94a3b8;
                               font-weight:400;white-space:nowrap;">${e}</th>
                    <td style="padding:2px 0;color:#e2e8f0;">${t??`—`}</td>
                </tr>`}),i+=`</table></div>`),r.pset_properties?.length>0&&(i+=`
                <div style="background:rgba(0,0,0,.2);padding:10px;border-radius:6px;margin-bottom:10px;">
                    <p style="margin:0 0 8px;font-weight:500;">Property Sets</p>
                    <table style="width:100%;font-size:.82rem;border-collapse:collapse;">
            `,r.pset_properties.forEach(e=>{let t=e.val_string??e.val_double??e.val_int??`—`;i+=`<tr>
                    <th style="text-align:left;padding:2px 8px 2px 0;color:#94a3b8;
                               font-weight:400;white-space:nowrap;">${e.name}</th>
                    <td style="padding:2px 0;color:#e2e8f0;">${t}</td>
                </tr>`}),i+=`</table></div>`),r.field_photos?.length>0?(i+=`<div style="margin-top:4px;"><p style="margin:0 0 8px;font-weight:500;">Field Photographs</p>`,r.field_photos.forEach(e=>{let n=`${t}/media/${e.file_path?.split(`/`).pop()}`,r=e.photo_name||e.direction||`Survey photo`;i+=`
                    <div class="photo-container" style="margin-bottom:12px;">
                        <span class="photo-label">${r}</span>
                        ${e.notes?`<p style="font-size:.8rem;color:#94a3b8;margin:4px 0;">${e.notes}</p>`:``}
                        <img src="${n}" alt="${r}" style="width:100%;border-radius:4px;"
                             onerror="this.style.display='none'">
                        ${e.captured_at?`<p style="font-size:.75rem;color:#64748b;margin:4px 0 0;">${e.captured_at}</p>`:``}
                    </div>
                `}),i+=`</div>`):i+=`
                <div class="photo-container" style="text-align:center;padding:20px 0;">
                    <span class="photo-label">Media</span>
                    <p style="color:#64748b;font-style:italic;font-size:.85rem;margin-top:5px;">
                        No photos recorded for this element.
                    </p>
                </div>
            `,m.innerHTML=i}catch(e){console.error(`API Fetch Error:`,e),m.innerHTML=`
            <div style="background:rgba(239,68,68,.1);border:1px solid #ef4444;
                        padding:10px;border-radius:4px;">
                <p style="color:#ef4444;margin:0 0 5px;"><strong>Database connection failed.</strong></p>
                <p style="font-size:.8rem;color:#cbd5e1;margin:0;">
                    Ensure the Node.js backend (${t}) is running.
                </p>
            </div>
        `}}