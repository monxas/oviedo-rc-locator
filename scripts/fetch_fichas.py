"""Scrape + descarga de las Fichas de Ámbitos del PGOU de Oviedo."""
import json
import re
import sys
from pathlib import Path
from urllib.parse import urljoin

import requests

CACHE = Path.home() / ".cache" / "oviedo_rc"
FICHAS_DIR = CACHE / "fichas"
FICHAS_DIR.mkdir(parents=True, exist_ok=True)
LIST_FILE = CACHE / "fichas_listing.json"

BASE = "https://www.oviedo.es"
LIST_URL = "https://www.oviedo.es/vive/urbanismo-e-infraestructuras/pgou/fichas-de-ambitos"
PORTLET = "_com_liferay_document_library_web_portlet_IGDisplayPortlet_INSTANCE_OrjqGhMJUXbr_cur"
PAGES = 12

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) "
                  "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
}

LINK_RE = re.compile(
    r'href="(/documents/35127/[^"]+)"\s*class="card-title"[^>]*>\s*([^<\s][^<]*?\.pdf)',
    re.IGNORECASE
)

# Extrae tipo+código del nombre: ..._<TIPO>_<CODIGO>_Ficha_n_<NUM>_...
# TIPO: UG, AU, AA, AUS, PE, UG2, ...
META_RE = re.compile(
    r"_(UG2?|AU|AUS|AA|PE|SUNC|API|SR)_([A-Z0-9]+)_Ficha_n_(\d+)",
    re.IGNORECASE
)


def _clean_url(url: str) -> str:
    return re.sub(r"[?&]documentThumbnail=\d+", "", url)


def _parse_meta(filename: str) -> dict:
    """Devuelve {tipo, codigo, num} si el nombre matchea, si no {}."""
    m = META_RE.search(filename)
    if not m:
        return {}
    return {
        "tipo": m.group(1).upper(),
        "codigo": m.group(2).upper(),
        "num": int(m.group(3)),
    }


def scrape() -> dict:
    """Devuelve {filename: {url, tipo, codigo, num}}."""
    out: dict[str, dict] = {}
    for cur in range(1, PAGES + 1):
        url = f"{LIST_URL}?{PORTLET}={cur}"
        print(f"  → page {cur}", flush=True)
        r = requests.get(url, headers=HEADERS, timeout=30)
        r.raise_for_status()
        html = r.text
        new = 0
        for m in LINK_RE.finditer(html):
            href, fname = m.group(1), m.group(2).strip()
            if fname in out:
                continue
            meta = _parse_meta(fname)
            out[fname] = {
                "url": urljoin(BASE, href),
                **meta,
            }
            new += 1
        print(f"    +{new}  total={len(out)}", flush=True)
    return out


def download(listing: dict) -> int:
    saved = 0
    for fname, info in sorted(listing.items()):
        dest = FICHAS_DIR / fname
        if dest.exists() and dest.stat().st_size > 1024:
            with dest.open("rb") as f:
                if f.read(5) == b"%PDF-":
                    saved += 1
                    continue
        url = _clean_url(info["url"])
        try:
            r = requests.get(url, headers=HEADERS, timeout=60, allow_redirects=True)
        except Exception as e:
            print(f"  ✗ {fname}: {e}", flush=True)
            continue
        if r.status_code != 200 or not r.content.startswith(b"%PDF"):
            print(f"  ✗ {fname}: status={r.status_code} ct={r.headers.get('content-type','?')[:30]}", flush=True)
            continue
        dest.write_bytes(r.content)
        saved += 1
        if saved % 20 == 0:
            print(f"  ↓ {saved}/{len(listing)}", flush=True)
    return saved


def main():
    print("Scraping fichas de ámbitos…", flush=True)
    listing = scrape()
    print(f"\nTotal hojas: {len(listing)}", flush=True)
    LIST_FILE.write_text(json.dumps(listing, indent=2, ensure_ascii=False))
    print(f"  listing → {LIST_FILE}", flush=True)

    by_tipo: dict[str, int] = {}
    for v in listing.values():
        by_tipo[v.get("tipo", "?")] = by_tipo.get(v.get("tipo", "?"), 0) + 1
    print("  por tipo:", by_tipo, flush=True)

    print("\nDescargando…", flush=True)
    n = download(listing)
    print(f"\nTotal descargados: {n}/{len(listing)}")


if __name__ == "__main__":
    main()
