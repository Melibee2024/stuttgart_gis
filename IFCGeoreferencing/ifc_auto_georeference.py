"""
ifc_auto_georeference.py
========================
Nexus3D Stuttgart — Stage 0: IFC Georeferencer
===============================================
Automatically georeferences an IFC file to EPSG:25832 (ETRS89 / UTM 32N).

Geocoding pipeline (in priority order):
  1. VERIFIED coords  — hardcoded per-building, sub-metre accuracy (preferred)
  2. ALKIS WFS snap   — LGL Baden-Württemberg cadastral footprint centroid
  3. Nominatim        — OSM geocoding, rough fallback (~50–100 m)

IFC processing:
  4. Inspects unit scale, local site offset, legacy lat/lon
  5. Injects IfcMapConversion + IfcProjectedCRS (EPSG:25832)
  6. Clears legacy RefLatitude/RefLongitude
  7. Saves *_georef.ifc and prints a verification summary + OSM link

Verification:
  Stage D — re-opens saved file, validates every georeferencing field
  Stage E — Folium HTML map (visual check) + ALKIS-based rotation calculator

Requirements:
    pip install ifcopenshell pyproj requests folium

No API keys needed.

FIXES vs previous version:
  - VERIFIED_NORTHING corrected to 5403041.8 (was 5403013.8 — 28 m too far south)
  - inject_georeferencing(): Scale: 1.0 was accidentally commented out — fixed
  - verify_output(): XAxisAbscissa/Ordinate now checked against actual injected
    values (-0.442097 / 0.896967), not wrong defaults (1.0 / 0.0)
"""

import os
import re
import sys
import math
import time
import requests
import ifcopenshell
import ifcopenshell.api
import ifcopenshell.util.placement
from pyproj import Transformer


# ══════════════════════════════════════════════════════════════════════════════
#  CONFIG — edit this section only
# ══════════════════════════════════════════════════════════════════════════════

IFC_PATH = r"C:\Users\abhir\PycharmProjects\stuttgart_gis\IFCGeoreferencing\HFT_Bau4_2025.04.22.ifc"

BUILDING_ADDRESS = "Hochschule für Technik Stuttgart, Schellingstraße 24, Stuttgart"

TARGET_HEIGHT_M = 245.0         # Ground elevation, DHHN2016 datum (Stuttgart avg)

ALKIS_SEARCH_RADIUS_M = 300     # ALKIS WFS search radius around rough geocode point

# ── Verified building coords (EPSG:25832) ─────────────────────────────────────
# FIX: Northing corrected from 5403013.8 → 5403041.8
# Previous value placed the building ~28 m too far south in PostGIS/QGIS.
# Derived by comparing DB bounding box vs ALKIS footprint ring and averaging
# the south-edge (34 m) and north-edge (22 m) offsets → 28 m correction.
VERIFIED_EASTING  = 512614.7    # EPSG:25832 easting  — from ALKIS (unchanged)
VERIFIED_NORTHING = 5403041.8   # EPSG:25832 northing — CORRECTED (+28 m)

# ── Rotation values for Bau 4 ─────────────────────────────────────────────────
# Long-axis bearing: 116.24° from East (NW–SE along Schellingstraße)
# These are injected into IfcMapConversion.XAxisAbscissa / XAxisOrdinate.
# Do NOT change unless re-deriving from ALKIS ring geometry.
XAXIS_ABSCISSA = -0.442097   # cos(116.24°)
XAXIS_ORDINATE =  0.896967   # sin(116.24°)

# ── ALKIS WFS ─────────────────────────────────────────────────────────────────
ALKIS_WFS_ENDPOINT = "https://owsproxy.lgl-bw.de/owsproxy/wfs/WFS_LGL-BW_ALKIS"

ALKIS_LAYER_NAMES = [
    "nora:AX_Gebaeude",
    "AX_Gebaeude",
    "nora:ax_gebaeude",
]

