from pyproj import Transformer
t = Transformer.from_crs("EPSG:4326", "EPSG:25832", always_xy=True)
lat, lon = 48.780032, 9.171674   # ← replace with your OSM values
e, n = t.transform(lon, lat)
print(f"VERIFIED_EASTING  = {e:.3f}")
print(f"VERIFIED_NORTHING = {n:.3f}")