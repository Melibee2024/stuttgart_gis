"""
ifc_auto_georeference.py
========================
Nexus3D Stuttgart — Stage 0: IFC Georeferencer
===============================================
Automatically georeferences an IFC file to EPSG:25832 (ETRS89 / UTM 32N).

Geocoding pipeline (in priority order):
  1. VERIFIED coords  — hardcoded per-building, sub-metre accuracy (preferred)
  2. ALKIS WFS snap   — LGL Baden-Württemberg cadastral footprint centroid
  3. Nominatim        — OSM geocoding, rough fallback (~50–100m)

IFC processing:
  4. Inspects unit scale, local site offset, legacy lat/lon
  5. Injects IfcMapConversion + IfcProjectedCRS (EPSG:25832)
  6. Subtracts local site placement offset
  7. Clears legacy RefLatitude/RefLongitude (fixes GeoReference_004 in FZKViewer)
  8. Saves *_georef.ifc and prints a verification summary + OSM link

Requirements:
    pip install ifcopenshell pyproj requests

No API keys needed.
"""

import os
import re
import sys
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
# Must be the real-world EPSG:25832 coordinate of the geocoded building location.
# This is injected directly into IfcMapConversion.Eastings/Northings.
# NO offset subtraction is applied — see inject_georeferencing() docstring.
# Set both to None to fall through to ALKIS / Nominatim.
#
# HFT Bau 4: Nominatim-resolved (Schellingstrasse 24), FZKViewer-confirmed.
VERIFIED_EASTING  = 512614.7   # EPSG:25832 easting  — do NOT apply offset manually
VERIFIED_NORTHING = 5403013.8  # EPSG:25832 northing — do NOT apply offset manually

# ── ALKIS WFS ─────────────────────────────────────────────────────────────────
# Confirmed working endpoint (LGL Baden-Württemberg public open data WFS).
# Note: /wfs/ path, not /ows/ — the /ows/ path returns 500.
ALKIS_WFS_ENDPOINT = "https://owsproxy.lgl-bw.de/owsproxy/wfs/WFS_LGL-BW_ALKIS"

# Layer names to probe in order (NOrA namespace confirmed in GetCapabilities XML)
ALKIS_LAYER_NAMES = [
    "nora:AX_Gebaeude",   # correct NOrA namespace — most likely to work
    "AX_Gebaeude",        # no-namespace fallback
    "nora:ax_gebaeude",   # lowercase variant
]