# ── Hardcoded ALKIS footprint for Bau 4 (EPSG:25832) ─────────────────────────
BAU4_ALKIS_RING = [
    [512627.75,  5402997.15],
    [512605.60,  5403042.09],
    [512603.55,  5403046.25],
    [512592.55,  5403040.84],
    [512592.18,  5403040.66],
    [512596.49,  5403031.88],
    [512597.85,  5403029.13],
    [512600.23,  5403024.29],
    [512616.36,  5402991.54],
    [512627.75,  5402997.15],  # closing vertex
]

# ── Folium output path ────────────────────────────────────────────────────────
FOLIUM_OUTPUT = r"C:\Users\abhir\PycharmProjects\stuttgart_gis\IFCGeoreferencing\hft_bau4_verify.html"

# ── Known buildings fallback (last-resort Nominatim) ─────────────────────────
KNOWN_BUILDINGS_WGS84 = {
    "hft":              (48.780274, 9.172525),
    "bau4":             (48.780274, 9.172525),
    "schellingstrasse": (48.780274, 9.172525),
}


# ══════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _sep(char="─", width=62):
    print(char * width)


def _header(title: str):
    _sep("═")
    print(f"  {title}")
    _sep("═")


def dms_to_dd(dms_tuple) -> float:
    d, m, s, ms = dms_tuple
    dd = abs(d) + m / 60 + s / 3600 + ms / 3_600_000_000
    return -dd if d < 0 else dd


def utm32n_to_wgs84(easting: float, northing: float) -> tuple:
    t = Transformer.from_crs("EPSG:25832", "EPSG:4326", always_xy=True)
    lon, lat = t.transform(easting, northing)
    return lat, lon


def wgs84_to_utm32n(lat: float, lon: float) -> tuple:
    t = Transformer.from_crs("EPSG:4326", "EPSG:25832", always_xy=True)
    easting, northing = t.transform(lon, lat)
    return easting, northing


def osm_link(lat: float, lon: float, zoom: int = 19) -> str:
    return f"https://www.openstreetmap.org/#map={zoom}/{lat:.6f}/{lon:.6f}"


# ══════════════════════════════════════════════════════════════════════════════
#  STAGE A — GEOCODING
# ══════════════════════════════════════════════════════════════════════════════

def geocode(building_address: str) -> tuple:
    # Tier 1 — verified hardcoded coords
    if VERIFIED_EASTING is not None and VERIFIED_NORTHING is not None:
        lat, lon = utm32n_to_wgs84(VERIFIED_EASTING, VERIFIED_NORTHING)
        print("\n[Geocode] Tier 1 — Using verified hardcoded coords")
        print(f"          E={VERIFIED_EASTING:.3f}, N={VERIFIED_NORTHING:.3f}")
        print(f"          WGS84: {lat:.6f}°N, {lon:.6f}°E")
        print(f"          OSM:   {osm_link(lat, lon)}")
        return VERIFIED_EASTING, VERIFIED_NORTHING

    # Tier 2 — ALKIS WFS
    print("\n[Geocode] Tier 1 skipped (VERIFIED coords not set)")
    print("[Geocode] Tier 2 — Attempting ALKIS WFS snap...")
    rough_e, rough_n = _nominatim_to_utm(building_address)
    if rough_e is not None:
        centroid = _alkis_snap(rough_e, rough_n)
        if centroid:
            e, n = centroid
            lat, lon = utm32n_to_wgs84(e, n)
            print(f"          ✅ ALKIS centroid: E={e:.3f}, N={n:.3f}")
            print(f"          WGS84: {lat:.6f}°N, {lon:.6f}°E")
            return e, n

    # Tier 3 — Nominatim rough
    print("[Geocode] Tier 2 failed — falling back to Nominatim (rough)")
    if rough_e is not None:
        lat, lon = utm32n_to_wgs84(rough_e, rough_n)
        print(f"          ⚠ Using rough Nominatim: E={rough_e:.3f}, N={rough_n:.3f}")
        print("          ⚠ Accuracy ~50–100 m. Verify the OSM pin visually.")
        return rough_e, rough_n

    # Last resort — KNOWN_BUILDINGS
    addr_lower = building_address.lower()
    for keyword, (lat, lon) in KNOWN_BUILDINGS_WGS84.items():
        if keyword in addr_lower:
            e, n = wgs84_to_utm32n(lat, lon)
            print(f"          ⚠ Using KNOWN_BUILDINGS entry for '{keyword}'")
            return e, n

    raise ValueError(
        f"All geocoding tiers failed for: '{building_address}'\n"
        f"Fix: Set VERIFIED_EASTING / VERIFIED_NORTHING in the CONFIG section."
    )


