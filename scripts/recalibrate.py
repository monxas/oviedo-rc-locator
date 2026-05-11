"""Recalibra calibration_offsets.json a partir de validator_labels.json."""
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
SCALE_DISPLAY_TO_NATIVE = 2  # crop downscale 2× → drag * 2 = nativo


def _coords():
    cache = Path.home() / ".cache" / "oviedo_rc"
    cf = cache / COORDS_FILE.name
    return json.loads(cf.read_text(encoding="utf-8"))


COORDS = _coords()


def cellsub(rc14):
    rec = COORDS.get(rc14)
    if not rec: return None, None
    x = rec.get("x") if isinstance(rec, dict) else rec[0]
    y = rec.get("y") if isinstance(rec, dict) else rec[1]
    col = int((x - MALLA_X0) // MALLA_CELL_W)
    row = int((MALLA_YMAX - y) // MALLA_CELL_H)
    if not (0 <= row < 25): return None, None
    letter = "ABCDEFGHIJKLMNOPQRSTUVWXY"[row]
    x_in = (x - (MALLA_X0 + col * MALLA_CELL_W)) / MALLA_CELL_W
    y_in = (MALLA_YMAX - row * MALLA_CELL_H - y) / MALLA_CELL_H
    compass = ("N" if y_in < NS_THRESHOLD else "S") + ("W" if x_in < EW_THRESHOLD else "E")
    return f"{col}-{letter}", SUB_CONVENTION[compass]


def main():
    old_cal = json.loads(CAL_FILE.read_text(encoding="utf-8"))
    new_labels = json.loads(LABELS_VAL.read_text(encoding="utf-8"))
    accepts = [l for l in new_labels if l["action"] == "accept"]
    print(f"labels validador: total={len(new_labels)}  accept={len(accepts)}")

    corrections = defaultdict(list)
    skipped = []
    for l in accepts:
        rc14 = l["rc"][:14]
        cell, sub = cellsub(rc14)
        if cell is None:
            skipped.append(l["rc"]); continue
        key = f"{cell}-{sub}"
        dx, dy = l["dxdy"]
        corrections[key].append((dx * SCALE_DISPLAY_TO_NATIVE, dy * SCALE_DISPLAY_TO_NATIVE))

    print(f"buckets con correcciones: {len(corrections)}")
    if skipped: print(f"skipped sin coords cache: {skipped}")

    csub_offsets = dict(old_cal.get("csub_offsets_px", {}))
    csub_stats = dict(old_cal.get("csub_stats", {}))

    print()
    for key, corrs in sorted(corrections.items()):
        cx = median(c[0] for c in corrs)
        cy = median(c[1] for c in corrs)
        old_cdx, old_cdy = csub_offsets.get(key, [0, 0])
        new_cdx, new_cdy = old_cdx + cx, old_cdy + cy
        csub_offsets[key] = [round(new_cdx, 1), round(new_cdy, 1)]
        n_old = csub_stats.get(key, {}).get("n", 0)
        sx = stdev(c[0] for c in corrs) if len(corrs) > 1 else 0.0
        sy = stdev(c[1] for c in corrs) if len(corrs) > 1 else 0.0
        csub_stats[key] = {
            "n": n_old + len(corrs),
            "std_x": round(sx, 2),
            "std_y": round(sy, 2),
            "expected_residual_px": round((sx**2 + sy**2) ** 0.5, 2),
        }
        print(f"  {key:12s} n={len(corrs)} drag_med=({cx:+6.1f},{cy:+6.1f}) → cal ({old_cdx:+.0f},{old_cdy:+.0f}) → ({new_cdx:+.0f},{new_cdy:+.0f})")

    # cell_offsets = median per cell
    cell_offsets = {}
    by_cell = defaultdict(list)
    for ck, v in csub_offsets.items():
        cell = ck.rsplit("-", 1)[0]
        by_cell[cell].append(v)
    for cell, vs in by_cell.items():
        cell_offsets[cell] = [round(median(v[0] for v in vs), 1),
                              round(median(v[1] for v in vs), 1)]

    all_vals = list(csub_offsets.values())
    global_bias = [round(median(v[0] for v in all_vals), 1),
                   round(median(v[1] for v in all_vals), 1)] if all_vals else [0, 0]

    out = {
        "version": 6,
        "calibrated_at": __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M"),
        "n_labels": old_cal.get("n_labels", 0) + len(accepts),
        "global_bias_px": global_bias,
        "cell_offsets_px": cell_offsets,
        "csub_offsets_px": csub_offsets,
        "csub_stats": csub_stats,
        "cells_with_direct_data": sorted({k.rsplit("-", 1)[0] for k in csub_offsets}),
        "cells_interpolated": [],
        "csub_buckets_with_data": sorted(csub_offsets.keys()),
    }

    bk = CAL_FILE.with_suffix(".bak.v5.json")
    if not bk.exists():
        bk.write_text(CAL_FILE.read_text(encoding="utf-8"))
        print(f"\nbackup creado: {bk.name}")
    CAL_FILE.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    print(f"escrito: {CAL_FILE.name}  v6  n_labels={out['n_labels']}")


if __name__ == "__main__":
    main()
