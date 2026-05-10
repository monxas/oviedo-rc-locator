"""Renderizado de PDFs y composición de PNGs anotados."""
import numpy as np

from .config import PDF_DPI, BODY_W_M, BODY_H_M


def render_pdf_page(pdf_path, dpi=PDF_DPI, page_idx=0):
    """Devuelve (img_bgr, page_width_pt, page_height_pt) de la página."""
    import fitz
    import cv2
    doc = fitz.open(str(pdf_path))
    try:
        page = doc[page_idx]
        pix = page.get_pixmap(dpi=dpi, alpha=False)
        arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.h, pix.w, 3)
        img = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
        return img, page.rect.width, page.rect.height
    finally:
        doc.close()


def detect_body_rect(img_bgr):
    """Detecta el rectángulo del *body* del plano (la región dentro del marco
    decorativo). Heurística: contorno rectangular con mayor área."""
    import cv2
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    _, th = cv2.threshold(gray, 200, 255, cv2.THRESH_BINARY_INV)
    contours, _ = cv2.findContours(th, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    best = None
    H, W = gray.shape
    for c in contours:
        x, y, w, h = cv2.boundingRect(c)
        if w * h < 0.3 * W * H:
            continue
        ratio = w / max(1, h)
        target = BODY_W_M / BODY_H_M
        if abs(ratio - target) > 0.2:
            continue
        if best is None or w * h > best[2] * best[3]:
            best = (x, y, w, h)
    if best is None:
        # Fallback: ~5% margen interno
        return int(W * 0.05), int(H * 0.05), int(W * 0.9), int(H * 0.9)
    return best


def body_rel_to_pixel(rx, ry, body_rect):
    bx, by, bw, bh = body_rect
    return int(bx + rx * bw), int(by + ry * bh)


def utm_polygon_to_pixel(poly_utm, body_rect, anchor_utm, sub_compass):
    """Convierte polígono UTM → píxeles del plano.
    `anchor_utm` es la esquina UTM (X_min, Y_max) del body, calculada por geom."""
    bx, by, bw, bh = body_rect
    ax, ay = anchor_utm
    out = []
    for X, Y in poly_utm:
        rx = (X - ax) / BODY_W_M
        ry = (ay - Y) / BODY_H_M
        out.append((int(bx + rx * bw), int(by + ry * bh)))
    return out


def draw_marker(img_bgr, px, py, *, color=(0, 0, 255), radius=18):
    import cv2
    out = img_bgr.copy()
    cv2.circle(out, (px, py), radius, color, 3)
    cv2.line(out, (px - radius - 8, py), (px - 4, py), color, 2)
    cv2.line(out, (px + 4, py), (px + radius + 8, py), color, 2)
    cv2.line(out, (px, py - radius - 8), (px, py - 4), color, 2)
    cv2.line(out, (px, py + 4), (px, py + radius + 8), color, 2)
    return out


def draw_polygon(img_bgr, poly_px, *, color=(0, 0, 255), thickness=3):
    import cv2
    out = img_bgr.copy()
    pts = np.array(poly_px, dtype=np.int32).reshape((-1, 1, 2))
    cv2.polylines(out, [pts], isClosed=True, color=color, thickness=thickness)
    return out


def crop_around(img_bgr, px, py, size=900):
    H, W = img_bgr.shape[:2]
    x1 = max(0, px - size // 2)
    y1 = max(0, py - size // 2)
    x2 = min(W, x1 + size)
    y2 = min(H, y1 + size)
    return img_bgr[y1:y2, x1:x2]
