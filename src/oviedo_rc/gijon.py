"""Gijón: lookup de ámbito PGOU por punto UTM 25830.

Datos en `~/.cache/oviedo_rc/gijon/ambitos.json` (generados por
`scripts/fetch_gijon_kml.py`).

Modelo distinto a Oviedo: Gijón publica un KML con 513 polígonos
georreferenciados directamente; no hay paginación de hojas ni calibración.
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.request
from pathlib import Path

CACHE = Path.home() / ".cache" / "oviedo_rc" / "gijon"
AMBITOS_FILE = CACHE / "ambitos.json"
FICHAS_META_DIR = CACHE / "fichas_meta"
FICHAS_META_DIR.mkdir(parents=True, exist_ok=True)
FICHA_BASE = "https://documentos.gijon.es/PGO/ficha.php?id="

ID_MUNICIPIO_GIJON = 33024
BBOX_UTM = (270921.0, 4813038.0, 292773.0, 4829451.0)  # min_x, min_y, max_x, max_y

# TTL para entradas de ficha_meta sin contenido útil (clase/categoria vacíos):
# tras este tiempo se refresca por si el Ayto. arregló el HTML.
FICHA_META_STALE_SEC = 86400  # 24h

_AMBITOS_CACHE: list[dict] | None = None
_AMBITOS_MTIME: float = 0.0
_AMBITOS_LOCK = threading.Lock()


def _load() -> list[dict]:
    """Carga ambitos.json con cache mtime-aware (recarga si el JSON cambia en disco)."""
    global _AMBITOS_CACHE, _AMBITOS_MTIME
    if not AMBITOS_FILE.exists():
        return []
    mtime = AMBITOS_FILE.stat().st_mtime
    with _AMBITOS_LOCK:
        if _AMBITOS_CACHE is None or mtime > _AMBITOS_MTIME:
            try:
                _AMBITOS_CACHE = json.loads(AMBITOS_FILE.read_text(encoding="utf-8"))
                _AMBITOS_MTIME = mtime
            except (OSError, json.JSONDecodeError):
                if _AMBITOS_CACHE is None:
                    _AMBITOS_CACHE = []
        return _AMBITOS_CACHE


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


FICHAS_DATA_DIR = CACHE / "fichas_data"


def get_ficha_data(ambito_id: str) -> dict | None:
    """Lee el JSON estructurado generado por scripts/parse_gijon_fichas.py.
    Devuelve None si no hay ficha parseada para ese ámbito."""
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", ambito_id)
    p = FICHAS_DATA_DIR / f"{safe_id}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


_FICHA_HEAD_RE = re.compile(
    r"<h3>\s*Clase:\s*([^<]+?)</h3>.*?"
    r"<h4>\s*Categoría:\s*([^<]+?)</h4>.*?"
    r"<h4>\s*Plan de Desarrollo:\s*([^<]+?)</h4>.*?"
    r"<h4>\s*Iniciativa:\s*([^<]+?)</h4>",
    re.S,
)
_FICHA_PDF_RE = re.compile(r"href=\"(https://documentos\.gijon\.es/doc/Urbanismo/PGO/Fichas/[^\"]+\.pdf)")
_PLANO_PDF_RE = re.compile(r"href=\"(https://documentos\.gijon\.es/doc/Urbanismo/PGO/Planos/[^\"]+\.pdf)")
_NOMBRE_RE = re.compile(r"<h2>\s*([^<]+?)\s*</h2>\s*<h3>\s*Clase:", re.S)


def fetch_ficha_meta(ambito_id: str) -> dict:
    """Descarga + parsea la ficha HTML de un ámbito. Cachea en disco.

    Devuelve: {clase, categoria, plan_desarrollo, iniciativa, nombre_largo,
               ficha_pdf_url, plano_pdf_url}
    Campos vacíos si la ficha no los tiene.
    """
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", ambito_id)
    cache = FICHAS_META_DIR / f"{safe_id}.json"
    if cache.exists():
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            # TTL implícito: si la cache no tiene `clase` (parsed roto o error
            # antiguo) y han pasado >24h, forzamos refetch — el Ayto. puede
            # haber arreglado el HTML.
            age = time.time() - cache.stat().st_mtime
            if data.get("clase") or age <= FICHA_META_STALE_SEC:
                return data
        except Exception:
            pass

    out: dict = {"ambito_id": ambito_id, "ficha_web_url": FICHA_BASE + ambito_id}
    try:
        req = urllib.request.Request(
            FICHA_BASE + ambito_id,
            headers={"User-Agent": "iarq-locator/1.0"},
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            html = r.read().decode("utf-8", errors="ignore")
    except Exception as e:
        # No persistir errores: que el próximo intento reintente.
        out["error"] = f"fetch fail: {type(e).__name__}"
        return out

    m = _FICHA_HEAD_RE.search(html)
    if m:
        out["clase"] = m.group(1).strip()
        out["categoria"] = m.group(2).strip()
        out["plan_desarrollo"] = m.group(3).strip()
        out["iniciativa"] = m.group(4).strip()
    nm = _NOMBRE_RE.search(html)
    if nm:
        out["nombre_largo"] = nm.group(1).strip()
    fm = _FICHA_PDF_RE.search(html)
    if fm:
        out["ficha_pdf_url"] = fm.group(1)
    pm = _PLANO_PDF_RE.search(html)
    if pm:
        out["plano_pdf_url"] = pm.group(1)

    # Sólo cachear si el parseo produjo contenido útil. Si Ayto. cambió el
    # HTML y no logramos `clase` ni `categoria`, no persistimos (next call
    # reintenta).
    if out.get("clase") or out.get("categoria"):
        cache.write_text(json.dumps(out, ensure_ascii=False, indent=2))
    return out


def stats() -> dict:
    ambitos = _load()
    from collections import Counter
    return {
        "ambitos": len(ambitos),
        "categorias": dict(Counter(a["categoria"] for a in ambitos).most_common(10)),
    }
