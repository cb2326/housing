"""One-time export of Armenian boundaries and town points from OpenStreetMap (Overpass API) into GeoJSON.
Output: <outdir>/armenia-areas.geojson (11 marz-level units + 12 Yerevan districts) and <outdir>/osm-places.json."""
import json, math, sys, time, urllib.parse, urllib.request

OUT = sys.argv[1]
API = "https://overpass-api.de/api/interpreter"
UA = {"User-Agent": "open-housing-atlas-armenia-setup/1.0 (one-time boundary export)"}

import hashlib, os
def overpass(q):
    cache = f"{OUT}/raw-{hashlib.md5(q.encode()).hexdigest()[:10]}.json"
    if os.path.exists(cache):
        return json.load(open(cache, encoding="utf-8"))
    data = urllib.parse.urlencode({"data": q}).encode()
    for attempt in range(4):
        try:
            req = urllib.request.Request(API, data=data, headers=UA)
            with urllib.request.urlopen(req, timeout=300) as r:
                txt = r.read().decode("utf-8")
            open(cache, "w", encoding="utf-8").write(txt)
            return json.loads(txt)
        except urllib.error.HTTPError as e:
            if e.code not in (429, 502, 503, 504) or attempt == 3:
                raise
            time.sleep(20 * (attempt + 1))

def stitch(ways):
    """Join way segments (lists of [lon,lat]) into closed rings."""
    segs = [w[:] for w in ways if len(w) >= 2]
    rings = []
    while segs:
        ring = segs.pop(0)
        changed = True
        while ring[0] != ring[-1] and changed:
            changed = False
            for i, s in enumerate(segs):
                if s[0] == ring[-1]:
                    ring += s[1:]
                elif s[-1] == ring[-1]:
                    ring += s[::-1][1:]
                elif s[-1] == ring[0]:
                    ring = s[:-1] + ring
                elif s[0] == ring[0]:
                    ring = s[::-1][:-1] + ring
                else:
                    continue
                segs.pop(i); changed = True; break
        if ring[0] == ring[-1] and len(ring) >= 4:
            rings.append(ring)
    return rings

def dp(points, eps):
    """Douglas-Peucker line simplification."""
    if len(points) < 3:
        return points
    a, b = points[0], points[-1]
    dx, dy = b[0] - a[0], b[1] - a[1]
    norm = math.hypot(dx, dy) or 1e-12
    idx, dmax = 0, 0.0
    for i in range(1, len(points) - 1):
        p = points[i]
        d = abs(dy * p[0] - dx * p[1] + b[0] * a[1] - b[1] * a[0]) / norm
        if d > dmax:
            idx, dmax = i, d
    if dmax > eps:
        return dp(points[: idx + 1], eps)[:-1] + dp(points[idx:], eps)
    return [a, b]

def simplify_ring(ring, eps):
    # A closed ring starts and ends at the same point, so split it at the point farthest from the start first.
    far = max(range(len(ring)), key=lambda i: (ring[i][0] - ring[0][0]) ** 2 + (ring[i][1] - ring[0][1]) ** 2)
    s = dp(ring[: far + 1], eps)[:-1] + dp(ring[far:], eps)
    if len(s) < 4:
        s = ring[:: max(1, len(ring) // 6)] + [ring[0]]
    return [[round(x, 5), round(y, 5)] for x, y in s]

def area(r):
    return abs(sum(r[i][0] * r[i + 1][1] - r[i + 1][0] * r[i][1] for i in range(len(r) - 1))) / 2

def inside(pt, ring):
    x, y = pt; c = False
    for i in range(len(ring) - 1):
        (x1, y1), (x2, y2) = ring[i], ring[i + 1]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            c = not c
    return c

def to_feature(rel, eps, kind):
    outers = [[[n["lon"], n["lat"]] for n in m["geometry"]] for m in rel["members"] if m["type"] == "way" and m.get("role") in ("outer", "") and "geometry" in m]
    inners = [[[n["lon"], n["lat"]] for n in m["geometry"]] for m in rel["members"] if m["type"] == "way" and m.get("role") == "inner" and "geometry" in m]
    o_rings = [simplify_ring(r, eps) for r in stitch(outers)]
    i_rings = [simplify_ring(r, eps) for r in stitch(inners)]
    o_rings = [r for r in o_rings if area(r) > 1e-6] or o_rings
    polys = [[r] for r in sorted(o_rings, key=area, reverse=True)]
    for h in i_rings:
        for p in polys:
            if inside(h[0], p[0]):
                p.append(h); break
    t = rel["tags"]
    return {"type": "Feature",
            "properties": {"osm_id": rel["id"], "kind": kind, "name_hy": t.get("name:hy") or t.get("name"),
                           "name_en": t.get("name:en"), "admin_level": t.get("admin_level")},
            "geometry": {"type": "MultiPolygon", "coordinates": polys}}

feats = []
q_marz = '[out:json][timeout:240];area["ISO3166-1"="AM"]["admin_level"="2"]->.a;rel(area.a)["boundary"="administrative"]["admin_level"="4"];out geom;'
res = overpass(q_marz)
for rel in res["elements"]:
    feats.append(to_feature(rel, 0.004, "marz"))
print("marz-level:", len([f for f in feats if f["properties"]["kind"] == "marz"]))
time.sleep(5)
q_dist = '[out:json][timeout:240];area["name:en"="Yerevan"]["boundary"="administrative"]["admin_level"="4"]->.y;rel(area.y)["boundary"="administrative"]["admin_level"="5"];out geom;'
res = overpass(q_dist)
for rel in res["elements"]:
    feats.append(to_feature(rel, 0.0006, "district"))
print("districts:", len([f for f in feats if f["properties"]["kind"] == "district"]))
for f in feats:
    p = f["properties"]
    print(" ", p["kind"], p["osm_id"], p["name_en"], "| rings:", sum(len(pp) for pp in f["geometry"]["coordinates"]),
          "| pts:", sum(len(r) for pp in f["geometry"]["coordinates"] for r in pp))
with open(f"{OUT}/armenia-areas.geojson", "w", encoding="utf-8") as fh:
    json.dump({"type": "FeatureCollection", "features": feats}, fh, ensure_ascii=False, separators=(",", ":"))

time.sleep(5)
q_places = '[out:json][timeout:120];area["ISO3166-1"="AM"]["admin_level"="2"]->.a;node(area.a)["place"~"^(city|town|village)$"];out;'
res = overpass(q_places)
places = [{"name": e["tags"].get("name"), "name_hy": e["tags"].get("name:hy"), "name_en": e["tags"].get("name:en"),
           "place": e["tags"].get("place"), "lat": round(e["lat"], 5), "lon": round(e["lon"], 5)} for e in res["elements"]]
print("places:", len(places), "cities/towns:", len([p for p in places if p["place"] in ("city", "town")]))
with open(f"{OUT}/osm-places.json", "w", encoding="utf-8") as fh:
    json.dump(places, fh, ensure_ascii=False)
