"""Paso 1: selecciona 15 hojas para anotación (plan B).

Lee scan_fallback.json del paso 0 y elige:
  - 5 SU random
  - 5 SNU random
  - 5 SU con menor area_frac detectada (sospechosos de mal frame)

Output: annot_selection.json — lista de PDFs.
"""
import json
import random
import statistics
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCAN = HERE / "scan_fallback.json"
OUT = HERE / "annot_selection.json"


def main():
    data = json.load(open(SCAN))
    data = [d for d in data if "rect" in d]

    norms = []
    for d in data:
        x, y, w, h = d["rect"]
        W, H = d["W"], d["H"]
        norms.append({
            "pdf": d["pdf"],
            "kind": d["kind"],
            "area_frac": (w * h) / (W * H),
            "ratio": w / max(1, h),
        })

    afs = sorted(n["area_frac"] for n in norms)
    print("area_frac: min=%.3f median=%.3f max=%.3f" % (afs[0], statistics.median(afs), afs[-1]))
    rats = sorted(n["ratio"] for n in norms)
    print("ratio:     min=%.3f median=%.3f max=%.3f" % (rats[0], statistics.median(rats), rats[-1]))

    norms_sorted = sorted(norms, key=lambda n: n["area_frac"])
    su = [n for n in norms if n["kind"] == "SU"]
    snu = [n for n in norms if n["kind"] == "SNU"]
    random.seed(42)
    su_sample = random.sample(su, 5)
    snu_sample = random.sample(snu, 5)

    selected = [n["pdf"] for n in su_sample + snu_sample] + [n["pdf"] for n in norms_sorted[:5]]
    selected = list(dict.fromkeys(selected))
    print("\nSELECTION (%d hojas):" % len(selected))
    for s in selected:
        print(" ", s)

    OUT.write_text(json.dumps(selected, indent=2))
    print(f"\nWrote {OUT}")


if __name__ == "__main__":
    main()
