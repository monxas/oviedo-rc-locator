"""Scrape + descarga de los PDFs SNU + Mapa Guía."""
import json
import re
import sys
from pathlib import Path
from urllib.parse import urljoin

import requests

CACHE = Path.home() / ".cache" / "oviedo_rc"
CACHE.mkdir(parents=True, exist_ok=True)
SHEETS_FILE_SNU = CACHE / "sheets_snu.json"

BASE = "https://www.oviedo.es"
LIST_URL = "https://www.oviedo.es/vive/urbanismo-e-infraestructuras/pgou/ficheros-pdf-suelo-no-urbanizable"
PORTLET = "_com_liferay_document_library_web_portlet_IGDisplayPortlet_INSTANCE_J556oMSZtTY5_cur"
PAGES = 4

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) "
                  "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
}

# El href apunta a UUID y el nombre PDF aparece justo después (whitespace+newlines)
LINK_RE = re.compile(
    r'href="(/documents/35127/[^"]+)"\s*class="card-title"[^>]*>\s*PLANO_([A-Z])_(\d+)\.pdf',
    re.IGNORECASE
)
MAPA_GUIA_RE = re.compile(
    r'href="(/documents/[^"]+MAPA[^"]*GUIA[^"]+\.pdf[^"]*)"',
    re.IGNORECASE
)


def scrape():
    sheets: dict[str, str] = {}
    mapa_guia: str | None = None
    for cur in range(1, PAGES + 1):
        url = f"{LIST_URL}?{PORTLET}={cur}"
        print(f"  → page {cur}")
        r = requests.get(url, headers=HEADERS, timeout=30)
        r.raise_for_status()
        html = r.text
        for m in LINK_RE.finditer(html):
            href, letter, num = m.group(1), m.group(2).upper(), int(m.group(3))
            key = f"PLANO_{letter}_{num}.pdf"
            sheets.setdefault(key, urljoin(BASE, href))
        if mapa_guia is None:
            mg = MAPA_GUIA_RE.search(html)
            if mg:
                mapa_guia = urljoin(BASE, mg.group(1))
    return sheets, mapa_guia


def _clean_url(url: str) -> str:
    """Quita query params que sirven thumbnail (queremos el PDF original)."""
    return re.sub(r'[?&]documentThumbnail=\d+', '', url)


def download(sheets: dict[str, str], dest: Path) -> dict[str, str]:
    """Descarga los PDFs nuevos a dest. Devuelve {nombre: path}."""
    saved: dict[str, str] = {}
    for name, url in sorted(sheets.items()):
        out = dest / name
        if out.exists() and out.stat().st_size > 1024:
            # ya existente, verifica que sea PDF (no PNG thumbnail viejo)
            with out.open("rb") as f:
                head = f.read(5)
            if head == b"%PDF-":
                saved[name] = str(out)
                continue
            else:
                print(f"  (re-download {name}: era {head!r})")
        url_pdf = _clean_url(url)
        print(f"  ↓ {name}", end=" ", flush=True)
        r = requests.get(url_pdf, headers=HEADERS, timeout=60, allow_redirects=True)
        if r.status_code != 200 or not r.content.startswith(b"%PDF"):
            print(f"FAIL ({r.status_code} ct={r.headers.get('content-type','?')[:30]})")
            continue
        out.write_bytes(r.content)
        print(f"{len(r.content)//1024} KB")
        saved[name] = str(out)
    return saved


def main():
    print("Scraping listing SNU…")
    sheets, mapa_guia = scrape()
    print(f"  encontradas: {len(sheets)} hojas SNU")
    if mapa_guia: print(f"  mapa guía: {mapa_guia}")

    SHEETS_FILE_SNU.write_text(json.dumps({
        "sheets": sheets,
        "mapa_guia": mapa_guia,
    }, indent=2, ensure_ascii=False))
    print(f"  guardado: {SHEETS_FILE_SNU}")

    print("\nDescargando PDFs…")
    saved = download(sheets, CACHE)
    print(f"\nTotal descargados: {len(saved)}/{len(sheets)}")

    if mapa_guia:
        mg_path = CACHE / "snu_mapa_guia.pdf"
        if not mg_path.exists():
            r = requests.get(mapa_guia, headers=HEADERS, timeout=60, allow_redirects=True)
            if r.status_code == 200 and r.content.startswith(b"%PDF"):
                mg_path.write_bytes(r.content)
                print(f"Mapa guía guardado: {mg_path}")


if __name__ == "__main__":
    main()
