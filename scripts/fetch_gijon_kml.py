"""Descarga + parse del KML del PGOU de Gijón.

Output: ~/.cache/oviedo_rc/gijon/ambitos.json
Schema: [{id, name, categoria, ficha_url, polygons_utm: [[(x,y), ...], ...]}]

KML viene en EPSG:4326 (WGS84 lon/lat). Convertido a EPSG:25830 (ETRS89 UTM 30N)
para alinear con el resto del stack (catastro/Oviedo).
"""
import json
import re
import urllib.request
from pathlib import Path

from pyproj import Transformer

KML_URL = "https://documentos.gijon.es/PGO/pgo.kml"
CACHE = Path.home() / ".cache" / "oviedo_rc" / "gijon"
CACHE.mkdir(parents=True, exist_ok=True)
KML_PATH = CACHE / "pgo.kml"
OUT = CACHE / "ambitos.json"

PLACEMARK_RE = re.compile(
    r"<Placemark>\s*"
    r"<name>([^<]+)</name>\s*"
    r"<description>(.*?)</description>.*?"
    r"(<MultiGeometry>.*?</MultiGeometry>|<Polygon>.*?</Polygon>)\s*"
    r"</Placemark>",
    re.S,
)
POLYGON_RE = re.compile(r"<Polygon>.*?<coordinates>\s*(.*?)\s*</coordinates>", re.S)
FICHA_ID_RE = re.compile(r"id=([A-Z0-9 _-]+?)(?:[>\"]|$)", re.I)


def parse_polygons(geom_blob: str) -> list[list[tuple[float, float]]]:
    polys = []
    for m in POLYGON_RE.finditer(geom_blob):
        raw = m.group(1)
        pts = []
        for tok in raw.split():
            parts = tok.split(",")
            if len(parts) >= 2:
                try:
                    pts.append((float(parts[0]), float(parts[1])))
                except ValueError:
                    continue
        if len(pts) >= 3:
            polys.append(pts)
    return polys


def fetch_kml():
    if KML_PATH.exists() and KML_PATH.stat().st_size > 1_000_000:
        print(f"  cache hit: {KML_PATH} ({KML_PATH.stat().st_size//1024} KB)")
        return KML_PATH.read_text(encoding="utf-8")
    print(f"  download {KML_URL}…")
    req = urllib.request.Request(KML_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read().decode("utf-8")
    KML_PATH.write_text(data, encoding="utf-8")
    print(f"  saved {len(data)//1024} KB")
    return data


def main():
    print("Cargando KML Gijón…")
    txt = fetch_kml()
    print("Parseando placemarks…")
    transformer = Transformer.from_crs("EPSG:4326", "EPSG:25830", always_xy=True)

    ambitos = []
    for m in PLACEMARK_RE.finditer(txt):
        name = m.group(1).strip()
        desc = m.group(2)
        geom = m.group(3)
        # ficha id: prefer the link inside description; fallback to name
        fm = FICHA_ID_RE.search(desc)
        ficha_id = (fm.group(1).strip() if fm else name).replace(" ", "_")
        ficha_url = f"https://documentos.gijon.es/PGO/ficha.php?id={ficha_id}"
        categoria = name.split("-", 1)[0]

        polys_4326 = parse_polygons(geom)
        polys_utm = []
        for poly in polys_4326:
            utm_pts = [transformer.transform(lon, lat) for lon, lat in poly]
            polys_utm.append([(round(x, 2), round(y, 2)) for x, y in utm_pts])
        if not polys_utm:
            continue
        ambitos.append({
            "id": name,
            "ficha_id": ficha_id,
            "categoria": categoria,
            "ficha_url": ficha_url,
            "polygons_utm": polys_utm,
        })

    print(f"  ámbitos parseados: {len(ambitos)}")
    from collections import Counter
    cats = Counter(a["categoria"] for a in ambitos)
    print(f"  categorías: {dict(cats.most_common())}")

    OUT.write_text(json.dumps(ambitos, ensure_ascii=False, separators=(",", ":")))
    print(f"  salida: {OUT} ({OUT.stat().st_size//1024} KB)")


if __name__ == "__main__":
    main()
