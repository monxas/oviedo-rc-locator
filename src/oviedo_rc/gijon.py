"""Gijón: lookup de ámbito PGOU por punto UTM 25830.

Datos en `~/.cache/oviedo_rc/gijon/ambitos.json` (generados por
`scripts/fetch_gijon_kml.py`).

Modelo distinto a Oviedo: Gijón publica un KML con 513 polígonos
georreferenciados directamente; no hay paginación de hojas ni calibración.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

CACHE = Path.home() / ".cache" / "oviedo_rc" / "gijon"
AMBITOS_FILE = CACHE / "ambitos.json"

ID_MUNICIPIO_GIJON = 33024
BBOX_UTM = (270921.0, 4813038.0, 292773.0, 4829451.0)  # min_x, min_y, max_x, max_y


@lru_cache(maxsize=1)
def _load() -> list[dict]:
    if not AMBITOS_FILE.exists():
        return []
    return json.loads(AMBITOS_FILE.read_text(encoding="utf-8"))


def _bbox_of(poly: list[tuple[float, float]]) -> tuple[float, float, float, float]:
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    return (min(xs), min(ys), max(xs), max(ys))


def _point_in_poly(x: float, y: float, poly: list[tuple[float, float]]) -> bool:
    """Ray-casting clásico. Polígono cerrado (último = primero opcional)."""
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi + 1e-12) + xi):
            inside = not inside
        j = i
    return inside


def in_gijon(x: float, y: float) -> bool:
    """Heurística rápida por bbox del concejo. Para distinguir Oviedo/Gijón."""
    xmin, ymin, xmax, ymax = BBOX_UTM
    return xmin <= x <= xmax and ymin <= y <= ymax


def lookup_ambitos(x: float, y: float) -> list[dict]:
    """Devuelve TODOS los ámbitos que contienen (x,y) UTM 25830.
    Pueden ser varios solapados (un APP dentro de un AAA, etc.)."""
    ambitos = _load()
    hits = []
    for a in ambitos:
        for poly in a["polygons_utm"]:
            bx0, by0, bx1, by1 = _bbox_of(poly)
            if x < bx0 or x > bx1 or y < by0 or y > by1:
                continue
            if _point_in_poly(x, y, poly):
                hits.append({
                    "id": a["id"],
                    "ficha_id": a.get("ficha_id"),
                    "categoria": a["categoria"],
                    "ficha_url": a["ficha_url"],
                })
                break  # ya está dentro de algún anillo de este ámbito
    return hits


def lookup(x: float, y: float) -> dict | None:
    """Conveniencia: el ámbito "más relevante" en (x,y) UTM.
    Prioriza ámbitos más pequeños (más específicos)."""
    hits = lookup_ambitos(x, y)
    if not hits:
        return None
    ambitos = _load()
    by_id = {a["id"]: a for a in ambitos}

    def area_of(hit):
        a = by_id.get(hit["id"], {})
        total = 0.0
        for poly in a.get("polygons_utm", []):
            n = len(poly)
            s = 0.0
            for i in range(n):
                xi, yi = poly[i]
                xj, yj = poly[(i + 1) % n]
                s += xi * yj - xj * yi
            total += abs(s) / 2
        return total

    hits.sort(key=area_of)
    return hits[0]


def stats() -> dict:
    ambitos = _load()
    from collections import Counter
    return {
        "ambitos": len(ambitos),
        "categorias": dict(Counter(a["categoria"] for a in ambitos).most_common(10)),
    }
