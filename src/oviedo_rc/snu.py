"""SNU (Suelo No Urbanizable): mapping UTM → hoja PLANO_<letter>_<num>.pdf.

Cubre las 41.967 RCs (78%) sin cobertura de SU. El grid SNU es 9 columnas
(1-9) × 10 filas (A-J) sobre el bbox UTM aproximado del municipio.

Calibración inicial: bbox optimizado para maximizar match con las 61 hojas
reales disponibles, sobre 53.804 RCs de coords_local.json.
  hits = 49.988 (93%)
  miss = 3.816 (7%)

Pendiente: refinar bbox vía feature-matching del Mapa Guía SNU contra WMS.
"""
import json
import os
import re
from pathlib import Path

from .config import CACHE_DIR, HTTP_HEADERS
from .http_utils import http_get, fetch


# ---------- Calibración del grid SNU (cargada desde data/snu_grid.json) ----------
def _load_grid():
    # Busca data/snu_grid.json relativo al repo (..../oviedo_rc/snu.py → ../../data/)
    here = Path(__file__).resolve()
    candidates = [
        here.parent.parent.parent / "data" / "snu_grid.json",
        here.parent.parent / "data" / "snu_grid.json",
    ]
    for p in candidates:
        if p.exists():
            return json.loads(p.read_text())
    # Defaults conservadores
    return {
        "x0": 252290.93, "ymax": 4811487.92,
        "width": 29018.81, "height": 16194.95,
        "cols": 9, "rows": 10, "letters": "ABCDEFGHIJ",
    }

_GRID = _load_grid()
SNU_X0 = _GRID["x0"]
SNU_YMAX = _GRID["ymax"]
SNU_W = _GRID["width"]
SNU_H = _GRID["height"]
SNU_COLS = _GRID["cols"]
SNU_ROWS = _GRID["rows"]
SNU_CELL_W = SNU_W / SNU_COLS
SNU_CELL_H = SNU_H / SNU_ROWS
SNU_LETTERS = _GRID["letters"]

SNU_LIST_URL = (
    "https://www.oviedo.es/vive/urbanismo-e-infraestructuras/pgou/"
    "ficheros-pdf-suelo-no-urbanizable"
)
SNU_PORTLET = (
    "_com_liferay_document_library_web_portlet_IGDisplayPortlet"
    "_INSTANCE_J556oMSZtTY5_cur"
)
SNU_PORTLET_PAGES = 4
SNU_SHEETS_FILE = CACHE_DIR / "sheets_snu.json"

_BROWSER_UA = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) "
                  "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
}

_LINK_RE = re.compile(
    r'href="(/documents/35127/[^"]+)"\s*class="card-title"[^>]*>\s*'
    r'PLANO_([A-J])_(\d+)\.pdf',
    re.IGNORECASE,
)


