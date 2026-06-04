import ifcopenshell
import ifcopenshell.geom

settings = ifcopenshell.geom.settings()
settings.set(settings.USE_WORLD_COORDS, True)
settings.set(settings.WELD_VERTICES, True)

ifc = ifcopenshell.open("HFT_Bau4_2025.04.22_georef.ifc")

# Get one wall
wall = ifc.by_type("IfcWall")[0]
shape = ifcopenshell.geom.create_shape(settings, wall)

verts = shape.geometry.verts
# Print first 3 vertices
for i in range(0, 9, 3):
    print(f"  vertex: {verts[i]:.4f}, {verts[i+1]:.4f}, {verts[i+2]:.4f}")