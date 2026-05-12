"""
LightGlue a saco · procesa RCs urbanos del cache continuamente.

- Lee coords_local.json (~54k RCs)
- Filtra a urbanos válidos con hoja PGOU
- ThreadPool genera crops (CPU+IO en paralelo)
- LightGlue serializado (single-GPU/CPU)
- Output JSONL incremental con checkpoint resumible
- Guarda cada 25 RCs procesados
- Estadísticas en tiempo real
"""
from __future__ import annotations

import io
import json
import os
import random
import signal
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from queue import Queue, Empty

import cv2
import numpy as np
import torch
from PIL import Image

ROOT = Path.home() / "oviedo-rc-locator"
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "service"))

from validator_ui import _generate_for_rc, _covered_csub, COORDS
from oviedo_rc import wms, geom
from oviedo_rc.errors import RCError

OUT_DIR = ROOT / "data" / "lg_batch"
OUT_DIR.mkdir(parents=True, exist_ok=True)
JSONL = OUT_DIR / "lg_auto.jsonl"
CHECKPOINT = OUT_DIR / "checkpoint.json"
STATS_FILE = OUT_DIR / "stats.json"

WMS_PX = 1200
DISPLAY_CROP = 1800
MIN_MATCHES = 200
MIN_INLIER_RATIO = 0.4
MAX_OFFSET = 150
CHECKPOINT_EVERY = 25
GEN_WORKERS = 6


_stop = False
def _handle_signal(*_):
    global _stop; _stop = True
signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)


def canny_rgb(img: np.ndarray) -> np.ndarray:
    gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY) if img.ndim == 3 else img
    edges = cv2.Canny(gray, 80, 180)
    edges = cv2.dilate(edges, np.ones((2, 2), np.uint8), iterations=1)
    return cv2.cvtColor(edges, cv2.COLOR_GRAY2RGB)


def to_tensor(pil_img, device):
    arr = np.array(pil_img).astype(np.float32) / 255.0
    return torch.from_numpy(arr).permute(2, 0, 1).unsqueeze(0).to(device)