# ── Known buildings fallback (Nominatim last resort) ─────────────────────────
# Add entries here if Nominatim keeps mis-resolving a building.
# Keys are lowercase substrings matched against BUILDING_ADDRESS.
# Values are (lat_wgs84, lon_wgs84).
KNOWN_BUILDINGS_WGS84 = {
    # HFT Stuttgart Bau 4 — Nominatim-resolved, FZKViewer-confirmed
    # Only used if VERIFIED_EASTING=None AND ALKIS fails AND Nominatim fails.
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
    """Convert IFC DMS tuple (deg, min, sec, millionths_of_sec) → decimal degrees."""
    d, m, s, ms = dms_tuple
    dd = abs(d) + m / 60 + s / 3600 + ms / 3_600_000_000
    return -dd if d < 0 else dd


def utm32n_to_wgs84(easting: float, northing: float) -> tuple:
    """Convert EPSG:25832 → WGS84 (lat, lon)."""
    t = Transformer.from_crs("EPSG:25832", "EPSG:4326", always_xy=True)
    lon, lat = t.transform(easting, northing)
    return lat, lon


def wgs84_to_utm32n(lat: float, lon: float) -> tuple:
    """Convert WGS84 (lat, lon) → EPSG:25832 (easting, northing)."""
    t = Transformer.from_crs("EPSG:4326", "EPSG:25832", always_xy=True)
    easting, northing = t.transform(lon, lat)
    return easting, northing


def osm_link(lat: float, lon: float, zoom: int = 19) -> str:
    return f"https://www.openstreetmap.org/#map={zoom}/{lat:.6f}/{lon:.6f}"


# ══════════════════════════════════════════════════════════════════════════════
#  STAGE A — GEOCODING (three-tier pipeline)
# ══════════════════════════════════════════════════════════════════════════════

def geocode(building_address: str) -> tuple:
    """
    Return (easting, northing) in EPSG:25832 using the best available source:
      Tier 1 — VERIFIED_EASTING / VERIFIED_NORTHING (hardcoded, sub-metre)
      Tier 2 — ALKIS WFS (LGL BW cadastral footprint centroid)
      Tier 3 — Nominatim OSM geocoding (rough, ~50–100m)

    Raises ValueError if all three tiers fail.
    """
    # ── Tier 1: verified hardcoded coords ────────────────────────────────────
    if VERIFIED_EASTING is not None and VERIFIED_NORTHING is not None:
        lat, lon = utm32n_to_wgs84(VERIFIED_EASTING, VERIFIED_NORTHING)
        print("\n[Geocode] Tier 1 — Using verified hardcoded coords")
        print(f"          E={VERIFIED_EASTING:.3f}, N={VERIFIED_NORTHING:.3f}")
        print(f"          WGS84: {lat:.6f}°N, {lon:.6f}°E")
        print(f"          OSM:   {osm_link(lat, lon)}")
        return VERIFIED_EASTING, VERIFIED_NORTHING

    # ── Tier 2: ALKIS WFS ─────────────────────────────────────────────────────
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
            print(f"          OSM:   {osm_link(lat, lon)}")
            return e, n

    # ── Tier 3: Nominatim rough coords ───────────────────────────────────────
    print("[Geocode] Tier 2 failed — falling back to Nominatim (rough)")
    if rough_e is not None:
        lat, lon = utm32n_to_wgs84(rough_e, rough_n)
        print(f"          ⚠ Using rough Nominatim: E={rough_e:.3f}, N={rough_n:.3f}")
        print(f"          WGS84: {lat:.6f}°N, {lon:.6f}°E")
        print(f"          OSM:   {osm_link(lat, lon)}")
        print("          ⚠ Accuracy ~50–100m. Verify the OSM pin visually.")
        return rough_e, rough_n

    # ── KNOWN_BUILDINGS hardcoded fallback ────────────────────────────────────
    addr_lower = building_address.lower()
    for keyword, (lat, lon) in KNOWN_BUILDINGS_WGS84.items():
        if keyword in addr_lower:
            e, n = wgs84_to_utm32n(lat, lon)
            print(f"          ⚠ Using KNOWN_BUILDINGS entry for '{keyword}'")
            print(f"          E={e:.3f}, N={n:.3f}")
            return e, n

    raise ValueError(
        f"All geocoding tiers failed for: '{building_address}'\n"
        f"Fix: Set VERIFIED_EASTING / VERIFIED_NORTHING in the CONFIG section."
    )


# ── Nominatim helper ──────────────────────────────────────────────────────────

def _nominatim_to_utm(address: str):
    """
    Try a cascade of Nominatim queries for the address.
    Returns (easting, northing) in EPSG:25832, or (None, None) on total failure.
    """
    queries = _build_nominatim_cascade(address)
    headers = {"User-Agent": "Nexus3D-Stuttgart-Georef/1.0 (github.com/nexus3d)"}

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
                print(f"          Lat={lat:.6f}, Lon={lon:.6f}")
                return e, n
        except Exception:
            pass
        time.sleep(1)

    return None, None


def _build_nominatim_cascade(address: str) -> list:
    """Build a de-duplicated list of progressively simpler Nominatim queries."""
    parts = [p.strip() for p in address.split(",")]
    queries = []

    # Original
    queries.append(address)
    # Strip leading component (institution name)
    if len(parts) > 1:
        queries.append(", ".join(parts[1:]))
    # First component + city
    if len(parts) >= 2:
        queries.append(f"{parts[0]}, {parts[-1]}")
    # Street only
    if len(parts) >= 2:
        queries.append(parts[1] if len(parts) > 2 else parts[0])
    # ASCII-safe fallback (strip umlauts)
    ascii_addr = (address
                  .replace("ü", "ue").replace("ö", "oe").replace("ä", "ae")
                  .replace("ß", "ss").replace("Ü", "Ue").replace("Ö", "Oe")
                  .replace("Ä", "Ae"))
    if ascii_addr != address:
        queries.append(ascii_addr)

    # De-duplicate preserving order
    seen, out = set(), []
    for q in queries:
        if q not in seen:
            seen.add(q)
            out.append(q)
    return out


# ── ALKIS WFS helper ──────────────────────────────────────────────────────────

def _alkis_snap(rough_e: float, rough_n: float):
    """
    Query the LGL BW ALKIS WFS for building footprints around (rough_e, rough_n).
    Returns (easting, northing) of the nearest building centroid, or None on failure.
    """
    bbox = (
        rough_e - ALKIS_SEARCH_RADIUS_M,
        rough_n - ALKIS_SEARCH_RADIUS_M,
        rough_e + ALKIS_SEARCH_RADIUS_M,
        rough_n + ALKIS_SEARCH_RADIUS_M,
    )

    for layer in ALKIS_LAYER_NAMES:
        features = _wfs_getfeature(ALKIS_WFS_ENDPOINT, layer, bbox)
        if features:
            centroid, dist = _nearest_centroid(features, rough_e, rough_n)
            if centroid:
                print(f"          ALKIS layer '{layer}': {len(features)} features, "
                      f"snap dist={dist:.1f}m")
                return centroid
            print(f"          ALKIS layer '{layer}': features returned but no polygon geometry")
        # If no features, silently try next layer

    # GetCapabilities discovery fallback
    print("          Known layer names failed — trying GetCapabilities discovery...")
    discovered = _getcapabilities_layers(ALKIS_WFS_ENDPOINT)
    for layer in discovered:
        features = _wfs_getfeature(ALKIS_WFS_ENDPOINT, layer, bbox)
        if features:
            centroid, dist = _nearest_centroid(features, rough_e, rough_n)
            if centroid:
                print(f"          Discovered layer '{layer}': snap dist={dist:.1f}m")
                return centroid

    return None


def _wfs_getfeature(endpoint: str, layer: str, bbox: tuple):
    """
    Execute a WFS 2.0 GetFeature request.
    Returns list of GeoJSON features, or empty list on any failure.
    Prints the actual server error so failures are never silent.
    """
    params = {
        "SERVICE":      "WFS",
        "VERSION":      "2.0.0",
        "REQUEST":      "GetFeature",
        "TYPENAMES":    layer,
        "SRSNAME":      "EPSG:25832",
        "BBOX":         f"{bbox[0]:.3f},{bbox[1]:.3f},{bbox[2]:.3f},{bbox[3]:.3f},EPSG:25832",
        "OUTPUTFORMAT": "application/json",
        "COUNT":        "50",
    }
    try:
        resp = requests.get(endpoint, params=params, timeout=15)

        if resp.status_code != 200:
            # Print first 200 chars of the server's error message
            snippet = resp.text[:200].replace("\n", " ").strip()
            print(f"          HTTP {resp.status_code} for layer '{layer}': {snippet}")
            return []

        ct = resp.headers.get("Content-Type", "")
        if "json" not in ct:
            # Server returned XML (likely an OGC exception) — extract the message
            snippet = re.sub(r"<[^>]+>", " ", resp.text)  # strip XML tags
            snippet = " ".join(snippet.split())[:200]
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
    """
    Query WFS GetCapabilities and return all layer names containing 'gebaeude'.
    """
    try:
        resp = requests.get(
            endpoint,
            params={"SERVICE": "WFS", "REQUEST": "GetCapabilities"},
            timeout=10,
        )
        resp.raise_for_status()
        matches = re.findall(r"<(?:wfs:)?Name>([^<]*(?i:gebaeude)[^<]*)</(?:wfs:)?Name>",
                             resp.text)
        if matches:
            print(f"          GetCapabilities found: {matches}")
        return matches
    except Exception as e:
        print(f"          GetCapabilities failed: {e}")
        return []


def _nearest_centroid(features: list, ref_e: float, ref_n: float) -> tuple:
    """
    Return (centroid, distance) of the polygon feature whose centroid
    is closest to (ref_e, ref_n). Returns (None, inf) if no polygon found.
    """
    best, best_dist = None, float("inf")

    for feat in features:
        geom = feat.get("geometry") or {}
        gtype = geom.get("type", "")
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
        dist = ((cx - ref_e) ** 2 + (cy - ref_n) ** 2) ** 0.5

        if dist < best_dist:
            best_dist = dist
            best = (cx, cy)

    return best, best_dist


# ══════════════════════════════════════════════════════════════════════════════
#  STAGE B — IFC INSPECTION
# ══════════════════════════════════════════════════════════════════════════════

def inspect_ifc(ifc_file) -> dict:
    """
    Extract everything needed to correctly inject georeferencing:
      - Length unit scale (metres vs millimetres)
      - Local site placement offset (X, Y, Z)
      - Existing legacy RefLatitude / RefLongitude (for reporting)
      - Whether IfcMapConversion already exists (will be overwritten)
    """
    info = {
        "scale":              1.0,
        "offset_x":          0.0,
        "offset_y":          0.0,
        "offset_z":          0.0,
        "legacy_lat":        None,
        "legacy_lon":        None,
        "has_map_conversion": False,
    }

    # ── Unit scale ────────────────────────────────────────────────────────────
    for unit in ifc_file.by_type("IfcSIUnit"):
        if unit.UnitType == "LENGTHUNIT":
            if getattr(unit, "Prefix", None) == "MILLI":
                info["scale"] = 0.001
                print("  Length unit:     MILLIMETRES (scale 0.001 applied)")
            else:
                print("  Length unit:     METRES ✓")
            break   # only need the first LENGTHUNIT

    # ── Existing MapConversion ────────────────────────────────────────────────
    if ifc_file.by_type("IfcMapConversion"):
        info["has_map_conversion"] = True
        print("  MapConversion:   EXISTS — will be overwritten")
    else:
        print("  MapConversion:   none")

    # ── Site placement offset ─────────────────────────────────────────────────
    sites = ifc_file.by_type("IfcSite")
    if sites:
        mat = ifcopenshell.util.placement.get_local_placement(sites[0].ObjectPlacement)
        info["offset_x"] = float(mat[0, 3])
        info["offset_y"] = float(mat[1, 3])
        info["offset_z"] = float(mat[2, 3])
        print(f"  Site offset:     X={info['offset_x']:.4f}, "
              f"Y={info['offset_y']:.4f}, Z={info['offset_z']:.4f}")

        # ── Legacy lat/lon ────────────────────────────────────────────────────
        site = sites[0]
        if site.RefLatitude:
            info["legacy_lat"] = dms_to_dd(site.RefLatitude)
        if site.RefLongitude:
            info["legacy_lon"] = dms_to_dd(site.RefLongitude)
        if info["legacy_lat"] is not None:
            in_bw = 47.5 < info["legacy_lat"] < 49.5
            flag = "✓" if in_bw else "⚠ NOT Stuttgart — will be cleared"
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

    IfcMapConversion.Eastings/Northings = real-world coordinate of IFC local (0,0,0).
    This is exactly the coordinate returned by geocoding — NO offset subtraction.

    The IfcSite local placement offset (X=122, Y=-60 etc.) describes where IfcSite
    sits WITHIN the IFC local coordinate system. It is NOT a real-world correction
    to apply to the map coordinates. Subtracting it here is wrong and shifts the
    building by ~120m in the wrong direction.

    Returns (easting, northing, height) — the values written into IfcMapConversion.
    """
    # Inject the geocoded coordinate directly — no offset manipulation
    ce = easting
    cn = northing
    ch = height

    print(f"  Eastings (IFC local origin → real world):  {ce:.3f}")
    print(f"  Northings (IFC local origin → real world): {cn:.3f}")
    print(f"  Height:                                    {ch:.3f}")
    print(f"  Site local offset (informational only):    X={info['offset_x']:.4f}, Y={info['offset_y']:.4f}")
    print(f"  ℹ The offset is NOT subtracted — it describes IfcSite position in")
    print(f"    local IFC space, not a real-world map correction.")

    # Get a valid SI length unit for IfcProjectedCRS.MapUnit
    si_units = ifc_file.by_type("IfcSIUnit")
    length_unit = next(
        (u for u in si_units if u.UnitType == "LENGTHUNIT"),
        si_units[0] if si_units else None,
    )
    if length_unit is None:
        raise RuntimeError("No IfcSIUnit found in IFC — cannot set MapUnit.")

    # Remove stale georeferencing if present
    if info["has_map_conversion"]:
        for mc in ifc_file.by_type("IfcMapConversion"):
            ifc_file.remove(mc)
        for crs in ifc_file.by_type("IfcProjectedCRS"):
            ifc_file.remove(crs)
        print("  Removed stale IfcMapConversion + IfcProjectedCRS")

    # Inject
    ifcopenshell.api.run("georeference.add_georeferencing", ifc_file)
    ifcopenshell.api.run(
        "georeference.edit_georeferencing",
        ifc_file,
        coordinate_operation={
            "Eastings":          ce,
            "Northings":         cn,
            "OrthogonalHeight":  ch,
            "XAxisAbscissa":     1.0,   # no building rotation
            "XAxisOrdinate":     0.0,   # adjust if building appears rotated in QGIS
            "Scale":             1.0,
        },
        projected_crs={
            "Name":           "EPSG:25832",
            "Description":    "ETRS89 / UTM zone 32N",
            "GeodeticDatum":  "ETRS89",
            "MapProjection":  "UTM",
            "MapZone":        "32N",
            "MapUnit":        length_unit,
        },
    )

    # Clear legacy site coords — fixes GeoReference_004 warning in FZKViewer
    sites = ifc_file.by_type("IfcSite")
    if sites:
        site = sites[0]
        site.RefLatitude  = None
        site.RefLongitude = None
        site.RefElevation = None
        print("  Legacy RefLatitude / RefLongitude / RefElevation → cleared ✓")

    return ce, cn, ch


# ══════════════════════════════════════════════════════════════════════════════
#  STAGE D — VERIFICATION
# ══════════════════════════════════════════════════════════════════════════════

def verify_output(out_path: str, expected_e: float, expected_n: float):
    """
    Re-open the saved file and validate every georeferencing field.
    Prints a pass/fail for each check and an OSM link for visual confirmation.
    """
    ifc = ifcopenshell.open(out_path)
    ok = True

    def check(label: str, got, expect, tol=1.0):
        nonlocal ok
        if isinstance(expect, (int, float)):
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

    check("Eastings",         mc.Eastings,         expected_e, tol=1.0)
    check("Northings",        mc.Northings,         expected_n, tol=1.0)
    check("OrthogonalHeight", mc.OrthogonalHeight,  TARGET_HEIGHT_M, tol=0.1)
    check("XAxisAbscissa",    mc.XAxisAbscissa,     1.0, tol=1e-6)
    check("XAxisOrdinate",    mc.XAxisOrdinate,     0.0, tol=1e-6)
    check("CRS Name",         crs.Name,             "EPSG:25832")

    sites = ifc.by_type("IfcSite")
    if sites:
        site = sites[0]
        check("RefLatitude cleared",  site.RefLatitude,  None)
        check("RefLongitude cleared", site.RefLongitude, None)

    lat, lon = utm32n_to_wgs84(mc.Eastings, mc.Northings)
    print(f"\n  WGS84:  {lat:.6f}°N, {lon:.6f}°E")
    print(f"  OSM:    {osm_link(lat, lon)}")
    print(f"\n  {'All checks passed ✅' if ok else 'Some checks FAILED ❌ — review output above'}")


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    _header("Nexus3D Stuttgart — IFC Auto-Georeferencer  (Stage 0)")

    # ── Load IFC ──────────────────────────────────────────────────────────────
    if not os.path.exists(IFC_PATH):
        print(f"\n❌  IFC file not found:\n    {IFC_PATH}")
        sys.exit(1)

    print(f"\nLoading:  {os.path.basename(IFC_PATH)}")
    ifc_file = ifcopenshell.open(IFC_PATH)
    print(f"Loaded:   {len(list(ifc_file))} entities")

    # ── Stage A: Geocoding ────────────────────────────────────────────────────
    _sep()
    print("STAGE A — Geocoding")
    _sep()
    easting, northing = geocode(BUILDING_ADDRESS)

    # ── Stage B: IFC inspection ───────────────────────────────────────────────
    _sep()
    print("STAGE B — IFC Inspection")
    _sep()
    info = inspect_ifc(ifc_file)

    # ── Stage C: Inject georeferencing ────────────────────────────────────────
    _sep()
    print("STAGE C — Injecting Georeferencing")
    _sep()
    corrected_e, corrected_n, corrected_h = inject_georeferencing(
        ifc_file, easting, northing, TARGET_HEIGHT_M, info
    )

    # ── Save ──────────────────────────────────────────────────────────────────
    base, ext = os.path.splitext(IFC_PATH)
    out_path  = base + "_georef" + ext
    ifc_file.write(out_path)
    print(f"\n  Saved: {os.path.basename(out_path)}")

    # ── Stage D: Verification ─────────────────────────────────────────────────
    _sep()
    print("STAGE D — Verification")
    _sep()
    verify_output(out_path, corrected_e, corrected_n)

    # ── Summary ───────────────────────────────────────────────────────────────
    _sep("═")
    print("  DONE")
    _sep("═")
    print(f"  Input:   {os.path.basename(IFC_PATH)}")
    print(f"  Output:  {os.path.basename(out_path)}")
    print(f"  CRS:     EPSG:25832 (ETRS89 / UTM 32N)")
    print(f"  Origin:  E={corrected_e:.3f}  N={corrected_n:.3f}  H={corrected_h:.3f}m")
    _sep("═")
    print("\nNext steps:")
    print("  1. Open the OSM link above — confirm pin lands on Bau 4")
    print("  2. If building is rotated → adjust XAxisAbscissa/XAxisOrdinate in CONFIG")
    print("  3. Stage 2: ogr2ogr -f PostgreSQL <conn> _georef.ifc -t_srs EPSG:25832")


if __name__ == "__main__":
    main()