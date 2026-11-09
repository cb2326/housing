"""Trace Yerevan's 12 valuation zones from the official map (Government decision N 1023-N of 4 June 2020, Annex 3,
sheet 1) into GeoJSON, split by administrative district.

1. Georeference: fit the map's city outline to OpenStreetMap's Yerevan boundary (Armenian national grid, ARM-TM).
2. Classify each 3x3-pixel cell by the nearest legend color (majority vote; thin building/parcel lines are ignored).
3. Split cells by district (OSM district boundaries rasterized onto the same grid) and trace each (zone, district)
   area into polygons; simplify slightly.
Usage: trace_zones.py <map.png> <areas.geojson> <out.geojson>"""
import json, math, sys
from collections import Counter, defaultdict
sys.path.insert(0, r"C:\Users\khach\AppData\Local\Temp\claude\e6c96571643dedad\094649a6-d991-4ae6-a956-29f4fd98a5c8\scratchpad\lib")
sys.path.insert(0, r"C:\Users\khach\AppData\Local\Temp\claude\e6c96571643dedad\94649a6")
sys.stdout.reconfigure(encoding="utf-8")
from PIL import Image, ImageDraw
import tm

IMG, AREAS, OUT = sys.argv[1], sys.argv[2], sys.argv[3]
LEGEND = {1: (255, 247, 114), 2: (25, 103, 186), 3: (242, 99, 24), 4: (163, 219, 128), 5: (244, 116, 170), 6: (120, 154, 215),
          7: (102, 99, 100), 8: (219, 30, 38), 9: (172, 18, 132), 10: (69, 136, 58), 11: (146, 96, 79), 12: (255, 251, 190)}
DIST_IDS = {13404299: 'ajapnyak', 13404250: 'avan', 13404297: 'arabkir', 13404298: 'davtashen', 13404216: 'erebuni', 13404218: 'kentron',
            13404219: 'malatia-sebastia', 13404220: 'nor-nork', 13404217: 'nork-marash', 13404214: 'nubarashen', 13404215: 'shengavit',
            13404296: 'kanaker-zeytun'}

im = Image.open(IMG).convert("RGB")
W, H = im.size
px = im.load()

def excluded(x, y):
    # title, legend, frame, and the separate airport area drawn at the left
    return y < 130 or x < 12 or y < 12 or x > W - 12 or y > H - 12 or (x < 330 and y > 1370) or (x < 445 and 730 < y < 900)

# ---- 1. mask of the city on the map
mask = Image.new("1", (W, H), 0)
mp = mask.load()
for y in range(H):
    for x in range(W):
        if excluded(x, y):
            continue
        r, g, b = px[x, y]
        if r + g + b < 740:   # anything not white (zone 12 cream is 696)
            mp[x, y] = 1
xs = [x for y in range(0, H, 2) for x in range(0, W, 2) if mp[x, y]]
ys = [y for y in range(0, H, 2) for x in range(0, W, 2) if mp[x, y]]
mx0, mx1, my0, my1 = min(xs), max(xs), min(ys), max(ys)
print("map city bbox (px):", mx0, mx1, my0, my1)

areas = json.load(open(AREAS, encoding="utf-8"))
yer = next(f for f in areas["features"] if f["properties"]["osm_id"] == 364087)
polys = yer["geometry"]["coordinates"]
main = max(polys, key=lambda p: len(p[0]))[0]          # the city itself (not an exclave)
main_tm = [tm.forward(lon, lat) for lon, lat in main]
X0, X1 = min(p[0] for p in main_tm), max(p[0] for p in main_tm)
Y0, Y1 = min(p[1] for p in main_tm), max(p[1] for p in main_tm)
print("OSM Yerevan bbox (m): %.0f x %.0f" % (X1 - X0, Y1 - Y0))

# pixel -> grid: X = X0 + (x - mx0) * sx ; Y = Y1 - (y - my0) * sy
def make(sx, sy, ox, oy):
    to_px = lambda X, Y: ((X - X0 - ox) / sx + mx0, (Y1 + oy - Y) / sy + my0)
    to_xy = lambda x, y: (X0 + ox + (x - mx0) * sx, Y1 + oy - (y - my0) * sy)
    return to_px, to_xy

def iou(params, step=3):
    to_px, _ = make(*params)
    osm = Image.new("1", (W, H), 0)
    ImageDraw.Draw(osm).polygon([to_px(*p) for p in main_tm], fill=1)
    op = osm.load()
    inter = union = 0
    for y in range(130, H - 12, step):
        for x in range(12, W - 12, step):
            if excluded(x, y):
                continue
            a, b = mp[x, y], op[x, y]
            inter += a and b
            union += a or b
    return inter / union

