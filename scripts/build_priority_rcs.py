"""Pre-computa la lista de RC14 que caen dentro del polígono real de algún
ámbito PGOU con ficha PDF asociada. Lo guarda en
`~/.cache/oviedo_rc/priority_rcs_fichas.json` (lista de RC14).

Esto refina la priorización del validator: en lugar de bbox (que tiene
falsos positivos por la forma irregular de los ámbitos), usa point-in-polygon.

Re-ejecutar cuando:
  - cambien los polígonos WFS (descargas nuevas de PGOU)
  - se añadan/quiten fichas PDF
"""
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path.home() / "oviedo-rc-locator"
sys.path.insert(0, str(ROOT / "src"))

from oviedo_rc import ficha_plano as fp
from oviedo_rc.config import COORDS_FILE

CACHE = Path.home() / ".cache" / "oviedo_rc"
OUT = CACHE / "priority_rcs_fichas.json"

WFS = "http://visorrpgur.asturias.es:8090/geoserver/E79_ENTIDADES_URBANISTICAS/wfs"
ID_MUNICIPIO = 33044
LAYERS = ["n15_UNIDADES_GESTION", "n22_INSTRUMENTOS_PLANEAMIENTO", "n25_AREAS_MODIF_URBANISTICAS"]


PAGE_SIZE = 1000


def fetch_layer(layer_name):
    """Pagina WFS con startIndex hasta exhausto.

    Sin paginar, count=1000 truncaba silenciosamente cualquier layer con más
    features (p.ej. n22_INSTRUMENTOS_PLANEAMIENTO en municipios grandes).
    """
    all_features = []
    start = 0
    while True:
        url = (f"{WFS}?service=WFS&version=2.0.0&request=GetFeature"
               f"&typeNames={layer_name}&srsName=EPSG:25830&outputFormat=application/json"
               f"&CQL_FILTER=id_municipio={ID_MUNICIPIO}"
               f"&count={PAGE_SIZE}&startIndex={start}")
        print(f"  fetching {layer_name} (start={start})…", flush=True)
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        data = json.loads(urllib.request.urlopen(req, timeout=60).read())
        feats = data.get("features", [])
        if not feats:
            break
        all_features.extend(feats)
        # numberMatched/numberReturned son strings en GeoJSON-WFS — usar tamaño real
        if len(feats) < PAGE_SIZE:
            break
        start += PAGE_SIZE
    return {"features": all_features}


def point_in_ring(x, y, ring):
    """Ray-casting (par/impar). ring = lista de [x,y]."""
    n = len(ring)
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi):
            inside = not inside
        j = i
    return inside


def extract_polygons(geom):
    """Devuelve lista de outer-rings (excluye huecos). Soporta Polygon/MultiPolygon."""
    if not geom: return []
    typ = geom.get("type")
    coords = geom.get("coordinates")
    if typ == "Polygon":
        return [coords[0]] if coords else []
    if typ == "MultiPolygon":
        return [poly[0] for poly in coords if poly]
    return []


def main():
    # 1) Etiquetas con ficha PDF asociada
    print("Detectando ámbitos con ficha PDF…", flush=True)
    fichas_dir = CACHE / "fichas"
    etiquetas_con_pdf = set()
    for pdf in fichas_dir.glob("*.pdf"):
        m = fp._match_ambito_for_filename(pdf.name)
        if m and m.get("etiqueta"):
            etiquetas_con_pdf.add(m["etiqueta"])
    print(f"  ámbitos con PDF: {len(etiquetas_con_pdf)}", flush=True)

    # 2) Descarga polígonos WFS de los layers de interés
    polygons = []  # lista de (etiqueta, outer_ring)
    for layer in LAYERS:
        data = fetch_layer(layer)
        for feat in data.get("features", []):
            props = feat.get("properties", {})
            et = (props.get("Etiqueta") or props.get("Nombre_del_Area")
                  or props.get("Denominación_Instrumento") or "").strip()
            if not et or et not in etiquetas_con_pdf:
                continue
            for ring in extract_polygons(feat.get("geometry")):
                polygons.append((et, ring))
    print(f"polígonos efectivos: {len(polygons)}", flush=True)

    # 3) Carga COORDS y filtra PIP
    coords = json.loads((CACHE / COORDS_FILE.name).read_text(encoding="utf-8"))
    print(f"coords totales: {len(coords)}", flush=True)

    # Bboxes para skip rápido
    bboxes = []
    for et, ring in polygons:
        xs = [p[0] for p in ring]; ys = [p[1] for p in ring]
        bboxes.append((et, ring, min(xs), min(ys), max(xs), max(ys)))

    out = set()
    for k, rec in coords.items():
        x, y = (rec.get("x"), rec.get("y")) if isinstance(rec, dict) else (rec[0], rec[1])
        if x is None or y is None: continue
        for et, ring, xmin, ymin, xmax, ymax in bboxes:
            if not (xmin <= x <= xmax and ymin <= y <= ymax): continue
            if point_in_ring(x, y, ring):
                out.add(k)
                break

    print(f"\nRC14 dentro de polígono real con ficha: {len(out)}")
    OUT.write_text(json.dumps(sorted(out), ensure_ascii=False))
    print(f"Salida: {OUT} ({OUT.stat().st_size//1024} KB)")


if __name__ == "__main__":
    main()
