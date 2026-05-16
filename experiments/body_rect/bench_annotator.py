"""Plan B (no usado): bench contra GT anotado a mano por annotator.py.

Sólo usar si `analyze_via_labels.py` no es concluyente. Lee body_rect_gt.json
del annotator y compara los 4 métodos por IoU y corner error.

Outputs:
  bench_annotator.md / bench_annotator.json
"""
import json
import os
import sys
import time
from pathlib import Path

from _setup import setup_paths

setup_paths()
import body_detect  # noqa: E402
from oviedo_rc import render  # noqa: E402
from oviedo_rc.config import CACHE_DIR as _OVIEDO_CACHE  # noqa: E402

HERE = Path(__file__).resolve().parent
CACHE = str(_OVIEDO_CACHE)
GT_FILE = HERE / "body_rect_gt.json"
OUT_JSON = HERE / "bench_annotator.json"
OUT_MD = HERE / "bench_annotator.md"

METHODS = ["heuristic", "heuristic_v2", "hough", "template"]


def iou(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ax2, ay2 = ax + aw, ay + ah
    bx2, by2 = bx + bw, by + bh
    ix = max(0, min(ax2, bx2) - max(ax, bx))
    iy = max(0, min(ay2, by2) - max(ay, by))
    inter = ix * iy
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


def corner_err(pred, gt):
    """Devuelve (tl_err_px, br_err_px) en píxeles."""
    px, py, pw, ph = pred
    gx, gy, gw, gh = gt
    tl = ((px - gx) ** 2 + (py - gy) ** 2) ** 0.5
    br = ((px + pw - gx - gw) ** 2 + (py + ph - gy - gh) ** 2) ** 0.5
    return tl, br


def main():
    if not GT_FILE.exists():
        print(f"ERROR: {GT_FILE} no existe — anota primero.")
        sys.exit(1)
    gt_data = json.load(open(GT_FILE))

    annotated = {pdf: d for pdf, d in gt_data.items()
                 if d.get("user_action") in ("accept_heuristic", "drag")}
    if not annotated:
        print("ERROR: no hay anotaciones válidas (accept_heuristic | drag).")
        sys.exit(1)

    print(f"Benchmarking {len(annotated)} hojas anotadas, {len(METHODS)} métodos…")

    results = []
    for pdf, d in annotated.items():
        gt = tuple(d["gt_rect"])
        pdf_path = os.path.join(CACHE, pdf)
        t0 = time.time()
        img, _, _ = render.render_pdf_page(pdf_path)
        H, W = img.shape[:2]
        render_ms = (time.time() - t0) * 1000

        row = {"pdf": pdf, "W": W, "H": H, "gt": list(gt), "user_action": d["user_action"]}
        for m in METHODS:
            t1 = time.time()
            pred = body_detect.detect_body_rect(img, method=m)
            elapsed_ms = (time.time() - t1) * 1000
            row[m] = {
                "rect": list(pred),
                "iou": iou(pred, gt),
                "tl_err_px": corner_err(pred, gt)[0],
                "br_err_px": corner_err(pred, gt)[1],
                "elapsed_ms": elapsed_ms,
            }
        row["render_ms"] = render_ms
        results.append(row)
        print(f"  {pdf:30s}  IoU: " + " ".join(f"{m[0]}={row[m]['iou']:.3f}" for m in METHODS))

    # Agregados
    def median(xs):
        xs = sorted(xs)
        n = len(xs)
        return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2

    summary = {}
    for m in METHODS:
        ious = [r[m]["iou"] for r in results]
        tls = [r[m]["tl_err_px"] for r in results]
        brs = [r[m]["br_err_px"] for r in results]
        ts = [r[m]["elapsed_ms"] for r in results]
        summary[m] = {
            "iou_median": median(ious),
            "iou_min": min(ious),
            "iou_mean": sum(ious) / len(ious),
            "tl_err_median_px": median(tls),
            "tl_err_max_px": max(tls),
            "br_err_median_px": median(brs),
            "br_err_max_px": max(brs),
            "elapsed_median_ms": median(ts),
        }

    out = {"results": results, "summary": summary, "n_sheets": len(results),
           "methods": METHODS, "ts": time.time()}
    OUT_JSON.write_text(json.dumps(out, indent=2))

    md = [f"# Bench body_detect ({len(results)} hojas)\n"]
    md.append("## Resumen\n")
    md.append("| método | IoU mediana | IoU min | tl_err mediana px | tl_err max px | br_err mediana px | ms |")
    md.append("|---|---|---|---|---|---|---|")
    for m in METHODS:
        s = summary[m]
        md.append(f"| {m} | {s['iou_median']:.4f} | {s['iou_min']:.4f} "
                  f"| {s['tl_err_median_px']:.1f} | {s['tl_err_max_px']:.1f} "
                  f"| {s['br_err_median_px']:.1f} | {s['elapsed_median_ms']:.1f} |")

    base_iou = summary["heuristic"]["iou_median"]
    md.append(f"\n## Criterio de despliegue\n")
    md.append(f"- Baseline (heuristic) IoU mediana: **{base_iou:.4f}**")
    md.append("- Requiere ≥10% mejora en mediana IoU AND paridad/mejora en tl_err\n")
    for m in METHODS:
        if m == "heuristic":
            continue
        s = summary[m]
        delta = (s["iou_median"] - base_iou) / max(base_iou, 1e-9) * 100
        tl_delta = s["tl_err_median_px"] - summary["heuristic"]["tl_err_median_px"]
        verdict = "✅ DESPLEGAR" if delta >= 10 and tl_delta <= 0 else "❌ NO desplegar"
        md.append(f"- **{m}**: ΔIoU={delta:+.1f}%, Δtl_err={tl_delta:+.1f}px → {verdict}")

    md.append("\n## Detalle por hoja\n")
    md.append("| pdf | gt | " + " | ".join(f"{m} IoU" for m in METHODS) + " |")
    md.append("|---|---|" + "|".join("---" for _ in METHODS) + "|")
    for r in results:
        cells = " | ".join(f"{r[m]['iou']:.3f}" for m in METHODS)
        md.append(f"| {r['pdf']} | {r['gt']} | {cells} |")

    OUT_MD.write_text("\n".join(md) + "\n")
    print(f"\nWrote {OUT_MD} and {OUT_JSON}")
    print(open(OUT_MD).read())


if __name__ == "__main__":
    main()
