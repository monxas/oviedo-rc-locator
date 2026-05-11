"""Consulta WFS GeoServer Principado de Asturias para info de planeamiento PGOU.

Fuente: `http://visorrpgur.asturias.es:8090/geoserver/E79_ENTIDADES_URBANISTICAS/wfs`
CRS: EPSG:25830 (ETRS89 UTM 30N).

Capas consultadas por coord UTM:
  n15_UNIDADES_GESTION         → UG con uso/edificabilidad
  n25_AREAS_MODIF_URBANISTICAS → Modificaciones puntuales
  n22_INSTRUMENTOS_PLANEAMIENTO → Plan Parcial, Plan Especial, etc
  n12_USOS_PORMENORIZADOS      → ordenanza/uso aplicable (sin UG)
  n06_NUCLEOS_RURALES          → núcleo rural (SNU)
  n07_SIST_GENERALES_AREAS     → sistemas generales (zonas verdes, dotacionales)

Filtrado por id_municipio=33044 (Oviedo según INE).
"""
from __future__ import annotations

import json
from urllib.parse import urlencode

from .http_utils import http_get
from . import fichas as fichas_mod

WFS_URL = "http://visorrpgur.asturias.es:8090/geoserver/E79_ENTIDADES_URBANISTICAS/wfs"
SRS = "EPSG:25830"
ID_MUNICIPIO_OVIEDO = 33044

# Orden de consulta (capa, props_principales)
LAYERS = [
    ("n15_UNIDADES_GESTION",         ["Nombre_del_Area", "Etiqueta", "Uso_predominante",
                                       "Edificabilidad_(m.2/m.2)", "Densidad_(Viv./Ha.)",
                                       "Sistema_de_Actuación", "Área_(m.2)"]),
    ("n25_AREAS_MODIF_URBANISTICAS", ["Etiqueta", "Nombre_del_Area"]),
    ("n22_INSTRUMENTOS_PLANEAMIENTO", ["Denominación_Instrumento", "Cód._Tipo_Instrumento",
                                        "Etiqueta", "Instrumento"]),
    ("n12_USOS_PORMENORIZADOS",      ["Nombre_de_Ordenanza", "Uso_Predominante",
                                       "Edificabilidad_(m.2/m.2)", "Etiqueta"]),
    ("n06_NUCLEOS_RURALES",          ["Nombre_Oficial", "Subcategoria_de_SNU", "Etiqueta"]),
    ("n07_SIST_GENERALES_AREAS",     ["Etiqueta"]),
]

# Capas de patrimonio/afecciones: nunca son "ámbito"; sólo info adicional
PATRIMONIO_LAYERS = [
    "n23_ELEM_CATALOGADOS_AREAS",
    "n27_BICS",
]


def _wfs_at_point(layer: str, x: float, y: float, props: list[str],
                   bbox_tolerance: float = 0.0) -> list[dict]:
    """WFS GetFeature al rededor de (x,y).
    bbox_tolerance=0 → INTERSECTS estricto. >0 → BBOX (afecciones cercanas)."""
    if bbox_tolerance > 0:
        t = bbox_tolerance
        cql = (
            f"id_municipio={ID_MUNICIPIO_OVIEDO} "
            f"AND BBOX(GEOMETRY,{x-t},{y-t},{x+t},{y+t})"
        )
    else:
        cql = (
            f"id_municipio={ID_MUNICIPIO_OVIEDO} "
            f"AND INTERSECTS(GEOMETRY,POINT({x} {y}))"
        )
    params = {
        "service": "WFS",
        "version": "2.0.0",
        "request": "GetFeature",
        "typeNames": layer,
        "srsName": SRS,
        "outputFormat": "application/json",
        "count": 10,
        "CQL_FILTER": cql,
    }
    # Omitir propertyName: caracteres acentuados/paréntesis lo rompen y
    # los payloads son pequeños sin la geometría (null cuando no se filtra).
    url = f"{WFS_URL}?{urlencode(params)}"
    try:
        r = http_get(url, timeout=15)
        data = json.loads(r.text)
        return [f.get("properties", {}) for f in data.get("features", [])]
    except Exception:
        return []


def _normalize(s: str | None) -> str:
    if not s:
        return ""
    return s.upper().replace("-", " ").replace("_", " ").replace("Ñ", "N")