def _nominatim_to_utm(address: str):
    queries = _build_nominatim_cascade(address)
    headers = {"User-Agent": "Nexus3D-Stuttgart-Georef/1.0"}
    for query in queries:
        try:
            resp = requests.get(
                "https://nominatim.openstreetmap.org/search",
                params={"q": query, "format": "json", "limit": 1},
                headers=headers,
                timeout=10,
            )
            resp.raise_for_status()
            results = resp.json()
            if results:
                lat = float(results[0]["lat"])
                lon = float(results[0]["lon"])
                e, n = wgs84_to_utm32n(lat, lon)
                print(f"          Nominatim hit on: '{query}'")
                return e, n
        except Exception:
            pass
        time.sleep(1)
    return None, None


def _build_nominatim_cascade(address: str) -> list:
    parts = [p.strip() for p in address.split(",")]
    queries = [address]
    if len(parts) > 1:
        queries.append(", ".join(parts[1:]))
    if len(parts) >= 2:
        queries.append(f"{parts[0]}, {parts[-1]}")
    if len(parts) >= 2:
        queries.append(parts[1] if len(parts) > 2 else parts[0])
    ascii_addr = (address
                  .replace("ü", "ue").replace("ö", "oe").replace("ä", "ae")
                  .replace("ß", "ss").replace("Ü", "Ue").replace("Ö", "Oe")
                  .replace("Ä", "Ae"))
    if ascii_addr != address:
        queries.append(ascii_addr)
    seen, out = set(), []
    for q in queries:
        if q not in seen:
            seen.add(q)
            out.append(q)
    return out


def _alkis_snap(rough_e: float, rough_n: float):
    bbox = (
        rough_e - ALKIS_SEARCH_RADIUS_M, rough_n - ALKIS_SEARCH_RADIUS_M,
        rough_e + ALKIS_SEARCH_RADIUS_M, rough_n + ALKIS_SEARCH_RADIUS_M,
    )
    for layer in ALKIS_LAYER_NAMES:
        features = _wfs_getfeature(ALKIS_WFS_ENDPOINT, layer, bbox)
        if features:
            centroid, dist = _nearest_centroid(features, rough_e, rough_n)
            if centroid:
                print(f"          ALKIS layer '{layer}': {len(features)} features, "
                      f"snap dist={dist:.1f} m")
                return centroid
    print("          Known layer names failed — trying GetCapabilities discovery...")
    discovered = _getcapabilities_layers(ALKIS_WFS_ENDPOINT)
    for layer in discovered:
        features = _wfs_getfeature(ALKIS_WFS_ENDPOINT, layer, bbox)
        if features:
            centroid, dist = _nearest_centroid(features, rough_e, rough_n)
            if centroid:
                return centroid
    return None


def _wfs_getfeature(endpoint: str, layer: str, bbox: tuple):
    params = {
        "SERVICE": "WFS", "VERSION": "2.0.0", "REQUEST": "GetFeature",
        "TYPENAMES": layer, "SRSNAME": "EPSG:25832",
        "BBOX": f"{bbox[0]:.3f},{bbox[1]:.3f},{bbox[2]:.3f},{bbox[3]:.3f},EPSG:25832",
        "OUTPUTFORMAT": "application/json", "COUNT": "50",
    }
    try:
        resp = requests.get(endpoint, params=params, timeout=15)
        if resp.status_code != 200:
            snippet = resp.text[:200].replace("\n", " ").strip()
            print(f"          HTTP {resp.status_code} for layer '{layer}': {snippet}")
            return []
        ct = resp.headers.get("Content-Type", "")
        if "json" not in ct:
            snippet = " ".join(re.sub(r"<[^>]+>", " ", resp.text).split())[:200]
            print(f"          Non-JSON response for layer '{layer}': {snippet}")
            return []
        return resp.json().get("features", [])
    except requests.exceptions.ConnectionError as e:
        print(f"          Connection error: {e}")
        return []
    except Exception as e:
        print(f"          Unexpected error for layer '{layer}': {e}")
        return []


