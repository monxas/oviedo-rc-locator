"""Carga corrección post-modelo por cell desde calibration_offsets.json.

El modelo geométrico tiene biases sistemáticos distintos por zona del PGOU.
Estos offsets se calcularon de 45 puntos labeled manualmente vía la web
validator. Se aplican en píxel-space tras `utm_polygon_to_pixel`.

Multi-concejo (PR3): las funciones aceptan un `Concejo` opcional. Mientras
sólo haya un concejo registrado (OVIEDO), se mantiene el path legacy
`data/calibration_offsets.json` para no romper a `recalibrate.py`. Cuando
se añada un segundo concejo, este módulo migrará al esquema
`data/calibration/<ine>_<slug>.json` (ver TODO).
"""
import json
import threading
from pathlib import Path

from .concejo import OVIEDO, Concejo

_CACHE: dict[str, dict] = {}
_CACHE_MTIME: dict[str, float] = {}
_CACHE_LOCK = threading.Lock()


def _path(concejo: Concejo | None = None) -> Path:
    """Path del JSON de offsets para el concejo dado.

    OVIEDO usa el path legacy (`data/calibration_offsets.json`) para no
    romper a recalibrate.py. Otros concejos: `data/calibration/<ine>_<slug>.json`.
    """
    c = concejo or OVIEDO
    repo_root = Path(__file__).resolve().parents[2]
    if c.id_ine == OVIEDO.id_ine:
        return repo_root / "data" / "calibration_offsets.json"
    return repo_root / "data" / "calibration" / f"{c.id_ine}_{c.slug}.json"


def _load(concejo: Concejo | None = None):
    """Carga (o re-carga) `calibration_offsets.json` para el concejo.

    Se cachea el contenido en memoria, pero se re-lee si la mtime del fichero
    cambia. Esto permite que `recalibrate.py` actualice las offsets en disco
    y los servicios (locator/validator) las recojan en la próxima request,
    sin necesidad de un restart explícito.
    """
    c = concejo or OVIEDO
    key = c.slug
    p = _path(c)
    try:
        mtime = p.stat().st_mtime
    except FileNotFoundError:
        mtime = -1.0
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached is not None and _CACHE_MTIME.get(key, -1.0) == mtime:
            return cached
        if mtime < 0:
            data = {"global_bias_px": [0, 0], "cell_offsets_px": {}}
        else:
            try:
                data = json.loads(p.read_text())
            except (OSError, json.JSONDecodeError):
                # Don't blow up mid-write — keep previous cache if any.
                if cached is not None:
                    return cached
                data = {"global_bias_px": [0, 0], "cell_offsets_px": {}}
        _CACHE[key] = data
        _CACHE_MTIME[key] = mtime
        return data


def offset_for(cell: str, sub_quadrant: str | None = None,
                concejo: Concejo | None = None) -> tuple[int, int]:
    """Devuelve (dx, dy) en píxeles a sumar a la predicción del modelo.

    Resolución preferida:
      1) Offset por (cell, sub_quadrant) si existe — más preciso (median ~3 px)
      2) Offset por cell (incluye interpolados) — median ~18 px
      3) Bias global como último fallback
    """
    cal = _load(concejo)
    if sub_quadrant:
        key = f"{cell}-{sub_quadrant}"
        cs_off = cal.get("csub_offsets_px", {}).get(key)
        if cs_off:
            return int(round(cs_off[0])), int(round(cs_off[1]))
    cell_off = cal.get("cell_offsets_px", {}).get(cell)
    if cell_off:
        return int(round(cell_off[0])), int(round(cell_off[1]))
    g = cal.get("global_bias_px", [0, 0])
    return int(round(g[0])), int(round(g[1]))


# Backwards-compat alias
def offset_for_cell(cell: str, concejo: Concejo | None = None) -> tuple[int, int]:
    return offset_for(cell, None, concejo)


def has_offset_for(cell: str, sub_quadrant: str | None = None,
                    concejo: Concejo | None = None) -> bool:
    cal = _load(concejo)
    if sub_quadrant and f"{cell}-{sub_quadrant}" in cal.get("csub_offsets_px", {}):
        return True
    return cell in cal.get("cell_offsets_px", {})