def list_candidates() -> list[str]:
    """RCs urbanos válidos con cell-sub cubierto, ordenados aleatoriamente."""
    covered = _covered_csub()
    out = []
    for rc14, rec in COORDS.items():
        rc = rc14 + "0001AA"
        try:
            geom.validate_rc(rc)
        except RCError:
            continue
        x = rec.get("x") if isinstance(rec, dict) else rec[0]
        y = rec.get("y") if isinstance(rec, dict) else rec[1]
        from oviedo_rc.config import (MALLA_X0, MALLA_YMAX, MALLA_CELL_W, MALLA_CELL_H,
                                       NS_THRESHOLD, EW_THRESHOLD, SUB_CONVENTION)
        col = int((x - MALLA_X0) // MALLA_CELL_W)
        row = int((MALLA_YMAX - y) // MALLA_CELL_H)
        if not (0 <= row < 25): continue
        letter = "ABCDEFGHIJKLMNOPQRSTUVWXY"[row]
        x_in = (x - (MALLA_X0 + col * MALLA_CELL_W)) / MALLA_CELL_W
        y_in = (MALLA_YMAX - row * MALLA_CELL_H - y) / MALLA_CELL_H
        compass = ("N" if y_in < NS_THRESHOLD else "S") + ("W" if x_in < EW_THRESHOLD else "E")
        sub = SUB_CONVENTION[compass]
        key = f"{col}-{letter}-{sub}"
        if key not in covered: continue
        out.append(rc)
    random.seed(42); random.shuffle(out)
    return out


def load_done() -> set:
    """Lee JSONL existente para resumir."""
    if not JSONL.exists(): return set()
    done = set()
    with JSONL.open(encoding="utf-8") as f:
        for line in f:
            try:
                done.add(json.loads(line)["rc"])
            except Exception:
                pass
    return done


def gen_for_rc(rc: str) -> dict | None:
    """Pipeline costoso de validador para 1 RC. Devuelve dict o None si falla."""
    try:
        data = _generate_for_rc(rc)
    except Exception as e:
        return {"rc": rc, "error": str(e)[:150]}
    # WMS hi-res
    try:
        info = geom.locate(rc)
        X, Y = info["utm"]
        wms_hires = wms.get(X - 150, Y - 150, X + 150, Y + 150, w=WMS_PX)
    except Exception:
        wms_hires = data["wms_png"]
    data["wms_hires"] = wms_hires
    return data


def main():
    from lightglue import LightGlue, SuperPoint
    from lightglue.utils import rbd

    device = "mps" if torch.backends.mps.is_available() else "cpu"
    extractor = SuperPoint(max_num_keypoints=4096).eval().to(device)
    matcher = LightGlue(features="superpoint", filter_threshold=0.3).eval().to(device)
    print(f"[lg_batch] device={device}  models loaded", flush=True)

    print("[lg_batch] listing candidates…", flush=True)
    candidates = list_candidates()
    done = load_done()
    todo = [rc for rc in candidates if rc not in done]
    print(f"[lg_batch] {len(candidates)} total · {len(done)} done · {len(todo)} TODO", flush=True)

    stats = defaultdict(int)
    t_start = time.time()
    last_save = time.time()

    # Generación en paralelo, consumo serializado para LG
    gen_pool = ThreadPoolExecutor(max_workers=GEN_WORKERS)
    # Pre-encola un buffer
    buffer_size = GEN_WORKERS * 2
    pending = {}
    idx = 0

    def submit_next():
        nonlocal idx
        if idx >= len(todo): return
        rc = todo[idx]; idx += 1
        pending[rc] = gen_pool.submit(gen_for_rc, rc)

    for _ in range(buffer_size):
        submit_next()

    n_processed = 0
    out_f = JSONL.open("a", encoding="utf-8")
    try:
        for rc in todo:
            if _stop:
                print("[lg_batch] SIGTERM, finalizando", flush=True)
                break
            if rc not in pending: submit_next()
            fut = pending.pop(rc)
            submit_next()
            data = fut.result()
            if not data or "error" in (data or {}):
                rec = {"rc": rc, "ts": time.time(), "error": (data or {}).get("error", "no_data")}
                out_f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                stats["gen_error"] += 1
            else:
                # LG
                t0 = time.time()
                wms_arr = np.array(Image.open(io.BytesIO(data["wms_hires"])).convert("RGB"))
                if wms_arr.shape[0] != WMS_PX:
                    wms_arr = cv2.resize(wms_arr, (WMS_PX, WMS_PX), interpolation=cv2.INTER_AREA)
                wms_canny = canny_rgb(wms_arr)
                pgou_arr = np.array(Image.open(io.BytesIO(data["crop_png"])).convert("RGB"))
                pgou_small = cv2.resize(pgou_arr, (900, 900), interpolation=cv2.INTER_AREA)
                scale = DISPLAY_CROP / 900

                with torch.inference_mode():
                    f0 = extractor.extract(to_tensor(Image.fromarray(wms_canny), device))
                    f1 = extractor.extract(to_tensor(Image.fromarray(pgou_small), device))
                    m = matcher({"image0": f0, "image1": f1})
                f0, f1, m = [rbd(x) for x in [f0, f1, m]]
                idx_m = m["matches"].cpu().numpy()
                n_m = int(len(idx_m))

                rec = {
                    "rc": rc, "ts": time.time(),
                    "cell": data["cell"], "sub": data["sub_quadrant"],
                    "snap_dxdy": data["snap_dxdy"],
                    "cal_dxdy": data["cal_dxdy"],
                    "snap_score": data["snap_score"],
                    "matches": n_m,
                    "lg_ms": int((time.time() - t0) * 1000),
                }

                if n_m < MIN_MATCHES:
                    rec["applied"] = False; rec["reason"] = f"matches<{MIN_MATCHES}"
                    stats["fb_few_matches"] += 1
                else:
                    kp0 = f0["keypoints"].cpu().numpy()[idx_m[:, 0]]
                    kp1 = f1["keypoints"].cpu().numpy()[idx_m[:, 1]] * scale
                    H, mask = cv2.findHomography(kp0, kp1, cv2.RANSAC, ransacReprojThreshold=5.0)
                    if H is None:
                        rec["applied"] = False; rec["reason"] = "no_homography"
                        stats["fb_no_homog"] += 1
                    else:
                        n_inl = int(mask.sum()); ratio = n_inl / n_m
                        rec["inliers"] = n_inl; rec["inlier_ratio"] = round(ratio, 2)
                        if ratio < MIN_INLIER_RATIO:
                            rec["applied"] = False; rec["reason"] = f"ratio<{MIN_INLIER_RATIO}"
                            stats["fb_low_ratio"] += 1
                        else:
                            pred = cv2.perspectiveTransform(
                                np.array([[[WMS_PX/2, WMS_PX/2]]], dtype=np.float32), H)[0, 0]
                            snap_center = np.array(data["poly_snap"]).mean(axis=0)
                            dx = int(pred[0] - snap_center[0])
                            dy = int(pred[1] - snap_center[1])
                            rec["raw_dx"] = dx; rec["raw_dy"] = dy
                            if abs(dx) > MAX_OFFSET or abs(dy) > MAX_OFFSET:
                                rec["applied"] = False; rec["reason"] = f"offset>{MAX_OFFSET}"
                                stats["fb_big_offset"] += 1
                            else:
                                rec["applied"] = True
                                rec["dx_display"] = dx
                                rec["dy_display"] = dy
                                rec["dx_native"] = dx * 2  # display→native
                                rec["dy_native"] = dy * 2
                                stats["applied"] += 1

                out_f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                stats["total"] += 1

            n_processed += 1
            if n_processed % 5 == 0:
                rate = n_processed / (time.time() - t_start)
                applied = stats.get("applied", 0)
                pct = 100 * applied / max(stats["total"], 1)
                print(f"  [{n_processed:5d}/{len(todo)}] rate={rate:.1f}/s  applied={applied} ({pct:.0f}%)  fb={stats['fb_few_matches']+stats['fb_no_homog']+stats['fb_low_ratio']+stats['fb_big_offset']}", flush=True)
                out_f.flush()

            if time.time() - last_save > 60:
                STATS_FILE.write_text(json.dumps(dict(stats), indent=2))
                last_save = time.time()
    finally:
        out_f.close()
        gen_pool.shutdown(wait=False, cancel_futures=True)
        STATS_FILE.write_text(json.dumps(dict(stats), indent=2))
        print(f"\n[lg_batch] DONE  procesados={n_processed}  applied={stats.get('applied',0)}", flush=True)


if __name__ == "__main__":
    main()
