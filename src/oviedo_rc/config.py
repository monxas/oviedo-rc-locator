"""Configuración: paths, modelo geométrico, parámetros de la malla.

NOTA PR3 (multi-concejo): los parámetros específicos de Oviedo (`MALLA_*`,
`SUB_CONVENTION`, `PGOU_LIST_URL`, `PGOU_PORTLET`, `BBOX_OVIEDO`,
`URBAN_BBOX`) son ahora *aliases* del concejo `OVIEDO` definido en
`concejo.py`. Se mantienen para no romper código externo, pero el código
nuevo debe consumir directamente `concejo.malla.*`, `concejo.pgou_su.*`,
etc., para soportar más concejos sin hardcodes.
"""
import os
import re
from pathlib import Path

from .concejo import OVIEDO

# ---------- Caché ----------
CACHE_DIR = Path(os.environ.get("OVIEDO_CACHE", "~/.cache/oviedo_rc")).expanduser()
PARCELS_DIR = CACHE_DIR / "parcels"
WMS_DIR = CACHE_DIR / "wms"
COORDS_FILE = CACHE_DIR / "coords_local.json"
SHEETS_FILE = CACHE_DIR / "sheets.json"
for d in (CACHE_DIR, PARCELS_DIR, WMS_DIR):
    d.mkdir(parents=True, exist_ok=True)

# ---------- HTTP ----------
HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (oviedo_rc/1.0)",
    "Referer": "https://www.oviedo.es/",
}
HTTP_TIMEOUT = 30
HTTP_RETRIES = 3

# ---------- PGOU Ayuntamiento (DEPRECATED — usar concejo.pgou_su.*) ----------
PGOU_INDEX_URL = (
    "https://www.oviedo.es/documents/35127/1035865/MAPA_GUIA_1-1000.pdf/"
    "411239ce-7b61-46e6-814c-eaa7d573a452"
)
PGOU_LIST_URL = OVIEDO.pgou_su.url
PGOU_PORTLET = OVIEDO.pgou_su.instance
PGOU_PORTLET_PAGES = OVIEDO.pgou_su.pages
PGOU_EXPECTED_SHEETS_MIN = 100

# ---------- Catastro ----------
CATASTRO_HOST = "ovc.catastro.meh.es"
CATASTRO_IP = "195.66.151.66"  # IP fija para evitar problemas de DNS

# ---------- Modelo geométrico de la malla del PGOU (DEPRECATED — usar concejo.malla.*) ----------
# Cells RECTANGULARES (~1036 × 695 m). Body físico cubre
# (CELL_W/2 + 2·MARG_X) × (CELL_H/2 + 2·MARG_Y) por cinta marginal.
# Ajustado por LSQ con 71 calibraciones manuales (mediana 4.5 m, p90 7.75 m).
MALLA_X0     = OVIEDO.malla.x0
MALLA_YMAX   = OVIEDO.malla.ymax
MALLA_CELL_W = OVIEDO.malla.cell_w
MALLA_CELL_H = OVIEDO.malla.cell_h
MALLA_MARG_X = OVIEDO.malla.marg_x
MALLA_MARG_Y = OVIEDO.malla.marg_y
SUB_W = MALLA_CELL_W / 2
SUB_H = MALLA_CELL_H / 2
BODY_W_M = SUB_W + 2 * MALLA_MARG_X
BODY_H_M = SUB_H + 2 * MALLA_MARG_Y

# Convención del PGOU de Oviedo (verificada con 4 RCs reales):
SUB_CONVENTION = OVIEDO.malla.sub_convention
NS_THRESHOLD = OVIEDO.malla.ns_threshold
EW_THRESHOLD = OVIEDO.malla.ew_threshold

# Validación territorial: bbox del suelo urbano de Oviedo en UTM ETRS89 30N.
BBOX_OVIEDO = OVIEDO.bbox_utm

# Bbox urbano efectivo (donde están las parcelas con datos catastrales).
URBAN_BBOX = OVIEDO.urban_bbox

# ---------- Renderizado ----------
PDF_DPI = 300

# Patrón estricto de RC catastral: 14 chars (parcela) o 20 (con inmueble).
_RC14 = r"\d{7}[A-Z]{2}\d{4}[A-Z]"
_RC20 = _RC14 + r"\d{4}[A-Z]{2}"
RC_RE = re.compile(rf"^(?:{_RC14}|{_RC20})$")
