#!/usr/bin/env python3
"""
Snapshot the running Nexus3D backend into static files under public/api and
public/media, so the GitHub Pages build serves all building data with NO backend.

Run with the backend up on http://localhost:5000:
    python scripts/snapshot-api.py

Produces:
  public/api/buildings/filters      (singleton — exact route, served as-is)
  public/api/georef                 (singleton)
  public/api/building-heights       (singleton)
  public/api/building-themes        (singleton)
  public/api/_buildings.json        { global_id -> full record }  (IFC element clicks)
  public/api/_qfield.json           { id -> record }              (surveyed buildings)
  public/media/...                  field photos
"""
import json, os, sys, shutil, urllib.request, urllib.parse

API = os.environ.get("SNAPSHOT_API", "http://localhost:5000")
HERE = os.path.dirname(os.path.abspath(__file__))
PUB  = os.path.normpath(os.path.join(HERE, "..", "public"))
MEDIA_SRC = os.path.normpath(os.path.join(HERE, "..", "..", "stuttgart-backend", "media"))

def get(path):
    with urllib.request.urlopen(API + path, timeout=90) as r:
        return r.read()

def get_json(path):
    return json.loads(get(path).decode("utf-8"))

def write_bytes(rel, data):
    p = os.path.join(PUB, *rel.split("/"))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(data)

def write_json(rel, obj):
    write_bytes(rel, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

# 1) Singletons — mirror the exact API route so `${API}/api/<x>` resolves as a file.
for ep in ("buildings/filters", "georef", "building-heights", "building-themes"):
    write_bytes(f"api/{ep}", get(f"/api/{ep}"))
    print("singleton:", ep)

# 2) Per-IFC-element records → one bundled index the frontend looks up in prod.
elements = get_json("/api/buildings")
print("IFC elements:", len(elements))
buildings = {}
fails = 0
for i, e in enumerate(elements):
    gid = e["global_id"]
    try:
        buildings[gid] = get_json("/api/buildings/" + urllib.parse.quote(gid, safe=""))
    except Exception as ex:
        fails += 1
        print(f"  skip {gid}: {ex}")
    if i % 200 == 0:
        print(f"  ...{i}/{len(elements)}  (fails={fails})", flush=True)
write_json("api/_buildings.json", buildings)
print("wrote _buildings.json:", len(buildings), "records")

# 3) Surveyed buildings (QField) → index keyed by every id the click might pass.
#    The click sends alkis_id || gmlid; the backend maps DEBW_ -> DEBWL. Cover both.
georef = get_json("/api/georef")
objectid = georef.get("objectid")            # e.g. DEBWL52210005DwE
qfield = {}
if objectid:
    candidates = {objectid}
    if objectid.startswith("DEBWL"):
        candidates.add("DEBW_" + objectid[len("DEBWL"):])
    rec = None
    for key in list(candidates):
        try:
            rec = get_json("/api/qfield/" + urllib.parse.quote(key, safe=""))
        except Exception as ex:
            print("  qfield miss for", key, ex); continue
    if rec:
        for k in (rec.get("gmlid"), rec.get("alkis_id"), *candidates):
            if k:
                qfield[k] = rec
        print("qfield surveyed building:", objectid, "photos:", rec.get("photo_count"))
write_json("api/_qfield.json", qfield)
print("wrote _qfield.json keys:", list(qfield.keys()))

# 4) Photos → public/media (served at ${API}/media/...)
if os.path.isdir(MEDIA_SRC):
    dst = os.path.join(PUB, "media")
    os.makedirs(dst, exist_ok=True)
    n = 0
    for root, _, files in os.walk(MEDIA_SRC):
        for fn in files:
            if fn == ".gitkeep":
                continue
            s = os.path.join(root, fn)
            rel = os.path.relpath(s, MEDIA_SRC)
            d = os.path.join(dst, rel)
            os.makedirs(os.path.dirname(d), exist_ok=True)
            shutil.copy2(s, d)
            n += 1
    print("copied photos:", n)
else:
    print("no media dir at", MEDIA_SRC)

print("DONE")
