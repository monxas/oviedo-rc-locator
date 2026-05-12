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
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlencode

from .http_utils import http_get
from .concejo import OVIEDO, Concejo, get_concejo_for_utm
from . import fichas as fichas_mod

# Base WFS GeoServer Asturias (multi-concejo: workspace por concejo)
WFS_BASE = "http://visorrpgur.asturias.es:8090/geoserver"
SRS = "EPSG:25830"

# Backwards-compat (DEPRECATED — usar concejo.wfs_workspace / concejo.id_ine)
WFS_URL = f"{WFS_BASE}/{OVIEDO.wfs_workspace}/wfs"
ID_MUNICIPIO_OVIEDO = OVIEDO.id_ine


def _wfs_url_for(concejo: Concejo) -> str:
    return f"{WFS_BASE}/{concejo.wfs_workspace}/wfs"

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
                   bbox_tolerance: float = 0.0,
                   concejo: Concejo | None = None) -> list[dict]:
    """WFS GetFeature al rededor de (x,y).
    bbox_tolerance=0 → INTERSECTS estricto. >0 → BBOX (afecciones cercanas)."""
    c = concejo or OVIEDO
    if bbox_tolerance > 0:
        t = bbox_tolerance
        cql = (
            f"id_municipio={c.id_ine} "
            f"AND BBOX(GEOMETRY,{x-t},{y-t},{x+t},{y+t})"
        )
    else:
        cql = (
            f"id_municipio={c.id_ine} "
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
    url = f"{_wfs_url_for(c)}?{urlencode(params)}"
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


def _find_matching_ficha(ambito_name: str, etiqueta: str | None = None,
                          codigo_tipo: str | None = None) -> list[dict]:
    """Busca ficha cuyo nombre coincida con el ámbito.

    1) Si la etiqueta WFS (ej. "UG-RC4", "PE-3") trae un código corto que
       aparece en el filename de alguna ficha → match directo (alta confianza).
    2) Si no, fallback a tokens del nombre + número exacto."""
    if not ambito_name:
        return []
    listing = fichas_mod.load_listing()
    # Combina nombre + etiqueta WFS para más tokens (la etiqueta a veces
    # trae info adicional como "UG-RC4" → token "RC4" muy distintivo)
    combined = ambito_name + " " + (etiqueta or "")
    raw_tokens = [t for t in _normalize(combined).split() if t]
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


def lookup(x: float, y: float, concejo: Concejo | None = None) -> dict:
    """Para coords UTM (EPSG:25830) devuelve dict con todas las capas y matches.

    Si concejo es None, se infiere por bbox UTM (fallback OVIEDO).

    {
      "x": .., "y": ..,
      "layers": {<layer>: [<props>, ...]},
      "ug": [...] | None,            # principal UG si existe
      "fichas_match": [...],          # fichas que podrían corresponder
    }
    """
    if concejo is None:
        concejo = get_concejo_for_utm(x, y) or OVIEDO
    out: dict = {"x": x, "y": y, "layers": {}}

    # Paraleliza todas las queries WFS (8 capas en flight a la vez)
    tasks: list[tuple[str, float]] = [(L, 0.0) for L, _ in LAYERS]
    tasks += [(L, 50.0) for L in PATRIMONIO_LAYERS]
    results: dict[str, list[dict]] = {}
    with ThreadPoolExecutor(max_workers=len(tasks)) as pool:
        future_map = {
            pool.submit(_wfs_at_point, layer, x, y, [], tol, concejo): (layer, tol)
            for layer, tol in tasks
        }
        for fut in future_map:
            layer, tol = future_map[fut]
            try:
                feats = fut.result(timeout=20)
            except Exception:
                feats = []
            results[layer] = feats

    ug_props = None
    nombre_ambito = None
    for layer, _ in LAYERS:
        feats = results.get(layer) or []
        if feats:
            out["layers"][layer] = feats
            if layer == "n15_UNIDADES_GESTION" and not nombre_ambito:
                ug_props = feats[0]
                nombre_ambito = feats[0].get("Nombre_del_Area") or feats[0].get("Etiqueta")
            elif layer == "n25_AREAS_MODIF_URBANISTICAS" and not nombre_ambito:
                nombre_ambito = feats[0].get("Nombre_del_Area") or feats[0].get("Etiqueta")

    # Patrimonio
    patrimonio = []
    for layer in PATRIMONIO_LAYERS:
        for f in results.get(layer) or []:
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
    # Etiqueta para matching de alta confianza
    etiqueta = None
    codigo_tipo = None
    for src in (ug_props,
                (out["layers"].get("n25_AREAS_MODIF_URBANISTICAS") or [{}])[0],
                (out["layers"].get("n22_INSTRUMENTOS_PLANEAMIENTO") or [{}])[0]):
        if isinstance(src, dict):
            etiqueta = etiqueta or src.get("Etiqueta")
            codigo_tipo = codigo_tipo or src.get("Cód._Tipo_Instrumento")
    out["fichas_match"] = (_find_matching_ficha(nombre_ambito, etiqueta, codigo_tipo)
                            if (nombre_ambito and has_real_ambito) else [])
    return out
