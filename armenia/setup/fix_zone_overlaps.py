"""One-time clean-up of the traced valuation zones (armenia/static/yerevan_zones.geojson).

The tracing (trace_zones.py) kept only each zone's outer edge, so a ring-shaped zone (for example Kentron zone 2,
which wraps around zone 1) was stored as a solid shape that also covered the zone inside it. This script:
  1. cuts every smaller zone piece out of the bigger pieces it overlaps, within the same district, so pieces
     no longer overlap (the inner zone wins, which is how the official map reads);
  2. stores a label point for each piece ("lp": [lon, lat]): the point deepest inside its biggest part, so a
     label never lands in a ring's hole or outside a curved zone.

Needs shapely (pip install shapely); only used here, not by the monthly build.
Usage: python armenia/setup/fix_zone_overlaps.py
"""
import json
import pathlib
from collections import defaultdict

from shapely import make_valid
from shapely.geometry import mapping, shape
from shapely.ops import polylabel, unary_union

HERE = pathlib.Path(__file__).resolve().parent.parent
SRC = HERE / "static" / "yerevan_zones.geojson"
SITE = HERE.parent / "site" / "armenia" / "data" / "yerevan_zones.geojson"


def rounded(geom, nd=6):
    def r(c):
        return [r(x) for x in c] if isinstance(c[0], (list, tuple)) else [round(c[0], nd), round(c[1], nd)]
    g = mapping(geom)
    return {"type": g["type"], "coordinates": r(g["coordinates"])}


def main():
    data = json.loads(SRC.read_text(encoding="utf-8"))
    by_dist = defaultdict(list)
    for f in data["features"]:
        g = make_valid(shape(f["geometry"])) if f["geometry"]["coordinates"] else None
        if g is not None and not g.is_empty:
            by_dist[f["properties"]["district"]].append((f, g.buffer(0)))
    out, cut = [], 0
    for dist, items in by_dist.items():
        claimed = None
        for f, g in sorted(items, key=lambda x: x[1].area):    # smallest first: inner zones keep their area
            if claimed is not None and g.intersects(claimed):
                before = g.area
                g = g.difference(claimed).buffer(0)
                if before - g.area > before * 0.001:
                    cut += 1
            claimed = g if claimed is None else unary_union([claimed, g])
            polys = [p for p in getattr(g, "geoms", [g]) if p.geom_type == "Polygon" and p.area > 1e-9]
            if not polys:
                continue
            from shapely.geometry import MultiPolygon
            mp = MultiPolygon(polys)
            big = max(polys, key=lambda p: p.area)
            lp = polylabel(big, tolerance=0.00002)
            props = dict(f["properties"], lp=[round(lp.x, 6), round(lp.y, 6)])
            out.append({"type": "Feature", "properties": props, "geometry": rounded(mp)})
    data["features"] = sorted(out, key=lambda f: (f["properties"]["district"], int(f["properties"]["zone"])))
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    SRC.write_text(text, encoding="utf-8")
    if SITE.parent.exists():
        SITE.write_text(text, encoding="utf-8")
    print(f"{len(out)} zone pieces, {cut} had an inner zone cut out; wrote {SRC.name} ({len(text) // 1024} KB)")


if __name__ == "__main__":
    main()
