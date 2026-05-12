"""Render del plano de la página 1 de una ficha de ámbito + overlay del polígono catastral.

Pipeline:
1. Identificar el ámbito WFS (etiqueta) a partir del filename del PDF
2. Sacar centroide UTM de `ambitos_oviedo.json`
3. Renderizar página 1 del PDF a 200 dpi
4. Detectar body_rect (recuadro grande con marco)
5. Proyectar polígono catastral usando centroide ↔ centro body, escala 1:1000
6. Aplicar offset de calibración por ficha si existe (data/calibration_fichas.json)
7. Devolver PNG bytes

Calidad esperada sin cal: error ~20-30m. Tras 2-3 drags por ficha cae a <5m.
"""
from __future__ import annotations

import json
import re
import threading
from functools import lru_cache
from pathlib import Path

import cv2
import fitz
import numpy as np

CACHE = Path.home() / ".cache" / "oviedo_rc"
AMBITOS_FILE = CACHE / "ambitos_oviedo.json"
FICHAS_PDF_DIR = CACHE / "fichas"
RENDER_CACHE_DIR = CACHE / "ficha_planos"
RENDER_CACHE_DIR.mkdir(parents=True, exist_ok=True)
CAL_FILE = Path.home() / "oviedo-rc-locator" / "data" / "calibration_fichas.json"

DPI = 200
PX_PER_M = DPI / 25.4 * 1000 / 1000  # = DPI/25.4 ≈ 7.874 px/m a escala 1:1000
# (1m_real = 1mm_papel = 1/25.4 inch = DPI/25.4 px)

_AMBITOS_CACHE = None
_AMBITOS_LOCK = threading.Lock()


def _load_ambitos() -> dict:
    """Carga ambitos_oviedo.json con cache simple."""
    global _AMBITOS_CACHE
    with _AMBITOS_LOCK:
        if _AMBITOS_CACHE is None:
            if not AMBITOS_FILE.exists():
                _AMBITOS_CACHE = {}
            else:
                _AMBITOS_CACHE = json.loads(AMBITOS_FILE.read_text(encoding="utf-8"))
    return _AMBITOS_CACHE


