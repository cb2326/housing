"""One-time: build armenia/static/places.json (districts, marzes, towns with Armenian + English names and positions)."""
import json, sys
sys.stdout.reconfigure(encoding="utf-8")
geo_dir, out_dir = sys.argv[1], sys.argv[2]
areas = json.load(open(f"{geo_dir}/armenia-areas.geojson", encoding="utf-8"))
osm = json.load(open(f"{geo_dir}/osm-places.json", encoding="utf-8"))

# id, Armenian name as written in the Cadastre reports, English name, OSM relation id
DISTRICTS = [
    ("ajapnyak", "Աջափնյակ", "Ajapnyak", 13404299), ("avan", "Ավան", "Avan", 13404250),
    ("arabkir", "Արաբկիր", "Arabkir", 13404297), ("davtashen", "Դավթաշեն", "Davtashen", 13404298),
    ("erebuni", "Էրեբունի", "Erebuni", 13404216), ("kentron", "Կենտրոն", "Kentron", 13404218),
    ("malatia-sebastia", "Մալաթիա-Սեբաստիա", "Malatia-Sebastia", 13404219), ("nor-nork", "Նոր Նորք", "Nor Nork", 13404220),
    ("nork-marash", "Նորք-Մարաշ", "Nork-Marash", 13404217), ("nubarashen", "Նուբարաշեն", "Nubarashen", 13404214),
    ("shengavit", "Շենգավիթ", "Shengavit", 13404215), ("kanaker-zeytun", "Քանաքեռ-Զեյթուն", "Kanaker-Zeytun", 13404296),
]
# id, Armenian, English, OSM relation id
MARZES = [
    ("yerevan", "Երևան", "Yerevan", 364087), ("aragatsotn", "Արագածոտն", "Aragatsotn", 3917110),
    ("ararat", "Արարատ", "Ararat", 364083), ("armavir", "Արմավիր", "Armavir", 364086),
    ("gegharkunik", "Գեղարքունիք", "Gegharkunik", 364080), ("lori", "Լոռի", "Lori", 364078),
    ("kotayk", "Կոտայք", "Kotayk", 364084), ("shirak", "Շիրակ", "Shirak", 364077),
    ("syunik", "Սյունիք", "Syunik", 364082), ("vayots-dzor", "Վայոց ձոր", "Vayots Dzor", 364081),
    ("tavush", "Տավուշ", "Tavush", 364079),
]
# marz id -> towns (Armenian as in the Cadastre table 3.6-4, English)
TOWNS = {
    "aragatsotn": [("Աշտարակ", "Ashtarak"), ("Ապարան", "Aparan"), ("Թալին", "Talin")],
    "ararat": [("Արտաշատ", "Artashat"), ("Մասիս", "Masis"), ("Վեդի", "Vedi"), ("Արարատ", "Ararat")],
    "armavir": [("Վաղարշապատ", "Vagharshapat (Etchmiadzin)"), ("Արմավիր", "Armavir"), ("Մեծամոր", "Metsamor")],
    "gegharkunik": [("Սևան", "Sevan"), ("Գավառ", "Gavar"), ("Մարտունի", "Martuni"), ("Վարդենիս", "Vardenis"), ("Ճամբարակ", "Chambarak")],
    "lori": [("Վանաձոր", "Vanadzor"), ("Ստեփանավան", "Stepanavan"), ("Սպիտակ", "Spitak"), ("Ալավերդի", "Alaverdi"), ("Տաշիր", "Tashir"), ("Թումանյան", "Tumanyan"), ("Ախթալա", "Akhtala")],
    "kotayk": [("Աբովյան", "Abovyan"), ("Նոր Հաճն", "Nor Hachn"), ("Բյուրեղավան", "Byureghavan"), ("Եղվարդ", "Yeghvard"), ("Հրազդան", "Hrazdan"), ("Ծաղկաձոր", "Tsaghkadzor"), ("Չարենցավան", "Charentsavan")],
    "shirak": [("Գյումրի", "Gyumri"), ("Արթիկ", "Artik"), ("Մարալիկ", "Maralik")],
    "syunik": [("Գորիս", "Goris"), ("Կապան", "Kapan"), ("Սիսիան", "Sisian"), ("Մեղրի", "Meghri"), ("Քաջարան", "Kajaran")],
    "vayots-dzor": [("Վայք", "Vayk"), ("Եղեգնաձոր", "Yeghegnadzor"), ("Ջերմուկ", "Jermuk")],
    "tavush": [("Դիլիջան", "Dilijan"), ("Իջևան", "Ijevan"), ("Բերդ", "Berd"), ("Նոյեմբերյան", "Noyemberyan"), ("Այրում", "Ayrum")],
}

def inside(pt, ring):
    x, y = pt; c = False
    for i in range(len(ring) - 1):
        (x1, y1), (x2, y2) = ring[i], ring[i + 1]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            c = not c
    return c

marz_geom = {f["properties"]["osm_id"]: f["geometry"]["coordinates"] for f in areas["features"]}
def in_marz(lon, lat, osm_id):
    return any(inside((lon, lat), poly[0]) for poly in marz_geom[osm_id])

def norm(s):
    return (s or "").replace("և", "եւ").strip()

out = {"districts": [], "marzes": [], "towns": []}
for i, hy, en, oid in DISTRICTS:
    out["districts"].append({"id": i, "hy": hy, "en": en, "osm_id": oid})
for i, hy, en, oid in MARZES:
    out["marzes"].append({"id": i, "hy": hy, "en": en, "osm_id": oid})
marz_osm = {m[0]: m[3] for m in MARZES}
rank = {"city": 0, "town": 1, "village": 2}
missing = []
for marz, towns in TOWNS.items():
    for hy, en in towns:
        cands = [p for p in osm if norm(hy) in (norm(p["name"]), norm(p["name_hy"]))]
        cands = [p for p in cands if in_marz(p["lon"], p["lat"], marz_osm[marz])] or cands
        cands.sort(key=lambda p: rank.get(p["place"], 9))
        MANUAL = {"Tsaghkadzor": {"lat": 40.5326, "lon": 44.7203, "place": "town"}}
        if not cands and en in MANUAL:
            cands = [MANUAL[en]]
        if not cands:
            missing.append((marz, hy, en)); continue
        p = cands[0]
        slug = en.split(" (")[0].lower().replace(" ", "-")
        out["towns"].append({"id": slug, "hy": hy, "en": en, "marz": marz, "lat": p["lat"], "lon": p["lon"], "osm_place": p["place"]})
        print(f"{en:28} {p['place']:8} {p['lat']:.4f},{p['lon']:.4f}  ({len(cands)} candidates)")
print("towns:", len(out["towns"]), "missing:", missing)
json.dump(out, open(f"{out_dir}/places.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