def _getcapabilities_layers(endpoint: str) -> list:
    try:
        resp = requests.get(
            endpoint,
            params={"SERVICE": "WFS", "REQUEST": "GetCapabilities"},
            timeout=10,
        )
        resp.raise_for_status()
        matches = re.findall(
            r"<(?:wfs:)?Name>([^<]*(?i:gebaeude)[^<]*)</(?:wfs:)?Name>", resp.text
        )
        if matches:
            print(f"          GetCapabilities found: {matches}")
        return matches
    except Exception as e:
        print(f"          GetCapabilities failed: {e}")
        return []


def _nearest_centroid(features: list, ref_e: float, ref_n: float) -> tuple:
    best, best_dist = None, float("inf")
    for feat in features:
        geom   = feat.get("geometry") or {}
        gtype  = geom.get("type", "")
        coords = geom.get("coordinates", [])
        if gtype == "Polygon" and coords:
            ring = coords[0]
        elif gtype == "MultiPolygon" and coords:
            ring = coords[0][0]
        else:
            continue
        if len(ring) < 3:
            continue
        cx = sum(p[0] for p in ring) / len(ring)
        cy = sum(p[1] for p in ring) / len(ring)
        dist = math.hypot(cx - ref_e, cy - ref_n)
        if dist < best_dist:
            best_dist = dist
            best = (cx, cy)
    return best, best_dist


# ══════════════════════════════════════════════════════════════════════════════
#  STAGE B — IFC INSPECTION
# ══════════════════════════════════════════════════════════════════════════════

def inspect_ifc(ifc_file) -> dict:
    info = {
        "scale": 1.0,
        "offset_x": 0.0, "offset_y": 0.0, "offset_z": 0.0,
        "legacy_lat": None, "legacy_lon": None,
        "has_map_conversion": False,
    }

    for unit in ifc_file.by_type("IfcSIUnit"):
        if unit.UnitType == "LENGTHUNIT":
            if getattr(unit, "Prefix", None) == "MILLI":
                info["scale"] = 0.001
                print("  Length unit:     MILLIMETRES (scale 0.001 applied)")
            else:
                print("  Length unit:     METRES ✓")
            break

    if ifc_file.by_type("IfcMapConversion"):
        info["has_map_conversion"] = True
        print("  MapConversion:   EXISTS — will be overwritten")
    else:
        print("  MapConversion:   none")

    sites = ifc_file.by_type("IfcSite")
    if sites:
        mat = ifcopenshell.util.placement.get_local_placement(sites[0].ObjectPlacement)
        info["offset_x"] = float(mat[0, 3])
        info["offset_y"] = float(mat[1, 3])
        info["offset_z"] = float(mat[2, 3])
        print(f"  Site offset:     X={info['offset_x']:.4f}, "
              f"Y={info['offset_y']:.4f}, Z={info['offset_z']:.4f}")
        site = sites[0]
        if site.RefLatitude:
            info["legacy_lat"] = dms_to_dd(site.RefLatitude)
        if site.RefLongitude:
            info["legacy_lon"] = dms_to_dd(site.RefLongitude)
        if info["legacy_lat"] is not None:
            in_bw = 47.5 < info["legacy_lat"] < 49.5
            flag  = "✓" if in_bw else "⚠ NOT Stuttgart — will be cleared"
            print(f"  Legacy lat/lon:  {info['legacy_lat']:.6f}°, "
                  f"{info['legacy_lon']:.6f}°  {flag}")
        else:
            print("  Legacy lat/lon:  none")
    else:
        print("  IfcSite:         NOT FOUND — offset assumed (0, 0, 0)")

    return info