def _norm(s: str) -> str:
    if not s:
        return ""
    s = s.upper().replace("Ñ", "N").replace("_", " ").replace("-", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _match_ambito_for_filename(filename: str) -> dict | None:
    """Empareja filename ficha PDF → entrada de ambitos_oviedo.json.

    Filename ej: 'RODRIGUEZ_CABEZAS_4_UG_RC4_Ficha_n_0120_PGOU.pdf'
    Etiqueta WFS: 'UG-RODRIGUEZ CABEZAS 4' o 'PE-X NOMBRE'
    """
    ambitos = _load_ambitos()
    stem = filename.rsplit(".pdf", 1)[0]
    # parsear el filename: <NOMBRE>_<TIPO>_<CODIGO>_Ficha_n_<NUM>
    m = re.match(
        r"(?P<nombre>.+?)_(?P<tipo>UG2?[E]?|UG1|AU[SE]?|AA|PE|PP|SUNC|API|SR|AM\d?|ASM)"
        r"_(?P<codigo>[A-Z0-9]+(?:_\d+)?)_Ficha_n_(?P<num>\d+)",
        stem, re.IGNORECASE,
    )
    if not m:
        return None
    tipo = m.group("tipo").upper()
    nombre = _norm(m.group("nombre"))
    # Construir variantes de etiqueta esperada
    candidatos = {
        f"{tipo}-{nombre}",
        f"{tipo} {nombre}",
        nombre,
        f"{tipo}-{m.group('codigo').upper()}",
    }
    # Match flexible: comparar normalizado
    for etiqueta, data in ambitos.items():
        et_norm = _norm(etiqueta)
        for cand in candidatos:
            if _norm(cand) == et_norm:
                return {"etiqueta": etiqueta, **data}
    # Fallback: substring match (nombre contenido en etiqueta normalizada)
    for etiqueta, data in ambitos.items():
        et_norm = _norm(etiqueta)
        if nombre and nombre in et_norm:
            return {"etiqueta": etiqueta, **data}
    return None


def _detect_body_rect(img):
    """Detecta el recuadro cartográfico del plano (heurística: contorno mayor)."""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    _, th = cv2.threshold(gray, 200, 255, cv2.THRESH_BINARY_INV)
    contours, _ = cv2.findContours(th, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    H, W = gray.shape
    best = None
    for c in contours:
        x, y, w, h = cv2.boundingRect(c)
        if w * h < 0.3 * W * H:
            continue
        if best is None or w * h > best[2] * best[3]:
            best = (x, y, w, h)
    if best is None:
        return (int(W * 0.04), int(H * 0.10), int(W * 0.96), int(H * 0.88))
    return (best[0], best[1], best[0] + best[2], best[1] + best[3])


def _load_cal() -> dict:
    """Carga calibration_fichas.json (offset por etiqueta de ámbito)."""
    if not CAL_FILE.exists():
        return {}
    try:
        return json.loads(CAL_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def render_with_overlay(filename: str, polygon_utm: list[tuple[float, float]]) -> dict | None:
    """Renderiza la página 1 de la ficha con el polígono catastral dibujado.

    Args:
        filename: nombre del PDF de ficha (ej 'RODRIGUEZ_CABEZAS_4_UG_RC4_...pdf')
        polygon_utm: lista de (x_utm, y_utm) del polígono del RC

    Returns:
        dict con {png_bytes, ambito_etiqueta, body_rect, cal_offset, width, height}
        o None si no se puede emparejar/renderizar.
    """
    ambito = _match_ambito_for_filename(filename)
    if not ambito:
        return None
    pdf_path = FICHAS_PDF_DIR / filename
    if not pdf_path.exists():
        return None

    cx_utm, cy_utm = ambito["centroid_utm"]

    # Render página 1
    doc = fitz.open(str(pdf_path))
    pix = doc[0].get_pixmap(matrix=fitz.Matrix(DPI / 72, DPI / 72))
    doc.close()
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    if img.shape[2] == 4:
        img = cv2.cvtColor(img, cv2.COLOR_RGBA2BGR)
    else:
        img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR).copy()
    H, W = img.shape[:2]

    body = _detect_body_rect(img)
    body_cx = (body[0] + body[2]) / 2
    body_cy = (body[1] + body[3]) / 2

    # Offset calibración por ficha (px nativos del render)
    cal = _load_cal().get(ambito["etiqueta"], {"dx": 0, "dy": 0})
    offset_dx = cal.get("dx", 0)
    offset_dy = cal.get("dy", 0)

    def utm_to_px(ux, uy):
        dx_m = ux - cx_utm
        dy_m = uy - cy_utm   # Y crece al norte
        px = body_cx + dx_m * PX_PER_M + offset_dx
        py = body_cy - dy_m * PX_PER_M + offset_dy
        return int(round(px)), int(round(py))

    pts_px = [utm_to_px(x, y) for x, y in polygon_utm]
    poly_np = np.array(pts_px, dtype=np.int32)

    # Dibujar: outline + relleno translúcido
    overlay = img.copy()
    cv2.fillPoly(overlay, [poly_np], color=(0, 0, 255))
    img_out = cv2.addWeighted(overlay, 0.30, img, 0.70, 0)
    cv2.polylines(img_out, [poly_np], isClosed=True, color=(0, 0, 255), thickness=5)

    ok, buf = cv2.imencode(".png", img_out)
    if not ok:
        return None
    return {
        "png_bytes": buf.tobytes(),
        "ambito_etiqueta": ambito["etiqueta"],
        "ambito_categoria": ambito.get("categoria"),
        "body_rect": list(body),
        "cal_offset": [offset_dx, offset_dy],
        "centroid_utm": [cx_utm, cy_utm],
        "width": W,
        "height": H,
        "m_per_px": 1.0 / PX_PER_M,
    }


def render_and_cache(filename: str, rc14: str, polygon_utm: list) -> Path | None:
    """Render + cache en disco. Devuelve path al PNG."""
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", f"{rc14}__{filename.rsplit('.pdf',1)[0]}")
    out = RENDER_CACHE_DIR / f"{safe}.png"
    if out.exists() and out.stat().st_size > 1024:
        return out
    res = render_with_overlay(filename, polygon_utm)
    if not res:
        return None
    out.write_bytes(res["png_bytes"])
    return out
