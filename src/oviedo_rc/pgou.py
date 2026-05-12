"""Cliente del listado y descarga de hojas del PGOU 1:1000 (Ayuntamiento).

Multi-concejo (PR3): las funciones aceptan un `Concejo` opcional; si no se
pasa, usa OVIEDO (backwards-compat). El cache `sheets.json` está
compartido para OVIEDO; cuando haya >1 concejo, se separará por slug.
"""
import json
import re

from .config import CACHE_DIR, SHEETS_FILE, PGOU_EXPECTED_SHEETS_MIN
from .concejo import OVIEDO, Concejo
from .http_utils import http_get, fetch


def _sheets_file_for(concejo: Concejo):
    """Path del cache de listado por concejo. OVIEDO usa el path legacy."""
    if concejo.id_ine == OVIEDO.id_ine:
        return SHEETS_FILE
    return CACHE_DIR / f"sheets_{concejo.slug}.json"


def get_sheet_listing(concejo: Concejo | None = None):
    """Devuelve dict {sheet_name: url} para el concejo dado. Cacheado en disco."""
    c = concejo or OVIEDO
    if c.pgou_su is None:
        raise RuntimeError(f"Concejo {c.nombre} sin portlet SU configurado")
    cache_file = _sheets_file_for(c)
    if cache_file.exists():
        return json.loads(cache_file.read_text())
    sheets, raw_seen = {}, {}
    for cur in range(1, c.pgou_su.pages + 1):
        url = f"{c.pgou_su.url}?{c.pgou_su.instance}={cur}"
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
    cache_file.write_text(json.dumps(sheets, indent=2))
    return sheets


def fetch_sheet_pdf(sheet_name, concejo: Concejo | None = None):
    """Devuelve path local al PDF de una hoja, descargándolo si hace falta."""
    sheets = get_sheet_listing(concejo)
    url = sheets.get(sheet_name)
    if not url:
        raise FileNotFoundError(f"sheet not in listing: {sheet_name}")
    dest = CACHE_DIR / sheet_name
    fetch(url, dest, expected_type="application/pdf")
    return dest
