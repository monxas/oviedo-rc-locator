"""Parsea los 234 PDFs de fichas de Oviedo y cachea cada uno como JSON.

Output: ~/.cache/oviedo_rc/fichas_data/<basename_sin_pdf>.json
Idempotente: si el JSON existe, lo sobrescribe (re-parse barato).
"""
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path.home() / "oviedo-rc-locator" / "src"))
from oviedo_rc.oviedo_pdf_parse import parse_ficha_pdf

CACHE = Path.home() / ".cache" / "oviedo_rc"
PDF_DIR = CACHE / "fichas"
OUT_DIR = CACHE / "fichas_data"
OUT_DIR.mkdir(parents=True, exist_ok=True)

KEY_FIELDS = [
    "clave", "numero", "tipo_ambito", "nombre", "hoja_pgou",
    "origen_ambito", "sistema_actuacion", "iniciativa",
    "instrumento_planeamiento_definitiva",
    "expropiacion", "cesiones", "urbanizacion", "edificacion",
    "total_suelo_publico_m2", "total_suelo_privado_m2",
    "aprovechamiento_medio_codigo", "aprovechamiento_medio_uah",
    "superficie_total_ambito_m2", "indice_edif_bruta",
    "uso_global_predominante", "altura_maxima_plantas",
    "viviendas_proteccion_pct", "n_aprox_viv_proteccion",
    "viviendas_construibles_total", "densidad_bruta_viv_ha",
    "superficie_media_viv_m2",
]


def main():
    pdfs = sorted(PDF_DIR.glob("*.pdf"))
    print(f"PDFs a procesar: {len(pdfs)}", flush=True)
    filled = Counter()
    errored: list[tuple[str, str, str]] = []
    for i, pdf in enumerate(pdfs, 1):
        stem = pdf.stem
        out = OUT_DIR / f"{stem}.json"
        if i % 50 == 0:
            print(f"  [{i}/{len(pdfs)}]", flush=True)
        try:
            data = parse_ficha_pdf(pdf)
        except Exception as e:
            errored.append((pdf.name, type(e).__name__, str(e)[:80]))
            continue
        out.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        for k in KEY_FIELDS:
            v = data.get(k)
            if v not in (None, "", []):
                filled[k] += 1
    n = len(pdfs)
    print(f"\nProcesados: {n}  Errores: {len(errored)}")
    if errored:
        print("Errores:")
        for a, t, msg in errored[:20]:
            print(f"  {a}: {t}: {msg}")
    print(f"\nCobertura de campos (n/{n}):")
    for k in KEY_FIELDS:
        c = filled[k]
        print(f"  {k:42s}  {c:4d}  ({100*c/max(n,1):.0f}%)")


if __name__ == "__main__":
    main()
