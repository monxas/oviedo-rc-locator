"""Snap del polígono catastral a las líneas del plano PGOU.

Render del PDF en gris, blackhat para resaltar líneas oscuras, y
cross-correlation 2D del polígono renderizado contra el plano para
encontrar el desplazamiento (dx, dy) en píxeles.

Geometría correcta (la versión anterior fallaba con polígonos > 180 px):
  - El template DEBE contener el polígono completo + un margen `pad`.
  - La ROI DEBE contener el template + 2*search_radius por cada lado.
  - matchTemplate exige roi_shape >= template_shape.
  - Multi-escala: prueba kernels finos (5px) y gruesos (15px) para
    capturar tanto líneas finas de parcela como contornos de edificio.
"""
import numpy as np


def _render_polygon_canvas(poly_px, w, h, thickness=2):
    """Pinta el polígono en un canvas WxH. poly_px ya en coords del canvas."""
    import cv2
    canvas = np.zeros((h, w), dtype=np.uint8)
    pts = np.array(poly_px, dtype=np.int32).reshape((-1, 1, 2))
    cv2.polylines(canvas, [pts], isClosed=True, color=255, thickness=thickness)
    return canvas


def _blackhat(plan_gray, kernel_size):
    """Extrae estructuras oscuras menores que kernel_size. Líneas finas
    (1-2 px) → kernel pequeño 5-7. Contornos gruesos → kernel 15+."""
    import cv2
    k = cv2.getStructuringElement(cv2.MORPH_RECT, (kernel_size, kernel_size))
    bh = cv2.morphologyEx(plan_gray, cv2.MORPH_BLACKHAT, k)
    return cv2.normalize(bh, None, 0, 255, cv2.NORM_MINMAX).astype(np.uint8)


def _try_snap(blackhat, poly_px_array, search_radius, pad=8):
    """Cross-correlation 1 escala. poly_px_array en coords absolutas del plano.

    Returns (dx, dy, score) o None si no hay espacio para template+search."""
    import cv2

    H, W = blackhat.shape

    # Bounding box del polígono en coords del plano
    poly = np.array(poly_px_array, dtype=np.int32)
    px_min, py_min = poly.min(axis=0)
    px_max, py_max = poly.max(axis=0)
    pw, ph = px_max - px_min, py_max - py_min

    # Template: polígono centrado con margen pad por cada lado
    tw = pw + 2 * pad
    th = ph + 2 * pad
    poly_in_template = poly - [px_min - pad, py_min - pad]
    template = _render_polygon_canvas(poly_in_template, tw, th).astype(np.float32)

    # ROI: bbox del polígono + search_radius por cada lado, recortada al plano
    rx1 = max(0, px_min - search_radius - pad)
    ry1 = max(0, py_min - search_radius - pad)
    rx2 = min(W, px_max + search_radius + pad)
    ry2 = min(H, py_max + search_radius + pad)
    roi = blackhat[ry1:ry2, rx1:rx2].astype(np.float32)

    if roi.shape[0] < th or roi.shape[1] < tw:
        return None

    corr = cv2.matchTemplate(roi, template, cv2.TM_CCORR_NORMED)
    _, peak, _, peak_loc = cv2.minMaxLoc(corr)

    # peak_loc = top-left del template en la ROI.
    # Posición del bbox del polígono detectada en coords absolutas:
    #   detected_px_min = peak_loc[0] + rx1 - pad  (porque template tiene pad)
    # Espera: el template tiene poly_in_template empezando en (pad, pad).
    # Si peak_loc es donde el template encaja en roi (esquina top-left),
    # entonces el polígono detectado tiene esquina top-left en
    #   peak_loc + (pad, pad) en coords roi
    #   = peak_loc + (pad, pad) + (rx1, ry1) en coords absolutas
    detected_px_min = peak_loc[0] + pad + rx1
    detected_py_min = peak_loc[1] + pad + ry1
    dx = detected_px_min - px_min
    dy = detected_py_min - py_min
    return int(dx), int(dy), float(peak)


def snap(plan_gray, poly_px, search_radius=40):
    """Devuelve (dx, dy, score). dx,dy: cuánto mover el polígono para alinear.

    Estrategia (coarse-to-fine, prefer-small):
      - Multi-kernel: 5×5 (líneas finas) + 7×7 + 11×11 + 15×15
      - Multi-radio: 80 → 40 → 20 → 12
      - Filtro: |dx|, |dy| < 90% radio (rechaza espurios) + score >= 0.05
      - Selección: mejor score y luego, entre los que están dentro de
        un margen del mejor (≥70% de su score), gana el de menor
        desplazamiento. El modelo geométrico está calibrado, las
        correcciones reales son <10 m. Picos lejanos son típicamente
        contornos de parcelas vecinas.
    """
    poly_arr = np.array(poly_px, dtype=np.int32)
    # Score mínimo: 0.10 evita matches espurios en zonas con pocas líneas
    # (parcelas sin edificar). Por debajo, mejor no snap que snap mal.
    MIN_SCORE = 0.10

    candidates: list[tuple[float, int, int, int, int]] = []  # (score, dx, dy, k, r)
    for kernel_size in (5, 7, 11, 15):
        bh = _blackhat(plan_gray, kernel_size)
        for r in (search_radius, search_radius // 2, search_radius // 4, 12):
            if r < 8:
                break
            result = _try_snap(bh, poly_arr, r)
            if result is None:
                continue
            dx, dy, score = result
            if abs(dx) > r * 0.9 or abs(dy) > r * 0.9:
                continue
            if score < MIN_SCORE:
                continue
            candidates.append((score, dx, dy, kernel_size, r))

    if not candidates:
        return 0, 0, 0.0

    best_score = max(c[0] for c in candidates)
    threshold = best_score * 0.7
    # Ordenar candidatos "competitivos" por desplazamiento ascendente,
    # luego por score descendente.
    competitive = [c for c in candidates if c[0] >= threshold]
    competitive.sort(key=lambda c: (abs(c[1]) + abs(c[2]), -c[0]))
    score, dx, dy, *_ = competitive[0]
    return dx, dy, score