# ══════════════════════════════════════════════════════════════════════════════
#  STAGE C — INJECT GEOREFERENCING
# ══════════════════════════════════════════════════════════════════════════════

def inject_georeferencing(ifc_file, easting: float, northing: float,
                          height: float, info: dict) -> tuple:
    """
    Write IfcMapConversion + IfcProjectedCRS into the IFC file.

    FIX: Scale: 1.0 was previously commented out by accident (it was on the
    same line as the XAxisOrdinate comment). Now on its own line.

    XAxisAbscissa / XAxisOrdinate are read from CONFIG constants so they are
    visible and easy to update without touching this function.
    """
    ce, cn, ch = easting, northing, height

    print(f"  Eastings:         {ce:.3f}")
    print(f"  Northings:        {cn:.3f}")
    print(f"  Height:           {ch:.3f}")
    print(f"  XAxisAbscissa:    {XAXIS_ABSCISSA}  (cos 116.24°)")
    print(f"  XAxisOrdinate:    {XAXIS_ORDINATE}  (sin 116.24°)")
    print(f"  Scale:            1.0")
    print(f"  Site offset (info): X={info['offset_x']:.4f}, Y={info['offset_y']:.4f}")

    si_units    = ifc_file.by_type("IfcSIUnit")
    length_unit = next(
        (u for u in si_units if u.UnitType == "LENGTHUNIT"),
        si_units[0] if si_units else None,
    )
    if length_unit is None:
        raise RuntimeError("No IfcSIUnit found in IFC — cannot set MapUnit.")

    if info["has_map_conversion"]:
        for mc in ifc_file.by_type("IfcMapConversion"):
            ifc_file.remove(mc)
        for crs in ifc_file.by_type("IfcProjectedCRS"):
            ifc_file.remove(crs)
        print("  Removed stale IfcMapConversion + IfcProjectedCRS")

    ifcopenshell.api.run("georeference.add_georeferencing", ifc_file)
    ifcopenshell.api.run(
        "georeference.edit_georeferencing",
        ifc_file,
        coordinate_operation={
            "Eastings":         ce,
            "Northings":        cn,
            "OrthogonalHeight": ch,
            "XAxisAbscissa":    XAXIS_ABSCISSA,
            "XAxisOrdinate":    XAXIS_ORDINATE,
            "Scale":            1.0,            # FIX: was accidentally on comment line
        },
        projected_crs={
            "Name":          "EPSG:25832",
            "Description":   "ETRS89 / UTM zone 32N",
            "GeodeticDatum": "ETRS89",
            "MapProjection": "UTM",
            "MapZone":       "32N",
            "MapUnit":       length_unit,
        },
    )

    sites = ifc_file.by_type("IfcSite")
    if sites:
        site = sites[0]
        site.RefLatitude  = None
        site.RefLongitude = None
        site.RefElevation = None
        print("  Legacy RefLatitude / RefLongitude / RefElevation → cleared ✓")

    return ce, cn, ch


# ══════════════════════════════════════════════════════════════════════════════
#  STAGE D — FIELD VERIFICATION (re-reads the saved file)
# ══════════════════════════════════════════════════════════════════════════════

