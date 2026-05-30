
import ifcopenshell
import ifcopenshell.util.placement
from pyproj import Transformer
import folium

ifc = ifcopenshell.open(r"C:\Users\abhir\PycharmProjects\stuttgart_gis\IFCGeoreferencing\HFT_Bau4_2025.04.22_georef.ifc")

# Read the injected MapConversion
mc = ifc.by_type("IfcMapConversion")[0]
e, n = mc.Eastings, mc.Northings

# Convert UTM 32N → WGS84 for the map
transformer = Transformer.from_crs("EPSG:25832", "EPSG:4326", always_xy=True)
lon, lat = transformer.transform(e, n)

print(f"Building origin: {lat:.6f}°N, {lon:.6f}°E")
print(f"Expected:         48.780274°N, 9.172525°E")

# Plot
m = folium.Map(location=[lat, lon], zoom_start=19, tiles="OpenStreetMap")
folium.Marker([lat, lon], popup=f"HFT Bau4\n{e:.1f}E, {n:.1f}N").add_to(m)
folium.Circle([lat, lon], radius=50, color="red", fill=False).add_to(m)
m.save("hft_bau4_verify.html")
print("Saved: hft_bau4_verify.html — open in browser")