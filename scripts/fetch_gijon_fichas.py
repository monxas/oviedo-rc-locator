"""Descarga las fichas (HTML metadata + PDF cuando hay) de TODOS los ámbitos de Gijón.

Lee ambitos.json (output de fetch_gijon_kml.py), itera sobre cada `id`,
hace `fetch_ficha_meta()` (cachea HTML parsed), descarga el `ficha_pdf_url` si existe.

Output:
- ~/.cache/oviedo_rc/gijon/fichas_meta/<id>.json     (ya cacheado por fetch_ficha_meta)
- ~/.cache/oviedo_rc/gijon/fichas_pdf/<id>.pdf       (nuevo)

Resume: idempotente; no re-descarga lo que ya hay.
"""
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path.home() / "oviedo-rc-locator" / "src"))
from oviedo_rc import gijon  # noqa

CACHE = Path.home() / ".cache" / "oviedo_rc" / "gijon"
PDF_DIR = CACHE / "fichas_pdf"
PDF_DIR.mkdir(parents=True, exist_ok=True)


def safe_name(s: str) -> str:
    out = ""
    for c in s:
        out += c if c.isalnum() or c in "_-" else "_"
    return out[:200]


def main():
    ambitos = json.loads((CACHE / "ambitos.json").read_text())
    ids = sorted({a["id"] for a in ambitos})
    print(f"Ámbitos únicos: {len(ids)}")

    n_pdf_ok = n_pdf_skip = n_pdf_404 = n_pdf_err = n_no_pdf = n_meta_err = 0
    for i, ambito_id in enumerate(ids, 1):
        if i % 25 == 0:
            print(f"  [{i}/{len(ids)}] pdf_ok={n_pdf_ok} skip={n_pdf_skip} 404={n_pdf_404} err={n_pdf_err} no_pdf={n_no_pdf}", flush=True)
        try:
            meta = gijon.fetch_ficha_meta(ambito_id)
        except Exception as e:
            n_meta_err += 1
            print(f"  ! {ambito_id}: meta fail {type(e).__name__}", flush=True)
            continue

        pdf_url = meta.get("ficha_pdf_url")
        if not pdf_url:
            n_no_pdf += 1
            continue

        # nombre seguro
        local = PDF_DIR / f"{safe_name(ambito_id)}.pdf"
        if local.exists() and local.stat().st_size > 1024:
            with local.open("rb") as f:
                if f.read(5) == b"%PDF-":
                    n_pdf_skip += 1
                    continue

        try:
            req = urllib.request.Request(pdf_url, headers={"User-Agent": "iarq-locator/1.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                data = r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                n_pdf_404 += 1
            else:
                n_pdf_err += 1
                print(f"  ! {ambito_id}: pdf http_{e.code}", flush=True)
            continue
        except Exception as e:
            n_pdf_err += 1
            print(f"  ! {ambito_id}: pdf fail {type(e).__name__}", flush=True)
            continue

        if not data.startswith(b"%PDF"):
            n_pdf_err += 1
            continue
        local.write_bytes(data)
        n_pdf_ok += 1
        time.sleep(0.05)  # gentle

    print()
    print(f"Resumen: total={len(ids)}  pdf_descargados={n_pdf_ok}  ya_cacheados={n_pdf_skip}")
    print(f"         sin_pdf_url={n_no_pdf}  pdf_404={n_pdf_404}  pdf_err={n_pdf_err}  meta_err={n_meta_err}")
    print(f"PDFs en: {PDF_DIR}  ({sum(f.stat().st_size for f in PDF_DIR.glob('*.pdf'))//1024} KB total)")


if __name__ == "__main__":
    main()