def _find_matching_ficha(ambito_name: str) -> list[dict]:
    """Heurística: busca ficha cuyo nombre comparta tokens con el ámbito."""
    if not ambito_name:
        return []
    listing = fichas_mod.load_listing()
    raw_tokens = [t for t in _normalize(ambito_name).split() if t]
    if not raw_tokens:
        return []
    word_tokens = [t for t in raw_tokens if len(t) > 2]
    num_tokens = [t for t in raw_tokens if t.isdigit()]
    if not word_tokens:
        return []
    hits = []
    for fname in listing:
        norm = " " + _normalize(fname.rsplit(".pdf", 1)[0]) + " "
        n_word = sum(1 for t in word_tokens if t in norm)
        if n_word < max(1, len(word_tokens) // 2):
            continue
        # Bonus por coincidencia exacta de número (separado por espacios o _)
        n_num = sum(1 for t in num_tokens if f" {t} " in norm)
        score = n_word + n_num * 5  # número exacto pesa más
        hits.append({"filename": fname, "score": score,
                     "tokens_matched": n_word, "num_matched": n_num})
    hits.sort(key=lambda h: -h["score"])
    return hits[:5]


def lookup(x: float, y: float) -> dict:
    """Para coords UTM (EPSG:25830) devuelve dict con todas las capas y matches.

    {
      "x": .., "y": ..,
      "layers": {<layer>: [<props>, ...]},
      "ug": [...] | None,            # principal UG si existe
      "fichas_match": [...],          # fichas que podrían corresponder
    }
    """
    out: dict = {"x": x, "y": y, "layers": {}}
    ug_props = None
    nombre_ambito = None

    for layer, props in LAYERS:
        feats = _wfs_at_point(layer, x, y, props)
        if feats:
            out["layers"][layer] = feats
            if layer == "n15_UNIDADES_GESTION" and not nombre_ambito:
                ug_props = feats[0]
                nombre_ambito = feats[0].get("Nombre_del_Area") or feats[0].get("Etiqueta")
            elif layer == "n25_AREAS_MODIF_URBANISTICAS" and not nombre_ambito:
                nombre_ambito = feats[0].get("Nombre_del_Area") or feats[0].get("Etiqueta")

    # Patrimonio (BICs, elementos catalogados) — info separada, no ámbito
    patrimonio = []
    for layer in PATRIMONIO_LAYERS:
        # 50m de tolerancia: afecciones cercanas también cuentan
        feats = _wfs_at_point(layer, x, y, [], bbox_tolerance=50.0)
        for f in feats:
            patrimonio.append({
                "tipo": layer,
                "nombre": (f.get("Nombre_del_BIC")
                            or f.get("Denominación_Elemento_Protegido")
                            or f.get("Etiqueta")),
                "nivel_proteccion": f.get("Nivel_de_Protección"),
                "tipo_patrimonio": f.get("Tipo_de_Patrimonio"),
                "etiqueta": f.get("Etiqueta"),
            })
    out["patrimonio"] = patrimonio

    out["ug"] = ug_props
    # Fallback: si no hay UG/Modif, intenta n22 (PE/PP), n06 (núcleo rural), n12 (ordenanza)
    if not nombre_ambito:
        for fallback in ("n22_INSTRUMENTOS_PLANEAMIENTO", "n06_NUCLEOS_RURALES",
                          "n12_USOS_PORMENORIZADOS"):
            feats = out["layers"].get(fallback)
            if feats:
                nombre_ambito = (feats[0].get("Denominación_Instrumento")
                                  or feats[0].get("Nombre_Oficial")
                                  or feats[0].get("Nombre_de_Ordenanza")
                                  or feats[0].get("Etiqueta"))
                if nombre_ambito:
                    break
    out["ambito"] = nombre_ambito
    # Sólo buscar ficha cuando el ámbito viene de UG/Modif/Instrumento (con código).
    # Ordenanza n12 ("EDIFICACION RESIDENCIAL CERRADA") no tiene ficha asociada.
    has_real_ambito = bool(
        out["layers"].get("n15_UNIDADES_GESTION")
        or out["layers"].get("n25_AREAS_MODIF_URBANISTICAS")
        or out["layers"].get("n22_INSTRUMENTOS_PLANEAMIENTO")
    )
    out["fichas_match"] = (_find_matching_ficha(nombre_ambito)
                            if (nombre_ambito and has_real_ambito) else [])
    return out