sx0, sy0 = (X1 - X0) / (mx1 - mx0), (Y1 - Y0) / (my1 - my0)
best = (sx0, sy0, 0.0, 0.0)
bv = iou(best)
print("start fit: %.4f  (%.2f m/px x, %.2f m/px y)" % (bv, sx0, sy0))
for rnd_, (ds, dm) in enumerate(((0.02, 120), (0.008, 50), (0.003, 20), (0.001, 8))):
    improved = True
    while improved:
        improved = False
        for i, d in ((0, ds * best[0]), (1, ds * best[1]), (2, dm), (3, dm)):
            for sgn in (-1, 1):
                cand = list(best)
                cand[i] += sgn * d
                v = iou(tuple(cand))
                if v > bv:
                    best, bv, improved = tuple(cand), v, True
    print(f"  refine {rnd_}: overlap {bv:.4f}  scale {best[0]:.2f}/{best[1]:.2f} m/px  offset {best[2]:.0f},{best[3]:.0f} m")
to_px, to_xy = make(*best)

# ---- 2. zone of each 3x3 cell
C = 3
TOL = int(sys.argv[4]) if len(sys.argv) > 4 else 14
GW, GH = W // C, H // C
zone = [[0] * GW for _ in range(GH)]
cols = list(LEGEND.items())
# votes per cell and zone. Building outlines are drawn in (almost) zone 2's blue, so zone 2 only counts its exact fill.
V = [[[0] * GW for _ in range(GH)] for _ in range(13)]
for gy in range(GH):
    for gx in range(GW):
        x0, y0 = gx * C, gy * C
        if excluded(x0 + 1, y0 + 1):
            continue
        for y in range(y0, y0 + C):
            for x in range(x0, x0 + C):
                r, g, b = px[x, y]
                best_z, best_d = None, TOL ** 2 * 3   # only (nearly) exact fill colors: lines drawn on top are ignored
                for z, (R, G, B) in cols:
                    d = (r - R) ** 2 + (g - G) ** 2 + (b - B) ** 2
                    if d < best_d:
                        best_z, best_d = z, d
                if best_z == 2 and best_d > 3:
                    best_z = None
                if best_z:
                    V[best_z][gy][gx] += 1
# each cell takes the zone with the most votes in the surrounding (2R+1)x(2R+1) cells (summed-area tables)
R = int(sys.argv[5]) if len(sys.argv) > 5 else 2
S = []
for z in range(13):
    t = [[0] * (GW + 1) for _ in range(GH + 1)]
    vz = V[z]
    for gy in range(GH):
        run = 0
        for gx in range(GW):
            run += vz[gy][gx]
            t[gy + 1][gx + 1] = t[gy][gx + 1] + run
    S.append(t)
for gy in range(GH):
    y0, y1 = max(0, gy - R), min(GH, gy + R + 1)
    for gx in range(GW):
        if excluded(gx * C + 1, gy * C + 1):
            continue
        x0, x1 = max(0, gx - R), min(GW, gx + R + 1)
        best_z, best_v = 0, 0
        for z in range(1, 13):
            t = S[z]
            v = t[y1][x1] - t[y0][x1] - t[y1][x0] + t[y0][x0]
            if v > best_v:
                best_z, best_v = z, v
        zone[gy][gx] = best_z
# fill cells covered by lines/labels from their neighbors (inside the city only)
inside = [[bool(mp[min(W - 1, gx * C + 1), min(H - 1, gy * C + 1)]) for gx in range(GW)] for gy in range(GH)]
for _ in range(25):
    changed = 0
    new = [row[:] for row in zone]
    for gy in range(1, GH - 1):
        for gx in range(1, GW - 1):
            if zone[gy][gx] or not inside[gy][gx]:
                continue
            nb = Counter(zone[gy + dy][gx + dx] for dy in (-1, 0, 1) for dx in (-1, 0, 1) if zone[gy + dy][gx + dx])
            if nb:
                new[gy][gx] = nb.most_common(1)[0][0]
                changed += 1
    zone = new
    if not changed:
        break
# smooth single stray cells (majority filter)
new = [row[:] for row in zone]
for gy in range(1, GH - 1):
    for gx in range(1, GW - 1):
        z = zone[gy][gx]
        if not z:
            continue
        nb = Counter(zone[gy + dy][gx + dx] for dy in (-1, 0, 1) for dx in (-1, 0, 1) if (dy or dx) and zone[gy + dy][gx + dx])
        if nb and nb.get(z, 0) <= 1:
            new[gy][gx] = nb.most_common(1)[0][0]
zone = new

# ---- 3. district of each cell (OSM districts drawn on the same grid)
dimg = Image.new("L", (GW, GH), 0)
dd = ImageDraw.Draw(dimg)
dnames = {}
for k, f in enumerate([f for f in areas["features"] if f["properties"]["kind"] == "district"], 1):
    dnames[k] = DIST_IDS[f["properties"]["osm_id"]]
    for poly in f["geometry"]["coordinates"]:
        ring = [to_px(*tm.forward(lon, lat)) for lon, lat in poly[0]]
        dd.polygon([(x / C, y / C) for x, y in ring], fill=k)
