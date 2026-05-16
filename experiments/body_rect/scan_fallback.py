"""Paso 0: escanea todas las hojas cacheadas con detect_heuristic.

Output: scan_fallback.json con {pdf, kind, W, H, rect, fallback} por hoja.
Útil para identificar candidatos a anotación (sheets con fallback al 5%).
"""
import json
import os
import sys
from pathlib import Path

from _setup import setup_paths

setup_paths()
import cv2  # noqa: E402,F401
from oviedo_rc import render  # noqa: E402
from oviedo_rc.config import CACHE_DIR  # noqa: E402

OUT = Path(__file__).parent / "scan_fallback.json"


def main():
    cache = Path(CACHE_DIR)
    su_catalog = set(json.load(open(cache / "sheets.json")))
    pdfs = sorted(f for f in os.listdir(cache) if f.startswith("PLANO_") and f.endswith(".pdf"))
    results = []
    for i, f in enumerate(pdfs):
        pdf_path = cache / f
        try:
            img, _, _ = render.render_pdf_page(str(pdf_path))
            H, W = img.shape[:2]
            rect = render.detect_body_rect(img)
            x, y, w, h = rect
            is_fallback = (abs(x - int(W * 0.05)) <= 1 and abs(y - int(H * 0.05)) <= 1
                           and abs(w - int(W * 0.9)) <= 1 and abs(h - int(H * 0.9)) <= 1)
            kind = "SU" if f in su_catalog else "SNU"
            results.append({"pdf": f, "kind": kind, "W": W, "H": H,
                            "rect": list(rect), "fallback": is_fallback})
            if i % 20 == 0:
                print(f"  {i}/{len(pdfs)} {f} kind={kind} fallback={is_fallback}", flush=True)
        except Exception as e:
            print(f"  ERR {f}: {e}", flush=True)
            results.append({"pdf": f, "error": str(e)})

    OUT.write_text(json.dumps(results, indent=2))
    n_fb = sum(1 for r in results if r.get("fallback"))
    print(f"\nTotal: {len(results)}, fallback: {n_fb}")
    print(f"Wrote {OUT}")


if __name__ == "__main__":
    main()