def verify_output(out_path: str, expected_e: float, expected_n: float):
    """
    FIX: XAxisAbscissa / XAxisOrdinate are now checked against the actual
    injected values (XAXIS_ABSCISSA / XAXIS_ORDINATE from CONFIG), not the
    wrong defaults 1.0 / 0.0 that caused false ❌ in Stage D.
    """
    ifc = ifcopenshell.open(out_path)
    ok  = True

    def check(label, got, expect, tol=1.0):
        nonlocal ok
        if expect is None:
            passed = got is None
        elif isinstance(expect, float):
            passed = abs(got - expect) <= tol
        else:
            passed = got == expect
        mark = "✓" if passed else "❌"
        print(f"  {mark}  {label}: {got}  (expect ~{expect})")
        if not passed:
            ok = False

    mc_list  = ifc.by_type("IfcMapConversion")
    crs_list = ifc.by_type("IfcProjectedCRS")

    if not mc_list:
        print("  ❌  IfcMapConversion: NOT FOUND — injection failed")
        return
    if not crs_list:
        print("  ❌  IfcProjectedCRS: NOT FOUND — injection failed")
        return

    mc  = mc_list[0]
    crs = crs_list[0]

    check("Eastings",         mc.Eastings,        expected_e,      tol=1.0)
    check("Northings",        mc.Northings,        expected_n,      tol=1.0)
    check("OrthogonalHeight", mc.OrthogonalHeight, TARGET_HEIGHT_M, tol=0.1)
    check("XAxisAbscissa",    mc.XAxisAbscissa,    XAXIS_ABSCISSA,  tol=1e-4)
    check("XAxisOrdinate",    mc.XAxisOrdinate,    XAXIS_ORDINATE,  tol=1e-4)
    check("Scale",            mc.Scale,            1.0,             tol=1e-6)
    check("CRS Name",         crs.Name,            "EPSG:25832")

    sites = ifc.by_type("IfcSite")
    if sites:
        site = sites[0]
        check("RefLatitude cleared",  site.RefLatitude,  None)
        check("RefLongitude cleared", site.RefLongitude, None)

    lat, lon = utm32n_to_wgs84(mc.Eastings, mc.Northings)
    print(f"\n  WGS84:  {lat:.6f}°N, {lon:.6f}°E")
    print(f"  OSM:    {osm_link(lat, lon)}")
    print(f"\n  {'All checks passed ✅' if ok else 'Some checks FAILED ❌ — review above'}")


# ══════════════════════════════════════════════════════════════════════════════
#  STAGE E — VISUAL VERIFICATION + ROTATION CALCULATOR
# ══════════════════════════════════════════════════════════════════════════════

def _get_alkis_ring(ref_e: float, ref_n: float):
    if BAU4_ALKIS_RING is not None:
        print("  ALKIS footprint: using hardcoded ring from CONFIG "
              f"({len(BAU4_ALKIS_RING)} vertices)")
        return BAU4_ALKIS_RING

    print("  ALKIS footprint: attempting live WFS fetch...")
    bbox = (
        ref_e - ALKIS_SEARCH_RADIUS_M, ref_n - ALKIS_SEARCH_RADIUS_M,
        ref_e + ALKIS_SEARCH_RADIUS_M, ref_n + ALKIS_SEARCH_RADIUS_M,
    )
    for layer in ALKIS_LAYER_NAMES:
        features = _wfs_getfeature(ALKIS_WFS_ENDPOINT, layer, bbox)
        if not features:
            continue
        best_ring, best_dist = None, float("inf")
        for feat in features:
            geom   = feat.get("geometry") or {}
            gtype  = geom.get("type", "")
            coords = geom.get("coordinates", [])
            if gtype == "Polygon" and coords:
                ring = coords[0]
            elif gtype == "MultiPolygon" and coords:
                ring = coords[0][0]
            else:
                continue
            if len(ring) < 3:
                continue
            cx = sum(p[0] for p in ring) / len(ring)
            cy = sum(p[1] for p in ring) / len(ring)
            dist = math.hypot(cx - ref_e, cy - ref_n)
            if dist < best_dist:
                best_dist = dist
                best_ring = ring
        if best_ring:
            return best_ring

    print("  ALKIS footprint: not available — set BAU4_ALKIS_RING in CONFIG.")
    return None


def _long_axis_bearing(ring: list) -> float:
    longest_len = -1.0
    longest_dx  = 1.0
    longest_dy  = 0.0
    for i in range(len(ring) - 1):
        dx     = ring[i + 1][0] - ring[i][0]
        dy     = ring[i + 1][1] - ring[i][1]
        length = math.hypot(dx, dy)
        if length > longest_len:
            longest_len = length
            longest_dx  = dx
            longest_dy  = dy
    return math.degrees(math.atan2(longest_dy, longest_dx))


