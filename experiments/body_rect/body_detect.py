"""Detección del body_rect del plano PGOU — alternativas a la heurística legacy.

Selección por env var BODY_DETECT_METHOD (o argumento `method`):
  - "heuristic" (default): heurística legacy `render.detect_body_rect`.
  - "heuristic_v2"        : multi-threshold + Otsu + morfología, ratio relajada.
  - "hough"               : Hough lines → mayor cuadrilátero.
  - "template"            : template matching de 4 esquinas extraídas de una
                            hoja de referencia.

Todos devuelven `(x, y, w, h)` enteros, compatible con consumidores
existentes (pipeline, ficha_plano, validator_ui, scripts/validate_snap).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Tuple

import numpy as np

from oviedo_rc.config import BODY_H_M, BODY_W_M

# Ratio objetivo de body_rect (Oviedo SU/SNU coinciden en ~1.5).
_TARGET_RATIO = BODY_W_M / BODY_H_M
_MIN_AREA_FRAC = 0.30
_FALLBACK_MARGIN = 0.05


# --------------------------------------------------------------------------- #
# Heurística legacy (idéntica a render.detect_body_rect, repetida aquí para
# tener todas las variantes en un sitio y poder benchmark side-by-side).
# --------------------------------------------------------------------------- #
def detect_heuristic(img_bgr: np.ndarray) -> Tuple[int, int, int, int]:
    import cv2
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    _, th = cv2.threshold(gray, 200, 255, cv2.THRESH_BINARY_INV)
    contours, _ = cv2.findContours(th, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    best = None
    H, W = gray.shape
    for c in contours:
        x, y, w, h = cv2.boundingRect(c)
        if w * h < _MIN_AREA_FRAC * W * H:
            continue
        ratio = w / max(1, h)
        if abs(ratio - _TARGET_RATIO) > 0.2:
            continue
        if best is None or w * h > best[2] * best[3]:
            best = (x, y, w, h)
    if best is None:
        return _fallback_rect(W, H)
    return best


def _fallback_rect(W: int, H: int) -> Tuple[int, int, int, int]:
    return (int(W * _FALLBACK_MARGIN), int(H * _FALLBACK_MARGIN),
            int(W * (1 - 2 * _FALLBACK_MARGIN)), int(H * (1 - 2 * _FALLBACK_MARGIN)))


# --------------------------------------------------------------------------- #
# Variante (c): heurística v2 — Otsu + cierre morfológico + ratio relajada.
# --------------------------------------------------------------------------- #
def detect_heuristic_v2(img_bgr: np.ndarray) -> Tuple[int, int, int, int]:
    import cv2
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    H, W = gray.shape

    candidates: list[tuple[int, int, int, int]] = []
    for th_val in (200, 220, 180):
        _, th = cv2.threshold(gray, th_val, 255, cv2.THRESH_BINARY_INV)
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
        th = cv2.morphologyEx(th, cv2.MORPH_CLOSE, kernel)
        contours, _ = cv2.findContours(th, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for c in contours:
            x, y, w, h = cv2.boundingRect(c)
            if w * h < _MIN_AREA_FRAC * W * H:
                continue
            ratio = w / max(1, h)
            if abs(ratio - _TARGET_RATIO) > 0.3:
                continue
            candidates.append((x, y, w, h))

    _, th_otsu = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
    th_otsu = cv2.morphologyEx(th_otsu, cv2.MORPH_CLOSE, kernel)
    contours, _ = cv2.findContours(th_otsu, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    for c in contours:
        x, y, w, h = cv2.boundingRect(c)
        if w * h < _MIN_AREA_FRAC * W * H:
            continue
        ratio = w / max(1, h)
        if abs(ratio - _TARGET_RATIO) > 0.3:
            continue
        candidates.append((x, y, w, h))

    if not candidates:
        return _fallback_rect(W, H)
    candidates.sort(key=lambda r: -(r[2] * r[3]))
    best_area = candidates[0][2] * candidates[0][3]
    near_top = [c for c in candidates if c[2] * c[3] >= 0.95 * best_area]
    near_top.sort(key=lambda r: abs((r[2] / max(1, r[3])) - _TARGET_RATIO))
    return near_top[0]


# --------------------------------------------------------------------------- #
# Variante (b): Hough lines → mayor cuadrilátero.
# --------------------------------------------------------------------------- #
def detect_hough(img_bgr: np.ndarray) -> Tuple[int, int, int, int]:
    import cv2
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    H, W = gray.shape

    edges = cv2.Canny(gray, 50, 150, apertureSize=3)
    min_len = int(min(W, H) * 0.3)
    lines = cv2.HoughLinesP(edges, rho=1, theta=np.pi / 180, threshold=200,
                            minLineLength=min_len, maxLineGap=20)
    if lines is None:
        return _fallback_rect(W, H)

    horizontals: list[int] = []  # y positions
    verticals: list[int] = []    # x positions
    for line in lines[:, 0, :]:
        x1, y1, x2, y2 = line
        dx, dy = abs(x2 - x1), abs(y2 - y1)
        length = (dx ** 2 + dy ** 2) ** 0.5
        if length < min_len:
            continue
        if dy < 5 and dx >= min_len:
            horizontals.append((y1 + y2) // 2)
        elif dx < 5 and dy >= min_len:
            verticals.append((x1 + x2) // 2)

    if len(horizontals) < 2 or len(verticals) < 2:
        return _fallback_rect(W, H)

    def cluster(values, tol=15):
        """Agrupa valores cercanos en clusters (1D)."""
        values = sorted(values)
        clusters = [[values[0]]]
        for v in values[1:]:
            if v - clusters[-1][-1] <= tol:
                clusters[-1].append(v)
            else:
                clusters.append([v])
        return [int(sum(c) / len(c)) for c in clusters]

    hs = cluster(horizontals)
    vs = cluster(verticals)
    if len(hs) < 2 or len(vs) < 2:
        return _fallback_rect(W, H)

    best = None
    for i in range(len(vs)):
        for j in range(i + 1, len(vs)):
            x_left, x_right = vs[i], vs[j]
            w = x_right - x_left
            if w < _MIN_AREA_FRAC ** 0.5 * W:
                continue
            for a in range(len(hs)):
                for b in range(a + 1, len(hs)):
                    y_top, y_bot = hs[a], hs[b]
                    h = y_bot - y_top
                    if h < _MIN_AREA_FRAC ** 0.5 * H:
                        continue
                    if w * h < _MIN_AREA_FRAC * W * H:
                        continue
                    ratio = w / max(1, h)
                    if abs(ratio - _TARGET_RATIO) > 0.25:
                        continue
                    area = w * h
                    if best is None or area > best[0]:
                        best = (area, x_left, y_top, w, h)

    if best is None:
        return _fallback_rect(W, H)
    _, x, y, w, h = best
    return (x, y, w, h)


# --------------------------------------------------------------------------- #
# Variante (a): template matching de las 4 esquinas.
# --------------------------------------------------------------------------- #
_TEMPLATE_SIZE = 200
_TEMPLATE_CACHE_DIR = Path(os.environ.get(
    "BODY_DETECT_TEMPLATE_CACHE",
    str(Path(__file__).resolve().parent / "templates"),
))


def _template_paths(kind: str) -> dict:
    return {
        corner: _TEMPLATE_CACHE_DIR / f"{kind}_{corner}.png"
        for corner in ("tl", "tr", "bl", "br")
    }


def build_templates(reference_pdf: str, kind: str = "default") -> None:
    """Construye templates de las 4 esquinas a partir de una hoja de referencia.

    Se asume que `detect_heuristic` da un body_rect correcto en la referencia
    (validar visualmente antes de llamar). Crops de _TEMPLATE_SIZE×_TEMPLATE_SIZE
    centrados en cada esquina del body.
    """
    import cv2
    from oviedo_rc import render as _render
    img, _, _ = _render.render_pdf_page(reference_pdf)
    x, y, w, h = detect_heuristic(img)
    s = _TEMPLATE_SIZE
    half = s // 2
    H, W = img.shape[:2]
    corners = {
        "tl": (x, y), "tr": (x + w, y),
        "bl": (x, y + h), "br": (x + w, y + h),
    }
    _TEMPLATE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    for name, (cx, cy) in corners.items():
        x0 = max(0, cx - half)
        y0 = max(0, cy - half)
        x1 = min(W, x0 + s)
        y1 = min(H, y0 + s)
        crop = img[y0:y1, x0:x1]
        cv2.imwrite(str(_template_paths(kind)[name]), crop)


def _match_with_kind(gray: np.ndarray, kind: str) -> tuple | None:
    import cv2
    paths = _template_paths(kind)
    if not all(p.exists() for p in paths.values()):
        return None
    locs: dict[str, tuple[int, int, float]] = {}
    for name, p in paths.items():
        tmpl = cv2.imread(str(p), cv2.IMREAD_GRAYSCALE)
        if tmpl is None:
            return None
        res = cv2.matchTemplate(gray, tmpl, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(res)
        cx = max_loc[0] + tmpl.shape[1] // 2
        cy = max_loc[1] + tmpl.shape[0] // 2
        locs[name] = (cx, cy, max_val)
    return locs


def detect_template(img_bgr: np.ndarray, kind: str | None = None) -> Tuple[int, int, int, int]:
    """Template matching de 4 esquinas.

    Si `kind` es None, prueba todos los `kind` con templates disponibles
    y elige el de mejor score mínimo (peor esquina) → más robusto.
    """
    import cv2
    H, W = img_bgr.shape[:2]
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)

    kinds = [kind] if kind else _available_kinds()
    best: tuple | None = None  # (min_score, locs)
    for k in kinds:
        locs = _match_with_kind(gray, k)
        if locs is None:
            continue
        min_score = min(v[2] for v in locs.values())
        if best is None or min_score > best[0]:
            best = (min_score, locs)

    if best is None or best[0] < 0.35:
        return _fallback_rect(W, H)
    locs = best[1]
    x_left = (locs["tl"][0] + locs["bl"][0]) // 2
    x_right = (locs["tr"][0] + locs["br"][0]) // 2
    y_top = (locs["tl"][1] + locs["tr"][1]) // 2
    y_bot = (locs["bl"][1] + locs["br"][1]) // 2
    w = x_right - x_left
    h = y_bot - y_top
    if w * h < _MIN_AREA_FRAC * W * H:
        return _fallback_rect(W, H)
    ratio = w / max(1, h)
    if abs(ratio - _TARGET_RATIO) > 0.3:
        return _fallback_rect(W, H)
    return (x_left, y_top, w, h)


def _available_kinds() -> list[str]:
    if not _TEMPLATE_CACHE_DIR.exists():
        return []
    kinds = set()
    for f in _TEMPLATE_CACHE_DIR.iterdir():
        if f.suffix == ".png":
            stem = f.stem
            for suf in ("_tl", "_tr", "_bl", "_br"):
                if stem.endswith(suf):
                    kinds.add(stem[: -len(suf)])
    return sorted(kinds)


# --------------------------------------------------------------------------- #
# Dispatcher.
# --------------------------------------------------------------------------- #
_METHODS = {
    "heuristic": detect_heuristic,
    "heuristic_v2": detect_heuristic_v2,
    "hough": detect_hough,
    "template": detect_template,
}


def detect_body_rect(img_bgr: np.ndarray, method: str | None = None) -> Tuple[int, int, int, int]:
    """Punto de entrada con dispatcher por env var BODY_DETECT_METHOD."""
    if method is None:
        method = os.environ.get("BODY_DETECT_METHOD", "heuristic")
    fn = _METHODS.get(method)
    if fn is None:
        raise ValueError(f"método desconocido: {method!r}, opciones: {list(_METHODS)}")
    return fn(img_bgr)
