"""Configuración: paths, modelo geométrico, parámetros de la malla."""
import os
import re
from pathlib import Path

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

# ---------- PGOU Ayuntamiento ----------
PGOU_INDEX_URL = (
    "https://www.oviedo.es/documents/35127/1035865/MAPA_GUIA_1-1000.pdf/"
    "411239ce-7b61-46e6-814c-eaa7d573a452"
)
PGOU_LIST_URL = (
    "https://www.oviedo.es/vive/urbanismo-e-infraestructuras/pgou/ficheros-pdf-suelo-urbano"
)
PGOU_PORTLET = (
    "_com_liferay_document_library_web_portlet_IGDisplayPortlet_INSTANCE_7ckYazyK22lW_cur"
)
PGOU_PORTLET_PAGES = 8
PGOU_EXPECTED_SHEETS_MIN = 100

# ---------- Catastro ----------
CATASTRO_HOST = "ovc.catastro.meh.es"
CATASTRO_IP = "195.66.151.66"  # IP fija para evitar problemas de DNS

# ---------- Modelo geométrico de la malla del PGOU ----------
# Cells RECTANGULARES (~1036 × 695 m). Body físico cubre
# (CELL_W/2 + 2·MARG_X) × (CELL_H/2 + 2·MARG_Y) por cinta marginal.
# Ajustado por LSQ con 71 calibraciones manuales (mediana 4.5 m, p90 7.75 m).
MALLA_X0     = 253338.0196   # UTM X de la frontera oeste de col 0
MALLA_YMAX   = 4812335.9516  # UTM Y de la frontera norte de fila A
MALLA_CELL_W = 1036.1505
MALLA_CELL_H =  695.3860
MALLA_MARG_X =   12.5165
MALLA_MARG_Y =   10.9430
SUB_W = MALLA_CELL_W / 2
SUB_H = MALLA_CELL_H / 2
BODY_W_M = SUB_W + 2 * MALLA_MARG_X
BODY_H_M = SUB_H + 2 * MALLA_MARG_Y

# Convención del PGOU de Oviedo (verificada con 4 RCs reales):
SUB_CONVENTION = {"NW": "I", "NE": "II", "SW": "III", "SE": "IV"}
NS_THRESHOLD = 0.5000
EW_THRESHOLD = 0.5000

# Validación territorial: bbox del suelo urbano de Oviedo en UTM ETRS89 30N.
BBOX_OVIEDO = (253000, 4798000, 278000, 4815000)

# Bbox urbano efectivo (donde están las parcelas con datos catastrales).
URBAN_BBOX = (260000, 4801000, 275000, 4810000)

# ---------- Renderizado ----------
PDF_DPI = 300

# Patrón estricto de RC catastral: 14 chars (parcela) o 20 (con inmueble).
_RC14 = r"\d{7}[A-Z]{2}\d{4}[A-Z]"
_RC20 = _RC14 + r"\d{4}[A-Z]{2}"
RC_RE = re.compile(rf"^(?:{_RC14}|{_RC20})$")
