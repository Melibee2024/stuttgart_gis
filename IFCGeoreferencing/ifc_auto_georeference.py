"""
ifc_auto_georeference.py
========================
Automatically georeferences an IFC file using:
  1. Nominatim (OSM) to geocode a building address → WGS84
  2. pyproj to convert WGS84 → EPSG:25832 (UTM 32N)
  3. IfcOpenShell to inject IfcMapConversion + IfcProjectedCRS
  4. Saves a corrected *_georef.ifc file ready for ogr2ogr / PostGIS

Requirements:
    pip install ifcopenshell pyproj requests
"""

import os
import sys
import json
import time
import requests
import numpy as np
import ifcopenshell
import ifcopenshell.api
import ifcopenshell.util.placement
from pyproj import Transformer

# ─────────────────────────────────────────────
# CONFIG — edit these two values only
# ─────────────────────────────────────────────
IFC_PATH = r"C:\Users\abhir\PycharmProjects\stuttgart_gis\IFCGeoreferencing\HFT_Bau4_2025.04.22.ifc"
BUILDING_ADDRESS = "Hochschule für Technik Stuttgart, Schellingstraße 24, Stuttgart"
TARGET_HEIGHT_M = 245.0   # Ground elevation in DHHN2016 (Stuttgart avg)
# ─────────────────────────────────────────────


def dms_to_dd(dms_tuple):
    """Convert IFC DMS tuple (deg, min, sec, millionths_of_sec) to decimal degrees."""
    d, m, s, ms = dms_tuple
    dd = abs(d) + m / 60 + s / 3600 + ms / 3_600_000_000
    return -dd if d < 0 else dd


def nominatim_query(query: str) -> tuple[float, float] | None:
    """Single Nominatim query. Returns (lat, lon) or None if no results."""
    url = "https://nominatim.openstreetmap.org/search"
    params = {"q": query, "format": "json", "limit": 1}
    headers = {"User-Agent": "Nexus3D-Stuttgart-Georef/1.0"}
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=10)
        resp.raise_for_status()
        results = resp.json()
        if results:
            return float(results[0]["lat"]), float(results[0]["lon"])
    except Exception:
        pass
    time.sleep(1)  # rate limit between retries
    return None


def geocode_address(address: str) -> tuple[float, float]:
    """
    Try progressively simpler Nominatim queries, then fall back to
    a hardcoded coordinate table for known Stuttgart buildings.
    """
    print(f"\n[1/4] Geocoding address via Nominatim...")

    # Build a fallback query chain from the original address
    fallback_queries = [
        address,
        # Strip house number — try street + city only
        ", ".join(address.split(", ")[1:]) if ", " in address else address,
        # Try just the institution name + city
        address.split(",")[0] + ", Stuttgart",
        # Bare street + postcode
        "Schellingstrasse 24, 70174 Stuttgart",
        # Just the postcode area
        "70174 Stuttgart",
    ]
    # Deduplicate while preserving order
    seen = set()
    fallback_queries = [q for q in fallback_queries if not (q in seen or seen.add(q))]

    for query in fallback_queries:
        print(f"      Trying: '{query}'")
        result = nominatim_query(query)
        if result:
            lat, lon = result
            print(f"      ✓ Found → Lat: {lat:.6f}, Lon: {lon:.6f}")
            return lat, lon

    # ── Hardcoded fallback table for known Stuttgart buildings ──────────────
    # Add entries here as you onboard more buildings
    KNOWN_BUILDINGS = {
        "hft":        (48.69202, 9.12153),   # HFT Stuttgart main campus
        "bau4":       (48.69202, 9.12153),   # HFT Bau 4
        "schellingstrasse": (48.69202, 9.12153),
    }

    address_lower = address.lower()
    for keyword, coords in KNOWN_BUILDINGS.items():
        if keyword in address_lower:
            lat, lon = coords
            print(f"      ⚠ Nominatim failed — using hardcoded coords for '{keyword}'")
            print(f"        Lat: {lat:.6f}, Lon: {lon:.6f}")
            print(f"        (Verify in QGIS and update KNOWN_BUILDINGS if needed)")
            return lat, lon

    raise ValueError(
        f"Could not geocode: '{address}'\n"
        f"All {len(fallback_queries)} Nominatim queries failed and no hardcoded "
        f"entry matched.\n"
        f"Fix: Add your building to the KNOWN_BUILDINGS dict in geocode_address()."
    )