def infer_snu_cell(utm_x: float, utm_y: float) -> tuple[int, str] | None:
    """Devuelve (col, letra) del grid SNU para (utm_x, utm_y) o None si fuera."""
    col = int((utm_x - SNU_X0) // SNU_CELL_W) + 1
    row = int((SNU_YMAX - utm_y) // SNU_CELL_H)
    if not (1 <= col <= SNU_COLS):
        return None
    if not (0 <= row < SNU_ROWS):
        return None
    return col, SNU_LETTERS[row]


def infer_snu_sheet(utm_x: float, utm_y: float) -> str | None:
    """Devuelve `PLANO_<L>_<N>.pdf` para coords UTM. None si fuera del grid."""
    res = infer_snu_cell(utm_x, utm_y)
    if not res:
        return None
    col, letter = res
    return f"PLANO_{letter}_{col}.pdf"


def _strip_thumbnail(url: str) -> str:
    return re.sub(r"[?&]documentThumbnail=\d+", "", url)


def get_snu_sheet_listing() -> dict[str, str]:
    """Devuelve {sheet_name: url}. Cacheado en sheets_snu.json."""
    if SNU_SHEETS_FILE.exists():
        data = json.loads(SNU_SHEETS_FILE.read_text())
        if isinstance(data, dict) and "sheets" in data:
            return data["sheets"]
        return data
    from urllib.parse import urljoin
    sheets: dict[str, str] = {}
    for cur in range(1, SNU_PORTLET_PAGES + 1):
        url = f"{SNU_LIST_URL}?{SNU_PORTLET}={cur}"
        html = http_get(url, headers=_BROWSER_UA, timeout=60).text
        for m in _LINK_RE.finditer(html):
            href = m.group(1)
            letter = m.group(2).upper()
            num = int(m.group(3))
            key = f"PLANO_{letter}_{num}.pdf"
            sheets.setdefault(key, urljoin("https://www.oviedo.es", href))
    SNU_SHEETS_FILE.write_text(json.dumps({"sheets": sheets}, indent=2))
    return sheets


def list_local_snu_sheets() -> set[str]:
    """Hojas SNU presentes en cache local."""
    out = set()
    for f in os.listdir(CACHE_DIR):
        if re.match(r"PLANO_[A-J]_\d+\.pdf$", f):
            out.add(f)
    return out


def fetch_snu_sheet_pdf(sheet_name: str) -> Path:
    """Path local al PDF, descargándolo si hace falta."""
    dest = CACHE_DIR / sheet_name
    if dest.exists() and dest.stat().st_size > 1024:
        with dest.open("rb") as f:
            if f.read(5) == b"%PDF-":
                return dest
    sheets = get_snu_sheet_listing()
    url = sheets.get(sheet_name)
    if not url:
        raise FileNotFoundError(f"sheet not in SNU listing: {sheet_name}")
    url = _strip_thumbnail(url)
    fetch(url, dest, expected_type="application/pdf", headers=_BROWSER_UA)
    return dest


def cell_bbox_utm(col: int, letter: str) -> tuple[float, float, float, float]:
    """Bbox UTM (xmin, ymin, xmax, ymax) de la celda (col, letter) del grid SNU."""
    row = SNU_LETTERS.index(letter)
    x0 = SNU_X0 + (col - 1) * SNU_CELL_W
    y_top = SNU_YMAX - row * SNU_CELL_H
    y_bot = y_top - SNU_CELL_H
    x1 = x0 + SNU_CELL_W
    return x0, y_bot, x1, y_top


# Coeficientes del body cartográfico dentro del render del PDF SNU
# (verificado visualmente en PLANO_G_6 a 120 dpi · 2840×1918)
SNU_BODY_X0_FRAC = 0.005
SNU_BODY_Y0_FRAC = 0.005
SNU_BODY_X1_FRAC = 0.950   # cajetín derecho ocupa ~5%
SNU_BODY_Y1_FRAC = 0.910   # banda inferior ocupa ~9%


def overlay_polygon(sheet_name: str, polygon_utm: list[tuple[float, float]],
                     dpi: int = 120):
    """Renderiza la hoja SNU y superpone polígono UTM. Devuelve np.ndarray BGR.

    Calidad: ~regular (bbox del grid asumido uniforme; cajetín fijo). Suficiente
    para localización aproximada; no pixel-precise.
    """
    import cv2
    import numpy as np
    from . import render as render_mod
    m = re.match(r"PLANO_([A-J])_(\d+)\.pdf$", sheet_name)
    if not m:
        return None
    letter = m.group(1)
    col = int(m.group(2))
    x0, ymin, x1, y_top = cell_bbox_utm(col, letter)

    pdf_path = fetch_snu_sheet_pdf(sheet_name)
    img, _, _ = render_mod.render_pdf_page(pdf_path, dpi=dpi)
    H, W = img.shape[:2]
    bx0 = int(W * SNU_BODY_X0_FRAC)
    by0 = int(H * SNU_BODY_Y0_FRAC)
    bx1 = int(W * SNU_BODY_X1_FRAC)
    by1 = int(H * SNU_BODY_Y1_FRAC)

    def utm_to_px(x, y):
        # x_frac: 0 en x0, 1 en x1
        x_frac = (x - x0) / (x1 - x0)
        # y_frac: 0 en y_top (norte), 1 en ymin (sur)
        y_frac = (y_top - y) / (y_top - ymin)
        px = bx0 + x_frac * (bx1 - bx0)
        py = by0 + y_frac * (by1 - by0)
        return int(px), int(py)

    poly_px = [utm_to_px(x, y) for x, y in polygon_utm]
    return render_mod.draw_polygon(img.copy(), poly_px,
                                    color=(0, 0, 255), thickness=4)


def resolve_snu_sheet(utm_x: float, utm_y: float) -> str | None:
    """Devuelve hoja SNU **existente** más probable para (utm_x, utm_y).

    1) Intenta la celda directa.
    2) Si no existe, busca vecinas (radio 1, luego 2) por distancia Chebyshev.
    """
    res = infer_snu_cell(utm_x, utm_y)
    if not res:
        return None
    col, letter = res
    available = {
        (int(m.group(2)), m.group(1))
        for f in list_local_snu_sheets()
        if (m := re.match(r"PLANO_([A-J])_(\d+)\.pdf$", f))
    }
    # Si no hay locales, asume catálogo completo (61 hojas) — el caller
    # puede fetch on-demand.
    if not available:
        try:
            listing = get_snu_sheet_listing()
            available = {
                (int(m.group(2)), m.group(1))
                for k in listing
                if (m := re.match(r"PLANO_([A-J])_(\d+)\.pdf$", k))
            }
        except Exception:
            return None
    if (col, letter) in available:
        return f"PLANO_{letter}_{col}.pdf"
    # Busca vecino más cercano por Chebyshev (luego euclídea como tie-break)
    li = SNU_LETTERS.index(letter)
    best = None
    for (c, L) in available:
        ri = SNU_LETTERS.index(L)
        d = max(abs(c - col), abs(ri - li))
        e = (c - col) ** 2 + (ri - li) ** 2
        key = (d, e)
        if best is None or key < best[0]:
            best = (key, c, L)
    if best is None:
        return None
    _, c, L = best
    return f"PLANO_{L}_{c}.pdf"
