"""Plan A: análisis del impacto de body_rect en accuracy usando los labels reales.

Para cada accept label en data/validator_labels.json:
  1. UTM = coords_local[rc]
  2. (col, row, compass, sub, sheet) = geom
  3. (rx, ry) = utm_to_body_relative
  4. Render sheet, compute body_rect con cada método
  5. raw_pred(m) = body_rect(m).xy + (rx, ry) * body_rect(m).wh
  6. truth_pixel_abs = raw_pred(heuristic) + label.cal_dxdy + label.dxdy * SCALE
  7. error(m) = raw_pred(m) - truth_pixel
  8. Per-bucket optimal cal = median(error) por bucket
  9. Residual = error - bucket_median
  10. Métrica: median ||residual||₂ over labels.

Output: analyze_via_labels.json con summary + cal_per_bucket deltas.
Esto responde directamente: ¿qué método da menor error final con cal regenerada?
"""
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

from _setup import setup_paths, repo_root

setup_paths()
import body_detect  # noqa: E402
from oviedo_rc import render, geom  # noqa: E402
from oviedo_rc.concejo import OVIEDO, get_concejo_for_utm  # noqa: E402
from oviedo_rc.config import CACHE_DIR as _OVIEDO_CACHE  # noqa: E402

HERE = Path(__file__).resolve().parent
CACHE = str(_OVIEDO_CACHE)
COORDS = json.load(open(f"{CACHE}/coords_local.json"))
LABELS = json.load(open(repo_root() / "data" / "validator_labels.json"))
SHEETS_CATALOG = json.load(open(f"{CACHE}/sheets.json"))

SCALE = 2  # SCALE_DISPLAY_TO_NATIVE
METHODS = ["heuristic", "heuristic_v2", "hough", "template"]


def median(xs):
    xs = sorted(xs)
    n = len(xs)
    if n == 0:
        return 0
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def percentile(xs, p):
    if not xs:
        return 0
    xs = sorted(xs)
    k = max(0, min(len(xs) - 1, int(len(xs) * p / 100)))
    return xs[k]