# Constante px/m a 300 DPI sobre escala 1:1000
PX_PER_M = 11.81


def quality_for(cell: str, sub_quadrant: str | None = None,
                 concejo: Concejo | None = None) -> dict:
    """Devuelve un dict con la calidad esperada de la calibración para
    este (cell, sub). Útil para warnings en metadata del bundle.

    Keys:
      source: 'csub' | 'cell' | 'cell_interpolated' | 'global'
      n_labels: cuántos labels reales contribuyeron (0 si interpolado/global)
      expected_residual_px / _m: σ_total estimada del bucket (0 si insuficiente)
      reliability: 'high' | 'ok' | 'low' | 'unknown'
    """
    cal = _load(concejo)
    key = f"{cell}-{sub_quadrant}" if sub_quadrant else None
    stats = cal.get("csub_stats", {}).get(key) if key else None

    if stats:
        n = stats["n"]
        res_px = stats["expected_residual_px"]
        if n >= 3 and res_px < 25:
            rel = "high"
        elif n >= 2 and res_px < 50:
            rel = "ok"
        elif n == 1:
            rel = "low"  # 1 sample, no idea about variance
        else:
            rel = "low"
        return {
            "source": "csub",
            "n_labels": n,
            "expected_residual_px": res_px,
            "expected_residual_m": round(res_px / PX_PER_M, 2),
            "reliability": rel,
        }
    if cell in cal.get("cells_with_direct_data", []):
        return {"source": "cell", "n_labels": 0,
                "expected_residual_px": 30, "expected_residual_m": round(30 / PX_PER_M, 2),
                "reliability": "ok"}
    if cell in cal.get("cells_interpolated", []):
        return {"source": "cell_interpolated", "n_labels": 0,
                "expected_residual_px": 60, "expected_residual_m": round(60 / PX_PER_M, 2),
                "reliability": "low"}
    return {"source": "global", "n_labels": 0,
            "expected_residual_px": 80, "expected_residual_m": round(80 / PX_PER_M, 2),
            "reliability": "unknown"}


def edge_neighbors(X: float, Y: float, edge_threshold_m: float = 50,
                    concejo: Concejo | None = None) -> list[dict]:
    """Si el RC en UTM (X, Y) está cerca del borde de su cell, devuelve cells
    vecinas candidatas con sus sheets PGOU si existen."""
    from . import pgou
    c = concejo or OVIEDO
    m = c.malla
    if m is None:
        return []

    col = int((X - m.x0) // m.cell_w)
    row = int((m.ymax - Y) // m.cell_h)
    x_in = (X - (m.x0 + col * m.cell_w)) / m.cell_w
    y_in = (m.ymax - row * m.cell_h - Y) / m.cell_h

    candidates = set()
    if x_in * m.cell_w < edge_threshold_m:
        candidates.add((col - 1, row))
    if (1 - x_in) * m.cell_w < edge_threshold_m:
        candidates.add((col + 1, row))
    if y_in * m.cell_h < edge_threshold_m:
        candidates.add((col, row - 1))
    if (1 - y_in) * m.cell_h < edge_threshold_m:
        candidates.add((col, row + 1))

    try:
        sheets = pgou.get_sheet_listing(c)
    except Exception:
        return []
    out = []
    for cc, r in candidates:
        if not (0 <= r < 25):
            continue
        letter = "ABCDEFGHIJKLMNOPQRSTUVWXY"[r]
        nx_in = x_in - (cc - col)
        ny_in = y_in - (r - row)
        compass = ("N" if ny_in < m.ns_threshold else "S") + \
                  ("W" if nx_in < m.ew_threshold else "E")
        sub = m.sub_convention[compass]
        sheet = f"PLANO_{cc}_{letter}_{sub}.pdf"
        if sheet in sheets:
            out.append({
                "cell": f"{cc}-{letter}",
                "sub_quadrant": sub,
                "sub_compass": compass,
                "sheet_name": sheet,
                "col": cc, "row": r,
            })
    return out