dpx = dimg.load()
label = [[0] * GW for _ in range(GH)]
keys = {}
for gy in range(GH):
    for gx in range(GW):
        z, d = zone[gy][gx], dpx[gx, gy]
        if z and d:
            label[gy][gx] = keys.setdefault((z, d), len(keys) + 1)

# ---- trace each label's outline into rings (cell corners), then simplify
def rings_of(lid):
    edges = {}
    for gy in range(GH):
        row = label[gy]
        for gx in range(GW):
            if row[gx] != lid:
                continue
            up = gy > 0 and label[gy - 1][gx] == lid
            dn = gy < GH - 1 and label[gy + 1][gx] == lid
            lf = gx > 0 and row[gx - 1] == lid
            rt = gx < GW - 1 and row[gx + 1] == lid
            if not up: edges[(gx, gy)] = (gx + 1, gy)
            if not rt: edges[(gx + 1, gy)] = (gx + 1, gy + 1)
            if not dn: edges[(gx + 1, gy + 1)] = (gx, gy + 1)
            if not lf: edges[(gx, gy + 1)] = (gx, gy)
    rings = []
    while edges:
        start, nxt = edges.popitem()
        ring = [start, nxt]
        while nxt != start and nxt in edges:
            nxt = edges.pop(nxt)
            ring.append(nxt)
        if len(ring) > 4:
            rings.append(ring)
    return rings

def dp(points, eps):
    if len(points) < 3:
        return points
    (ax, ay), (bx, by) = points[0], points[-1]
    dx, dy = bx - ax, by - ay
    nrm = math.hypot(dx, dy) or 1e-9
    i, dmax = 0, -1
    for k in range(1, len(points) - 1):
        x, y = points[k]
        d = abs(dy * x - dx * y + bx * ay - by * ax) / nrm if nrm > 1e-9 else math.hypot(x - ax, y - ay)
        if d > dmax:
            i, dmax = k, d
    if dmax > eps:
        return dp(points[:i + 1], eps)[:-1] + dp(points[i:], eps)
    return [points[0], points[-1]]

def simplify(ring, eps=1.2):
    far = max(range(len(ring)), key=lambda k: (ring[k][0] - ring[0][0]) ** 2 + (ring[k][1] - ring[0][1]) ** 2)
    s = dp(ring[:far + 1], eps)[:-1] + dp(ring[far:], eps)
    return s if len(s) >= 4 else ring

def area2(r):
    return sum(r[i][0] * r[i + 1][1] - r[i + 1][0] * r[i][1] for i in range(len(r) - 1))

def inside_ring(pt, ring):
    x, y = pt; c = False
    for i in range(len(ring) - 1):
        (x1, y1), (x2, y2) = ring[i], ring[i + 1]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            c = not c
    return c

feats, cells = [], Counter(v for row in label for v in row if v)
for (z, d), lid in keys.items():
    if cells[lid] < 12:          # tiny specks (under ~0.02 km2) are left out
        continue
    rings = [simplify(r) for r in rings_of(lid)]
    outers = [r for r in rings if area2(r) > 0 and abs(area2(r)) >= 16]
    holes = [r for r in rings if area2(r) < 0 and abs(area2(r)) >= 16]
    polys = [[o] for o in sorted(outers, key=lambda r: -abs(area2(r)))]
    for h in holes:
        for p in polys:
            if inside_ring(h[0], p[0]):
                p.append(h)
                break
    def to_ll(r):
        return [[round(v, 5) for v in tm.inverse(*to_xy(x * C, y * C))] for x, y in r]
    feats.append({"type": "Feature", "properties": {"zone": z, "district": dnames[d], "cells": cells[lid]},
                  "geometry": {"type": "MultiPolygon", "coordinates": [[to_ll(r) for r in p] for p in polys]}})
json.dump({"type": "FeatureCollection", "features": feats}, open(OUT, "w", encoding="utf-8"), separators=(",", ":"))
print(f"wrote {len(feats)} zone pieces; size {len(json.dumps(feats)) // 1024} KB")

# ---- checks: where zone 1 sits, and cell share of each zone by district
z1 = [(gx, gy) for gy in range(GH) for gx in range(GW) if zone[gy][gx] == 1]
cx, cy = sum(p[0] for p in z1) / len(z1) * C, sum(p[1] for p in z1) / len(z1) * C
print("zone 1 center:", tm.inverse(*to_xy(cx, cy)), "(Republic Square is 44.5126, 40.1777; Opera 44.5155, 40.1863)")
by_d = defaultdict(Counter)
for (z, d), lid in keys.items():
    by_d[dnames[d]][z] += cells[lid]
for dn, cnt in sorted(by_d.items()):
    tot = sum(cnt.values())
    print(f"  {dn:17} " + " ".join(f"z{z}:{c * 100 // tot}%" for z, c in sorted(cnt.items()) if c * 100 // tot >= 3))