def sheet_for_rc(rc14, X, Y, concejo):
    """RC → (sheet_name, col, row_idx, compass) usando misma lógica que geom.locate."""
    m = concejo.malla
    col = int((X - m.x0) // m.cell_w)
    row_idx = int((m.ymax - Y) // m.cell_h)
    if not (0 <= row_idx < 25):
        return None
    letter = "ABCDEFGHIJKLMNOPQRSTUVWXY"[row_idx]
    x_in = (X - (m.x0 + col * m.cell_w)) / m.cell_w
    y_in = (m.ymax - row_idx * m.cell_h - Y) / m.cell_h
    compass = ("N" if y_in < m.ns_threshold else "S") + \
              ("W" if x_in < m.ew_threshold else "E")
    sub = m.sub_convention[compass]
    return f"PLANO_{col}_{letter}_{sub}.pdf", col, row_idx, compass, sub, letter


def main():
    accepts = [l for l in LABELS if l["action"] == "accept"]
    print(f"Total labels: {len(LABELS)}; accepts: {len(accepts)}")

    # Cache body_rect por sheet (cada sheet aparece muchas veces)
    sheet_cache = {}

    def get_rects(sheet_name):
        if sheet_name in sheet_cache:
            return sheet_cache[sheet_name]
        pdf_path = f"{CACHE}/{sheet_name}"
        if not os.path.exists(pdf_path):
            sheet_cache[sheet_name] = None
            return None
        img, _, _ = render.render_pdf_page(pdf_path)
        H, W = img.shape[:2]
        rects = {m: body_detect.detect_body_rect(img, method=m) for m in METHODS}
        sheet_cache[sheet_name] = (rects, W, H)
        return sheet_cache[sheet_name]

    per_label: list = []
    skipped = {"no_coords": 0, "no_concejo_malla": 0, "no_sheet": 0, "no_pdf": 0}
    t_start = time.time()
    for i, l in enumerate(accepts):
        rc14 = l["rc"][:14]
        if rc14 not in COORDS:
            skipped["no_coords"] += 1
            continue
        c = COORDS[rc14]
        X, Y = c["x"], c["y"]
        concejo = get_concejo_for_utm(X, Y) or OVIEDO
        if concejo.malla is None:
            skipped["no_concejo_malla"] += 1
            continue
        info = sheet_for_rc(rc14, X, Y, concejo)
        if info is None:
            skipped["no_sheet"] += 1
            continue
        sheet, col, row, compass, sub, letter = info
        bucket = f"{col}-{letter}-{sub}"

        # Sólo Oviedo SU: nuestro template "su" está calibrado a SU. Para mantener
        # alcance del bench, restringimos a sheets cacheadas (no descargamos).
        rects = get_rects(sheet)
        if rects is None:
            skipped["no_pdf"] += 1
            continue
        rect_map, W, H = rects

        rx, ry = geom.utm_to_body_relative(X, Y, col, row, compass, concejo)

        # raw_pred por método
        raw = {}
        for m in METHODS:
            bx, by, bw, bh = rect_map[m]
            px = bx + rx * bw
            py = by + ry * bh
            raw[m] = (px, py)

        # truth pixel (en coords nativas, referenciado al render heurístico)
        cal_dx, cal_dy = l.get("cal_dxdy", [0, 0])
        drag_dx, drag_dy = l["dxdy"]
        truth_px = raw["heuristic"][0] + cal_dx + drag_dx * SCALE
        truth_py = raw["heuristic"][1] + cal_dy + drag_dy * SCALE

        row_data = {"rc": rc14, "bucket": bucket, "sheet": sheet,
                    "rx": rx, "ry": ry, "truth": (truth_px, truth_py)}
        for m in METHODS:
            row_data[m] = {
                "raw": raw[m],
                "err": (raw[m][0] - truth_px, raw[m][1] - truth_py),
            }
        per_label.append(row_data)

        if (i + 1) % 100 == 0:
            elapsed = time.time() - t_start
            print(f"  {i+1}/{len(accepts)} processed, {len(per_label)} kept, {elapsed:.1f}s, sheets cached: {len(sheet_cache)}")

    print(f"\nKept {len(per_label)} labels. Skipped: {skipped}")

    # Per-bucket optimal cal (median error) por método
    cal_per_bucket = {m: {} for m in METHODS}
    bucket_errs = defaultdict(lambda: defaultdict(list))
    for r in per_label:
        for m in METHODS:
            bucket_errs[r["bucket"]][m].append(r[m]["err"])
    for bucket, by_m in bucket_errs.items():
        for m in METHODS:
            errs = by_m[m]
            cal_per_bucket[m][bucket] = (median(e[0] for e in errs),
                                         median(e[1] for e in errs))

    # Residual (raw - bucket_median) per label per method
    residuals = {m: [] for m in METHODS}
    raw_errs = {m: [] for m in METHODS}
    for r in per_label:
        for m in METHODS:
            ex, ey = r[m]["err"]
            bx, by = cal_per_bucket[m][r["bucket"]]
            rx_res = ex - bx
            ry_res = ey - by
            residuals[m].append((rx_res ** 2 + ry_res ** 2) ** 0.5)
            raw_errs[m].append((ex ** 2 + ey ** 2) ** 0.5)

    print("\n" + "=" * 90)
    print("RESULTADO (residual = error tras aplicar cal óptima por bucket)")
    print(f"  n labels = {len(per_label)}, n buckets = {len(bucket_errs)}")
    print("=" * 90)
    print(f"{'método':14s} {'raw_err_med_px':>16s} {'raw_err_p90':>14s} "
          f"{'residual_med_px':>17s} {'residual_p90':>16s} {'residual_mean':>15s}")
    summary = {}
    for m in METHODS:
        rs = residuals[m]
        rws = raw_errs[m]
        s = {
            "raw_err_median_px": median(rws),
            "raw_err_p90_px": percentile(rws, 90),
            "residual_median_px": median(rs),
            "residual_p90_px": percentile(rs, 90),
            "residual_mean_px": sum(rs) / len(rs) if rs else 0,
        }
        summary[m] = s
        print(f"{m:14s} {s['raw_err_median_px']:16.2f} {s['raw_err_p90_px']:14.2f} "
              f"{s['residual_median_px']:17.2f} {s['residual_p90_px']:16.2f} {s['residual_mean_px']:15.2f}")

    # body_rect deltas por sheet (cuanto cambian los rects entre métodos)
    print("\n" + "=" * 90)
    print("DELTAS body_rect entre heuristic y template, por sheet (top 5 muestras)")
    print("=" * 90)
    samples = 0
    for sheet, val in sheet_cache.items():
        if val is None:
            continue
        rect_map, W, H = val
        rh = rect_map["heuristic"]
        rt = rect_map["template"]
        dx_corner = rt[0] - rh[0]
        dy_corner = rt[1] - rh[1]
        dw = rt[2] - rh[2]
        dh = rt[3] - rh[3]
        print(f"  {sheet}  H={rh}  T={rt}  Δcorner=({dx_corner},{dy_corner})  Δsize=({dw},{dh})")
        samples += 1
        if samples >= 5:
            break

    print("\n" + "=" * 90)
    print("VEREDICTO")
    print("=" * 90)
    base = summary["heuristic"]["residual_median_px"]
    for m in METHODS:
        if m == "heuristic":
            continue
        s = summary[m]
        delta = base - s["residual_median_px"]
        delta_pct = (delta / base * 100) if base > 0 else 0
        verdict = "✅ MEJORA" if delta_pct >= 10 else ("⚠️ leve" if delta_pct > 0 else "❌ peor o igual")
        print(f"  {m}: residual_median {s['residual_median_px']:.2f}px vs heuristic {base:.2f}px "
              f"→ Δ={delta:+.2f}px ({delta_pct:+.1f}%) {verdict}")

    out = {
        "n_labels": len(per_label),
        "n_buckets": len(bucket_errs),
        "summary": summary,
        "cal_per_bucket_template_minus_heuristic_median": {
            b: (cal_per_bucket["template"][b][0] - cal_per_bucket["heuristic"][b][0],
                cal_per_bucket["template"][b][1] - cal_per_bucket["heuristic"][b][1])
            for b in cal_per_bucket["template"]
        },
        "ts": time.time(),
    }
    out_path = HERE / "analyze_via_labels.json"
    out_path.write_text(json.dumps(out, indent=2, default=str))
    print(f"\nDetalle en {out_path}")


if __name__ == "__main__":
    main()
