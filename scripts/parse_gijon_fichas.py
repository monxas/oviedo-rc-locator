"""Parsea todos los PDFs de fichas Gijón y cachea como JSON estructurado."""
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path.home() / "oviedo-rc-locator" / "src"))
from oviedo_rc.gijon_pdf_parse import parse_ficha_pdf

CACHE = Path.home() / ".cache" / "oviedo_rc" / "gijon"
PDF_DIR = CACHE / "fichas_pdf"
OUT_DIR = CACHE / "fichas_data"
OUT_DIR.mkdir(parents=True, exist_ok=True)

KEY_FIELDS = [
    "uso_predominante", "clase_suelo", "categoria_suelo",
    "planeamiento_desarrollo", "iniciativa", "sistema_actuacion",
    "instrumento_gestion", "ordenanza",
    "superficie_ambito_m2", "superficie_neta_m2",
    "edificabilidad_m2m2", "densidad_viv_ha", "viviendas_estimadas",
    "viviendas_protegidas_pct", "altura_max_plantas",
    "sup_residencial_m2", "sup_terciario_m2", "sup_industrial_m2",
    "sup_viario_m2", "sup_espacios_libres_m2",
    "sup_dotacional_publico_m2", "sup_dotacional_privado_m2",
    "sup_edificable_max_m2",
]


def main():
    pdfs = sorted(PDF_DIR.glob("*.pdf"))
    print(f"PDFs a procesar: {len(pdfs)}", flush=True)
    filled = Counter()
    errored = []
    for i, pdf in enumerate(pdfs, 1):
        ambito_id = pdf.stem
        out = OUT_DIR / f"{ambito_id}.json"
        if i % 25 == 0:
            print(f"  [{i}/{len(pdfs)}]", flush=True)
        try:
            data = parse_ficha_pdf(pdf)
        except Exception as e:
            errored.append((ambito_id, type(e).__name__, str(e)[:80]))
            continue
        # quita raw_text para ahorrar espacio en cache
        data.pop("raw_text_decoded", None)
        out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        for k in KEY_FIELDS:
            if data.get(k) not in (None, ""):
                filled[k] += 1
    n = len(pdfs)
    print(f"\nProcesados: {n}  Errores: {len(errored)}")
    if errored:
        print("Errores:")
        for a, t, msg in errored[:10]:
            print(f"  {a}: {t}: {msg}")
    print(f"\nCobertura de campos (n/{n}):")
    for k in KEY_FIELDS:
        c = filled[k]
        print(f"  {k:36s}  {c:4d}  ({100*c/max(n,1):.0f}%)")


if __name__ == "__main__":
    main()
