"""Cliente del listado y descarga de hojas del PGOU 1:1000 (Ayuntamiento)."""
import json
import re

from .config import (CACHE_DIR, SHEETS_FILE, PGOU_LIST_URL, PGOU_PORTLET,
                      PGOU_PORTLET_PAGES, PGOU_EXPECTED_SHEETS_MIN)
from .http_utils import http_get, fetch


def get_sheet_listing():
    """Devuelve dict {sheet_name: url}. Cacheado en sheets.json."""
    if SHEETS_FILE.exists():
        return json.loads(SHEETS_FILE.read_text())
    sheets, raw_seen = {}, {}
    for cur in range(1, PGOU_PORTLET_PAGES + 1):
        url = f"{PGOU_LIST_URL}?{PGOU_PORTLET}={cur}"
        html = http_get(url, timeout=60).text
        for m in re.finditer(
            r'href="(/documents/35127/[a-f0-9-]+)\?[^"]*"\s+'
            r'class="card-title"[^>]*>\s*(PLANO[^\s<]+\.pdf)',
            html,
        ):
            raw = m.group(2)
            norm = re.sub(r"^PLANO_+", "PLANO_", raw)
            norm = re.sub(r"[-_]+", "_", norm.replace("PLANO_", "", 1))
            norm = "PLANO_" + norm
            sheets.setdefault(norm, "https://www.oviedo.es" + m.group(1))
            raw_seen.setdefault(norm, []).append(raw)

    if len(sheets) < PGOU_EXPECTED_SHEETS_MIN:
        raise RuntimeError(
            f"Listado del Ayuntamiento incompleto: {len(sheets)} < "
            f"{PGOU_EXPECTED_SHEETS_MIN}"
        )
    SHEETS_FILE.write_text(json.dumps(sheets, indent=2))
    return sheets


def fetch_sheet_pdf(sheet_name):
    """Devuelve path local al PDF de una hoja, descargándolo si hace falta."""
    sheets = get_sheet_listing()
    url = sheets.get(sheet_name)
    if not url:
        raise FileNotFoundError(f"sheet not in listing: {sheet_name}")
    dest = CACHE_DIR / sheet_name
    fetch(url, dest, expected_type="application/pdf")
    return dest