def wgs84_to_utm32n(lat: float, lon: float) -> tuple[float, float]:
    """Convert WGS84 (lat, lon) → EPSG:25832 (easting, northing)."""
    print(f"\n[2/4] Converting WGS84 → EPSG:25832 (UTM 32N)...")
    transformer = Transformer.from_crs("EPSG:4326", "EPSG:25832", always_xy=True)
    easting, northing = transformer.transform(lon, lat)
    print(f"      Easting:  {easting:.3f} m")
    print(f"      Northing: {northing:.3f} m")
    return easting, northing


def inspect_ifc(ifc_file) -> dict:
    """
    Extract diagnostic info from the IFC file:
    local origin offset, existing lat/lon fields, unit scale.
    """
    print(f"\n[3/4] Inspecting IFC file...")
    info = {
        "local_offset_x": 0.0,
        "local_offset_y": 0.0,
        "local_offset_z": 0.0,
        "existing_lat": None,
        "existing_lon": None,
        "has_map_conversion": False,
        "scale_to_meters": 1.0,
    }

    # Check unit scale (mm vs m)
    for unit in ifc_file.by_type("IfcSIUnit"):
        if unit.UnitType == "LENGTHUNIT":
            if hasattr(unit, "Prefix") and unit.Prefix == "MILLI":
                info["scale_to_meters"] = 0.001
                print("      ⚠ Length unit is MILLIMETRES — scale factor 0.001 applied")
            else:
                print("      Length unit: METRES ✓")

    # Check for existing MapConversion
    if ifc_file.by_type("IfcMapConversion"):
        info["has_map_conversion"] = True
        print("      ⚠ IfcMapConversion already exists — will be overwritten")

    # Extract site local placement (the offset to subtract)
    sites = ifc_file.by_type("IfcSite")
    if sites:
        mat = ifcopenshell.util.placement.get_local_placement(sites[0].ObjectPlacement)
        info["local_offset_x"] = float(mat[0, 3])
        info["local_offset_y"] = float(mat[1, 3])
        info["local_offset_z"] = float(mat[2, 3])
        print(f"      Site local offset: X={info['local_offset_x']:.4f}, "
              f"Y={info['local_offset_y']:.4f}, Z={info['local_offset_z']:.4f}")

        # Check legacy lat/lon fields
        site = sites[0]
        if site.RefLatitude:
            info["existing_lat"] = dms_to_dd(site.RefLatitude)
        if site.RefLongitude:
            info["existing_lon"] = dms_to_dd(site.RefLongitude)
        if info["existing_lat"]:
            print(f"      Existing RefLatitude:  {info['existing_lat']:.6f} (legacy DMS)")
            print(f"      Existing RefLongitude: {info['existing_lon']:.6f} (legacy DMS)")
            if not (47.5 < info["existing_lat"] < 49.5):
                print("      ⚠ Existing lat/lon does NOT match Stuttgart — ignoring it")
    else:
        print("      No IfcSite found — offset assumed (0, 0, 0)")

    return info