def _arrow_endpoint(lat: float, lon: float, bearing_deg: float,
                    length_m: float = 40.0) -> tuple:
    t  = Transformer.from_crs("EPSG:4326", "EPSG:25832", always_xy=True)
    e, n = t.transform(lon, lat)
    e2 = e + length_m * math.cos(math.radians(bearing_deg))
    n2 = n + length_m * math.sin(math.radians(bearing_deg))
    t2 = Transformer.from_crs("EPSG:25832", "EPSG:4326", always_xy=True)
    lon2, lat2 = t2.transform(e2, n2)
    return lat2, lon2


def generate_folium_map(ifc_e: float, ifc_n: float, out_path: str):
    try:
        import folium
    except ImportError:
        print("\n  ⚠ folium not installed — skipping map generation.")
        print("    Install with:  pip install folium")
        return

    ifc_lat, ifc_lon = utm32n_to_wgs84(ifc_e, ifc_n)
    m = folium.Map(location=[ifc_lat, ifc_lon], zoom_start=19, tiles="OpenStreetMap")

    folium.Circle(
        location=[ifc_lat, ifc_lon], radius=10,
        color="#27ae60", weight=2, fill=False,
        tooltip="10 m accuracy ring",
    ).add_to(m)

    folium.Marker(
        location=[ifc_lat, ifc_lon],
        popup=(
            f"<b>IFC Origin (IfcMapConversion)</b><br>"
            f"E = {ifc_e:.3f}<br>"
            f"N = {ifc_n:.3f}<br>"
            f"WGS84: {ifc_lat:.6f}°N, {ifc_lon:.6f}°E"
        ),
        tooltip="IFC origin",
        icon=folium.Icon(color="red", icon="home"),
    ).add_to(m)

    ring = _get_alkis_ring(ifc_e, ifc_n)
    bearing_deg = None

    if ring:
        t = Transformer.from_crs("EPSG:25832", "EPSG:4326", always_xy=True)
        ring_wgs84 = [[*reversed(t.transform(p[0], p[1]))] for p in ring]

        folium.Polygon(
            locations=ring_wgs84,
            color="#2980b9", weight=2.5,
            fill=True, fill_color="#2980b9", fill_opacity=0.15,
            tooltip="ALKIS footprint (Bau 4)",
            popup="<b>ALKIS AX_Gebaeude</b><br>DEBWL52210005DwE<br>Verwaltungsgebäude",
        ).add_to(m)

        bearing_deg = _long_axis_bearing(ring)
        xaxis_a = math.cos(math.radians(bearing_deg))
        xaxis_o = math.sin(math.radians(bearing_deg))

        print(f"\n  ── Rotation Analysis ──────────────────────────────────")
        print(f"  Longest edge bearing:  {bearing_deg:.2f}°  (from East, CCW)")
        print(f"  XAxisAbscissa:         {xaxis_a:.6f}   (cos {bearing_deg:.2f}°)")
        print(f"  XAxisOrdinate:         {xaxis_o:.6f}   (sin {bearing_deg:.2f}°)")
        print(f"  Currently injected:    {XAXIS_ABSCISSA} / {XAXIS_ORDINATE}")

        diff = abs(bearing_deg - math.degrees(math.atan2(XAXIS_ORDINATE, XAXIS_ABSCISSA)))
        if diff < 1.0:
            print("  ✅ Injected rotation matches ALKIS bearing.")
        else:
            print(f"  ⚠ Mismatch — update XAXIS_ABSCISSA / XAXIS_ORDINATE in CONFIG.")

        arrow_end = _arrow_endpoint(ifc_lat, ifc_lon, bearing_deg, length_m=40)
        folium.PolyLine(
            locations=[[ifc_lat, ifc_lon], list(arrow_end)],
            color="#e67e22", weight=3,
            tooltip=f"Long-axis bearing: {bearing_deg:.1f}° from East",
        ).add_to(m)
        folium.CircleMarker(
            location=list(arrow_end), radius=5,
            color="#e67e22", fill=True, fill_color="#e67e22",
        ).add_to(m)
    else:
        print("\n  ⚠ No ALKIS polygon — map shows IFC origin marker only.")

    legend_html = """
    <div style="
        position:fixed; bottom:30px; left:30px; z-index:9999;
        background:white; padding:10px 14px; border-radius:6px;
        border:1px solid #ccc; font-family:Arial,sans-serif; font-size:13px;
        box-shadow:2px 2px 6px rgba(0,0,0,0.15);">
      <b>Nexus3D Stuttgart — Bau 4 Verification</b><br><br>
      <span style="color:#e74c3c">&#9679;</span> IFC origin (IfcMapConversion)<br>
      <span style="color:#2980b9">&#9632;</span> ALKIS footprint (ground truth)<br>
      <span style="color:#e67e22">&#9654;</span> Long-axis bearing<br>
      <span style="color:#27ae60">&#9675;</span> 10 m accuracy ring
    </div>
    """
    m.get_root().html.add_child(folium.Element(legend_html))
    m.save(out_path)
    print(f"\n  Folium map saved → {out_path}")


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    _header("Nexus3D Stuttgart — IFC Auto-Georeferencer  (Stage 0)")

    if not os.path.exists(IFC_PATH):
        print(f"\n❌  IFC file not found:\n    {IFC_PATH}")
        sys.exit(1)

    print(f"\nLoading:  {os.path.basename(IFC_PATH)}")
    ifc_file = ifcopenshell.open(IFC_PATH)
    print(f"Loaded:   {len(list(ifc_file))} entities")

    # Stage A — Geocoding
    _sep()
    print("STAGE A — Geocoding")
    _sep()
    easting, northing = geocode(BUILDING_ADDRESS)

    # Stage B — IFC inspection
    _sep()
    print("STAGE B — IFC Inspection")
    _sep()
    info = inspect_ifc(ifc_file)

    # Stage C — Inject georeferencing
    _sep()
    print("STAGE C — Injecting Georeferencing")
    _sep()
    final_e, final_n, final_h = inject_georeferencing(
        ifc_file, easting, northing, TARGET_HEIGHT_M, info
    )

    # Save
    base, ext = os.path.splitext(IFC_PATH)
    out_path  = base + "_georef" + ext
    ifc_file.write(out_path)
    print(f"\n  Saved: {os.path.basename(out_path)}")

    # Stage D — Field verification
    _sep()
    print("STAGE D — Field Verification (re-reading saved file)")
    _sep()
    verify_output(out_path, final_e, final_n)

    # Stage E — Visual verification + rotation
    if FOLIUM_OUTPUT:
        _sep()
        print("STAGE E — Visual Verification + Rotation Calculator")
        _sep()
        generate_folium_map(final_e, final_n, FOLIUM_OUTPUT)

    # Summary
    _sep("═")
    print("  DONE")
    _sep("═")
    print(f"  Input:    {os.path.basename(IFC_PATH)}")
    print(f"  Output:   {os.path.basename(out_path)}")
    print(f"  CRS:      EPSG:25832 (ETRS89 / UTM 32N)")
    print(f"  Origin:   E={final_e:.3f}  N={final_n:.3f}  H={final_h:.3f} m")
    print(f"  Rotation: XAxisAbscissa={XAXIS_ABSCISSA}  XAxisOrdinate={XAXIS_ORDINATE}")
    if FOLIUM_OUTPUT:
        print(f"  Map:      {FOLIUM_OUTPUT}")
    _sep("═")
    print("\nNext steps:")
    print("  1. Open hft_bau4_verify.html — confirm red marker inside blue polygon")
    print("  2. Stage 2+3: run ifc_to_postgis.py")
    print("  3. Stage 4: PostGIS reconciliation SQL (join with ALKIS schema)")


if __name__ == "__main__":
    main()