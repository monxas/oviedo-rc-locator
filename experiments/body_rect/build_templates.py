"""Paso 2: construye templates de 4 esquinas para el método `template`.

Lee scan_fallback.json + annot_selection.json y elige hojas de referencia
(una SU y una SNU) cuya area_frac esté cerca de la mediana, evitando las
del test set. Templates a $BODY_DETECT_TEMPLATE_CACHE.
"""
import json
from pathlib import Path

from _setup import setup_paths

setup_paths()
import body_detect  # noqa: E402

HERE = Path(__file__).resolve().parent
SCAN = HERE / "scan_fallback.json"
SELECTION = HERE / "annot_selection.json"
OUT = HERE / "template_refs.json"


def pick(kind, target_af, scan, sel):
    cands = [d for d in scan if d.get("rect") and d["kind"] == kind and d["pdf"] not in sel]
    norms = []
    for d in cands:
        af = (d["rect"][2] * d["rect"][3]) / (d["W"] * d["H"])
        norms.append((abs(af - target_af), d["pdf"], af))
    norms.sort()
    return norms[0]


def main():
    from oviedo_rc.config import CACHE_DIR
    cache = Path(CACHE_DIR)

    scan = json.load(open(SCAN))
    sel = set(json.load(open(SELECTION))) if SELECTION.exists() else set()

    # SU median area_frac ≈ 0.826; SNU median ≈ 0.847 (medido 2026-05-16)
    ref_su = pick("SU", 0.826, scan, sel)
    ref_snu = pick("SNU", 0.847, scan, sel)
    print(f"Ref SU:  {ref_su[1]}  (area_frac={ref_su[2]:.4f})")
    print(f"Ref SNU: {ref_snu[1]}  (area_frac={ref_snu[2]:.4f})")

    OUT.write_text(json.dumps({"su": ref_su[1], "snu": ref_snu[1]}, indent=2))

    body_detect.build_templates(str(cache / ref_su[1]), kind="su")
    body_detect.build_templates(str(cache / ref_snu[1]), kind="snu")

    print(f"\nTemplates en {body_detect._TEMPLATE_CACHE_DIR}")
    for f in sorted(body_detect._TEMPLATE_CACHE_DIR.iterdir()):
        if f.suffix == ".png":
            print(f"  {f.name}  {f.stat().st_size} bytes")


if __name__ == "__main__":
    main()
