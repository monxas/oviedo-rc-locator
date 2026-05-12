"""Recalibra `calibration_offsets.json` desde `validator_labels.json`.

LÓGICA CORRECTA (post 2026-05-12):
  Cada label `accept` guarda `cal_dxdy` (cal aplicada en el momento) y
  `dxdy` (drag user, display px). Por tanto la cal "óptima" estimada por
  ese label es:

      cal_estimate = cal_dxdy + drag_native
                                 ^─ dxdy × SCALE_DISPLAY_TO_NATIVE

  (snap_dxdy es ruido local del RC, NO se suma — la cal del bucket es lo
  común a todos los RCs del bucket; el snap absorbe variaciones por RC.)

  Cal nueva del bucket = MEDIAN(cal_estimates de los labels del bucket).

  Esto es ABSOLUTO, no acumulativo. La versión anterior hacía
  `new = old + median(drags)`, lo que aplicaba doble corrección porque
  los drags antiguos se midieron con offsets antiguos ya aplicados.

  Buckets sin labels → se mantienen como estaban (no se tocan).
"""
import json
import sys
from collections import defaultdict
from pathlib import Path
from statistics import median, stdev

ROOT = Path.home() / "oviedo-rc-locator"
sys.path.insert(0, str(ROOT / "src"))

from oviedo_rc.config import (
    MALLA_X0, MALLA_YMAX, MALLA_CELL_W, MALLA_CELL_H,
    NS_THRESHOLD, EW_THRESHOLD, SUB_CONVENTION, COORDS_FILE,
)

LABELS_VAL = ROOT / "data" / "validator_labels.json"
CAL_FILE = ROOT / "data" / "calibration_offsets.json"
SCALE_DISPLAY_TO_NATIVE = 2


def _coords():
    cache = Path.home() / ".cache" / "oviedo_rc"
    return json.loads((cache / COORDS_FILE.name).read_text(encoding="utf-8"))


COORDS = _coords()


def cellsub(rc14):
    rec = COORDS.get(rc14)
    if not rec:
        return None, None
    x = rec.get("x") if isinstance(rec, dict) else rec[0]
    y = rec.get("y") if isinstance(rec, dict) else rec[1]
    col = int((x - MALLA_X0) // MALLA_CELL_W)
    row = int((MALLA_YMAX - y) // MALLA_CELL_H)
    if not (0 <= row < 25):
        return None, None
    letter = "ABCDEFGHIJKLMNOPQRSTUVWXY"[row]
    x_in = (x - (MALLA_X0 + col * MALLA_CELL_W)) / MALLA_CELL_W
    y_in = (MALLA_YMAX - row * MALLA_CELL_H - y) / MALLA_CELL_H
    compass = ("N" if y_in < NS_THRESHOLD else "S") + ("W" if x_in < EW_THRESHOLD else "E")
    return f"{col}-{letter}", SUB_CONVENTION[compass]


def main():
    old_cal = json.loads(CAL_FILE.read_text(encoding="utf-8"))
    labels = json.loads(LABELS_VAL.read_text(encoding="utf-8"))
    accepts = [l for l in labels if l["action"] == "accept"]
    print(f"validator_labels: total={len(labels)}  accept={len(accepts)}")

    # Para cada bucket: lista de cal_estimates absolutos (en px nativos)
    estimates = defaultdict(list)
    skipped = 0
    for l in accepts:
        cell, sub = cellsub(l["rc"][:14])
        if cell is None:
            skipped += 1
            continue
        key = f"{cell}-{sub}"
        cal_dx, cal_dy = l.get("cal_dxdy", [0, 0])
        drag_dx, drag_dy = l["dxdy"]
        # Cal "óptima" estimada por este label = cal aplicada + drag (todo native px)
        est_x = cal_dx + drag_dx * SCALE_DISPLAY_TO_NATIVE
        est_y = cal_dy + drag_dy * SCALE_DISPLAY_TO_NATIVE
        estimates[key].append((est_x, est_y))

    print(f"buckets con datos directos: {len(estimates)}")
    if skipped:
        print(f"labels skipped (sin coords cache): {skipped}")

    # Partir de cal previa para preservar buckets sin labels nuevos
    csub_offsets = dict(old_cal.get("csub_offsets_px", {}))
    csub_stats = {}

    print()
    print(f"{'bucket':12s} {'n':>4s} {'old cal':>16s} {'new cal':>16s} {'residual':>10s}")
    for key, est_list in sorted(estimates.items()):
        med_x = median(e[0] for e in est_list)
        med_y = median(e[1] for e in est_list)
        old_cdx, old_cdy = csub_offsets.get(key, [0, 0])
        new_cdx, new_cdy = round(med_x, 1), round(med_y, 1)
        csub_offsets[key] = [new_cdx, new_cdy]
        sx = stdev(e[0] for e in est_list) if len(est_list) > 1 else 0.0
        sy = stdev(e[1] for e in est_list) if len(est_list) > 1 else 0.0
        residual = round((sx ** 2 + sy ** 2) ** 0.5, 2)
        csub_stats[key] = {
            "n": len(est_list),
            "std_x": round(sx, 2),
            "std_y": round(sy, 2),
            "expected_residual_px": residual,
        }
        print(f"  {key:10s} {len(est_list):>4d}  ({old_cdx:+5.0f},{old_cdy:+5.0f}) → ({new_cdx:+5.0f},{new_cdy:+5.0f})  {residual:>7.1f}px")

    # Conservar stats viejos para buckets sin labels en esta corrida
    for k, v in old_cal.get("csub_stats", {}).items():
        if k not in csub_stats:
            csub_stats[k] = v

    # cell_offsets = mediana de csub
    by_cell = defaultdict(list)
    for ck, v in csub_offsets.items():
        by_cell[ck.rsplit("-", 1)[0]].append(v)
    cell_offsets = {
        cell: [round(median(v[0] for v in vs), 1),
               round(median(v[1] for v in vs), 1)]
        for cell, vs in by_cell.items()
    }

    all_vals = list(csub_offsets.values())
    global_bias = [round(median(v[0] for v in all_vals), 1),
                   round(median(v[1] for v in all_vals), 1)] if all_vals else [0, 0]

    out = {
        "version": 7,
        "calibrated_at": __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M"),
        "n_labels": len(accepts),  # absoluto, no acumulativo (era bug v6)
        "global_bias_px": global_bias,
        "cell_offsets_px": cell_offsets,
        "csub_offsets_px": csub_offsets,
        "csub_stats": csub_stats,
        "cells_with_direct_data": sorted({k.rsplit("-", 1)[0] for k in csub_offsets}),
        "cells_interpolated": [],
        "csub_buckets_with_data": sorted(csub_offsets.keys()),
        "_method": "absolute (cal_dxdy + drag_native, median per bucket)",
    }

    # Write atómico
    tmp = CAL_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    tmp.replace(CAL_FILE)
    print(f"\nescrito: {CAL_FILE.name}  v7  n_labels={len(accepts)}")


if __name__ == "__main__":
    main()
