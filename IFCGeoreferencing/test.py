import ifcopenshell
import ifcopenshell.geom
import ifcopenshell.util.placement
import numpy as np

ifc_file = ifcopenshell.open(r"C:\Users\abhir\PycharmProjects\stuttgart_gis\IFCGeoreferencing\HFT_Bau4_2025.04.22.ifc")

# Check where the Site and Building are placed in local coords
site = ifc_file.by_type("IfcSite")
building = ifc_file.by_type("IfcBuilding")

if site:
    mat = ifcopenshell.util.placement.get_local_placement(site[0].ObjectPlacement)
    print("Site origin (local):", mat[:, 3])  # last column = translation

if building:
    mat = ifcopenshell.util.placement.get_local_placement(building[0].ObjectPlacement)
    print("Building origin (local):", mat[:, 3])

# Also check IfcSite lat/lon fields (sometimes populated even without MapConversion)
for s in ifc_file.by_type("IfcSite"):
    print("RefLatitude:",  s.RefLatitude)
    print("RefLongitude:", s.RefLongitude)
    print("RefElevation:", s.RefElevation)