def inject_georeferencing(ifc_file, easting, northing, height, info):
    """
    Write IfcMapConversion + IfcProjectedCRS into the IFC file.
    Subtracts the local site offset so geometry lands at the correct position.
    """
    print(f"\n[4/4] Injecting georeferencing...")

    scale = info["scale_to_meters"]

    # Subtract local offset (convert to metres if needed)
    corrected_easting  = easting  - info["local_offset_x"] * scale
    corrected_northing = northing - info["local_offset_y"] * scale
    corrected_height   = height   - info["local_offset_z"] * scale

    print(f"      Local offset subtracted:")
    print(f"        Easting  {easting:.3f} - {info['local_offset_x'] * scale:.4f} = {corrected_easting:.3f}")
    print(f"        Northing {northing:.3f} - {info['local_offset_y'] * scale:.4f} = {corrected_northing:.3f}")

    # Get a valid IfcSIUnit for MapUnit (metres)
    si_units = ifc_file.by_type("IfcSIUnit")
    length_unit = next(
        (u for u in si_units if u.UnitType == "LENGTHUNIT"), si_units[0]
    )

    # Remove existing georeferencing if present
    if info["has_map_conversion"]:
        for mc in ifc_file.by_type("IfcMapConversion"):
            ifc_file.remove(mc)
        for crs in ifc_file.by_type("IfcProjectedCRS"):
            ifc_file.remove(crs)

    # Inject
    ifcopenshell.api.run("georeference.add_georeferencing", ifc_file)
    ifcopenshell.api.run(
        "georeference.edit_georeferencing",
        ifc_file,
        coordinate_operation={
            "Eastings":          corrected_easting,
            "Northings":         corrected_northing,
            "OrthogonalHeight":  corrected_height,
            "XAxisAbscissa":     1.0,   # No rotation — adjust if QGIS snapping gives heading
            "XAxisOrdinate":     0.0,
            "Scale":             1.0,
        },
        projected_crs={
            "Name":             "EPSG:25832",
            "Description":      "ETRS89 / UTM zone 32N",
            "GeodeticDatum":    "ETRS89",
            "MapProjection":    "UTM",
            "MapZone":          "32N",
            "MapUnit":          length_unit,
        },
    )

    print(f"      IfcMapConversion injected ✓")
    print(f"      IfcProjectedCRS: EPSG:25832 ✓")


def main():
    # ── Load IFC ──────────────────────────────
    if not os.path.exists(IFC_PATH):
        print(f"ERROR: IFC file not found: {IFC_PATH}")
        sys.exit(1)

    print(f"Loading IFC: {os.path.basename(IFC_PATH)}")
    ifc_file = ifcopenshell.open(IFC_PATH)

    # ── Geocode address ───────────────────────
    lat, lon = geocode_address(BUILDING_ADDRESS)
    time.sleep(1)  # Nominatim rate limit: 1 req/sec

    # ── Convert to UTM 32N ────────────────────
    easting, northing = wgs84_to_utm32n(lat, lon)

    # ── Inspect IFC ───────────────────────────
    info = inspect_ifc(ifc_file)

    # ── Inject georef ─────────────────────────
    inject_georeferencing(ifc_file, easting, northing, TARGET_HEIGHT_M, info)

    # ── Save output ───────────────────────────
    base, ext = os.path.splitext(IFC_PATH)
    out_path = base + "_georef" + ext
    ifc_file.write(out_path)

    print(f"\n{'─'*55}")
    print(f"  ✅ SUCCESS")
    print(f"  Output: {out_path}")
    print(f"  CRS:    EPSG:25832 (ETRS89 / UTM 32N)")
    print(f"  Origin: E={easting:.2f}, N={northing:.2f}, H={TARGET_HEIGHT_M:.1f}m")
    print(f"{'─'*55}")
    print(f"\nNext steps:")
    print(f"  1. Open HFT_Bau4_georef.ifc in QGIS and verify it lands on Stuttgart")
    print(f"  2. If rotation is off, adjust XAxisAbscissa/XAxisOrdinate in the script")
    print(f"  3. Run ogr2ogr on the _georef.ifc for PostGIS ingestion")


if __name__ == "__main__":
    main()