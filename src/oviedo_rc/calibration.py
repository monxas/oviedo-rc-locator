"""Carga corrección post-modelo por cell desde calibration_offsets.json.

El modelo geométrico tiene biases sistemáticos distintos por zona del PGOU.
Estos offsets se calcularon de 45 puntos labeled manualmente vía la web
validator. Se aplican en píxel-space tras `utm_polygon_to_pixel`.
"""
import json
from pathlib import Path

_CACHE = None


def _path():
    return Path(__file__).resolve().parents[2] / "data" / "calibration_offsets.json"


def _load():
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    p = _path()
    if not p.exists():
        _CACHE = {"global_bias_px": [0, 0], "cell_offsets_px": {}}
    else:
        _CACHE = json.loads(p.read_text())
    return _CACHE


def offset_for(cell: str, sub_quadrant: str | None = None) -> tuple[int, int]:
    """Devuelve (dx, dy) en píxeles a sumar a la predicción del modelo.

    Resolución preferida:
      1) Offset por (cell, sub_quadrant) si existe — más preciso (median ~3 px)
      2) Offset por cell (incluye interpolados) — median ~18 px
      3) Bias global como último fallback
    """
    cal = _load()
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
def offset_for_cell(cell: str) -> tuple[int, int]:
    return offset_for(cell, None)


def has_offset_for(cell: str, sub_quadrant: str | None = None) -> bool:
    cal = _load()
    if sub_quadrant and f"{cell}-{sub_quadrant}" in cal.get("csub_offsets_px", {}):
        return True
    return cell in cal.get("cell_offsets_px", {})


# Constante px/m a 300 DPI sobre escala 1:1000
PX_PER_M = 11.81


def quality_for(cell: str, sub_quadrant: str | None = None) -> dict:
    """Devuelve un dict con la calidad esperada de la calibración para
    este (cell, sub). Útil para warnings en metadata del bundle.

    Keys:
      source: 'csub' | 'cell' | 'cell_interpolated' | 'global'
      n_labels: cuántos labels reales contribuyeron (0 si interpolado/global)
      expected_residual_px / _m: σ_total estimada del bucket (0 si insuficiente)
      reliability: 'high' | 'ok' | 'low' | 'unknown'
    """
    cal = _load()
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
    # Fallback: per-cell offset (con o sin interpolar)
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


def edge_neighbors(X: float, Y: float, edge_threshold_m: float = 50) -> list[dict]:
    """Si el RC en UTM (X, Y) está cerca del borde de su cell, devuelve cells
    vecinas candidatas con sus sheets PGOU si existen."""
    from .config import (
        MALLA_X0, MALLA_YMAX, MALLA_CELL_W, MALLA_CELL_H, SUB_CONVENTION,
    )
    from . import pgou

    col = int((X - MALLA_X0) // MALLA_CELL_W)
    row = int((MALLA_YMAX - Y) // MALLA_CELL_H)
    x_in = (X - (MALLA_X0 + col * MALLA_CELL_W)) / MALLA_CELL_W
    y_in = (MALLA_YMAX - row * MALLA_CELL_H - Y) / MALLA_CELL_H

    candidates = set()
    if x_in * MALLA_CELL_W < edge_threshold_m:
        candidates.add((col - 1, row))
    if (1 - x_in) * MALLA_CELL_W < edge_threshold_m:
        candidates.add((col + 1, row))
    if y_in * MALLA_CELL_H < edge_threshold_m:
        candidates.add((col, row - 1))
    if (1 - y_in) * MALLA_CELL_H < edge_threshold_m:
        candidates.add((col, row + 1))

    try:
        sheets = pgou.get_sheet_listing()
    except Exception:
        return []
    out = []
    for c, r in candidates:
        if not (0 <= r < 25):
            continue
        letter = "ABCDEFGHIJKLMNOPQRSTUVWXY"[r]
        nx_in = x_in - (c - col)
        ny_in = y_in - (r - row)
        compass = ("N" if ny_in < 0.5 else "S") + ("W" if nx_in < 0.5 else "E")
        sub = SUB_CONVENTION[compass]
        sheet = f"PLANO_{c}_{letter}_{sub}.pdf"
        if sheet in sheets:
            out.append({
                "cell": f"{c}-{letter}",
                "sub_quadrant": sub,
                "sub_compass": compass,
                "sheet_name": sheet,
                "col": c, "row": r,
            })
    return out
