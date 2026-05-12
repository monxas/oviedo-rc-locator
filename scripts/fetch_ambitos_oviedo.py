"""Cachea polígonos UTM de los ámbitos PGOU de Oviedo (UG, AU, PE, AUS, PP, AA).

Output: ~/.cache/oviedo_rc/ambitos_oviedo.json
Schema: {etiqueta: {bbox_utm: [xmin,ymin,xmax,ymax], polygon_utm: [...], categoria}}

Se usa en ficha_plano.py para proyectar polígonos catastrales sobre el plano de
la ficha del ámbito (página 1 del PDF). El centroide del polígono = anchor en
el centro del body cartográfico del plano.
"""
import json
import urllib.request
from pathlib import Path

WFS = "http://visorrpgur.asturias.es:8090/geoserver/E79_ENTIDADES_URBANISTICAS/wfs"
ID_MUNICIPIO = 33044

LAYERS = [
    ("n15_UNIDADES_GESTION",          "Etiqueta", "UG"),
    ("n22_INSTRUMENTOS_PLANEAMIENTO", "Etiqueta", "INSTR"),
    ("n25_AREAS_MODIF_URBANISTICAS",  "Etiqueta", "MODIF"),
]

CACHE = Path.home() / ".cache" / "oviedo_rc" / "ambitos_oviedo.json"


def walk_coords(coords):
    if isinstance(coords, list) and coords and isinstance(coords[0], (int, float)) and len(coords) == 2:
        yield coords
    elif isinstance(coords, list):
        for c in coords:
            yield from walk_coords(c)


def fetch_layer(layer_name):
    url = (f"{WFS}?service=WFS&version=2.0.0&request=GetFeature"
           f"&typeNames={layer_name}&srsName=EPSG:25830&outputFormat=application/json"
           f"&CQL_FILTER=id_municipio={ID_MUNICIPIO}&count=1000")
    print(f"  fetching {layer_name}…", flush=True)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    return json.loads(urllib.request.urlopen(req, timeout=60).read())


def main():
    out = {}
    for layer, label_field, cat in LAYERS:
        data = fetch_layer(layer)
        feats = data.get("features", [])
        print(f"    {layer}: {len(feats)} features", flush=True)
        for f in feats:
            props = f.get("properties", {})
            g = f.get("geometry")
            if not g or not g.get("coordinates"):
                continue
            etiqueta = (props.get(label_field) or props.get("Nombre_del_Area")
                        or props.get("Denominación_Instrumento") or "").strip()
            if not etiqueta:
                continue
            pts = list(walk_coords(g["coordinates"]))
            if not pts:
                continue
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            cx = sum(xs) / len(xs)
            cy = sum(ys) / len(ys)
            out[etiqueta] = {
                "categoria": cat,
                "layer": layer,
                "bbox_utm": [min(xs), min(ys), max(xs), max(ys)],
                "centroid_utm": [round(cx, 2), round(cy, 2)],
                "n_points": len(pts),
            }
    CACHE.write_text(json.dumps(out, ensure_ascii=False, indent=2))
    print(f"\nTotal ámbitos cacheados: {len(out)}")
    print(f"  por categoría:")
    from collections import Counter
    print(f"    {dict(Counter(v['categoria'] for v in out.values()))}")
    print(f"Salida: {CACHE}  ({CACHE.stat().st_size//1024} KB)")


if __name__ == "__main__":
    main()
