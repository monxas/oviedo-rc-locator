"""Recalibra `data/calibration_fichas.json` desde `data/validator_labels_fichas.json`.

Modelo (paralelo a recalibrate.py):
  Cada label `accept` guarda `cal_dxdy` (offset aplicado al renderizar el
  polígono) y `dxdy` (drag user). La cal óptima por ámbito (etiqueta) es:

      offset_estimate = cal_dxdy + drag

  (Drags son ya en ficha-native px — no hay downscale display→native como
  en el panel PGOU; el render se sirve a 200dpi crudo.)

  offset[etiqueta] = MEDIAN(estimates de todos los labels del ámbito).

Salida:
  {
    "<etiqueta>": {"dx": int, "dy": int, "n_labels": int},
    ...
  }

  `ficha_plano._load_cal()` lee este JSON y aplica `offset_dx/offset_dy`
  en `render_with_overlay` (lazy mtime reload).

Buckets sin labels → ausentes. Sin offset == 0,0 implícito.
"""
import json
import os
from collections import defaultdict
from pathlib import Path
from statistics import median

ROOT = Path.home() / "oviedo-rc-locator"
LABELS_FILE = ROOT / "data" / "validator_labels_fichas.json"
CAL_FILE = ROOT / "data" / "calibration_fichas.json"


def main():
    if not LABELS_FILE.exists():
        print(f"NO existe {LABELS_FILE} — nada que hacer.")
        return
    labels = json.loads(LABELS_FILE.read_text(encoding="utf-8"))
    print(f"labels totales: {len(labels)}")

    by_amb = defaultdict(list)
    for l in labels:
        et = l.get("etiqueta")
        if not et:
            continue
        cal_dx, cal_dy = l.get("cal_dxdy", [0, 0])
        drag_dx, drag_dy = l.get("dxdy", [0, 0])
        by_amb[et].append((cal_dx + drag_dx, cal_dy + drag_dy))

    out = {}
    for et, ests in by_amb.items():
        if not ests:
            continue
        dx_med = int(round(median(e[0] for e in ests)))
        dy_med = int(round(median(e[1] for e in ests)))
        out[et] = {
            "dx": dx_med,
            "dy": dy_med,
            "n_labels": len(ests),
        }

    print(f"ámbitos con cal: {len(out)}")
    for et, v in sorted(out.items(), key=lambda kv: -kv[1]["n_labels"])[:15]:
        print(f"  {et:50s}  dx={v['dx']:+4d} dy={v['dy']:+4d}  n={v['n_labels']}")

    tmp = CAL_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False, indent=2))
    os.replace(tmp, CAL_FILE)
    print(f"\nEscrito: {CAL_FILE} ({CAL_FILE.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
