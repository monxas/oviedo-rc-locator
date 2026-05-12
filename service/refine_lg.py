"""
Refinamiento del snap con LightGlue + SuperPoint.

Carga modelos una vez al import. Threadsafe para uso desde FastAPI.

Pipeline:
  refine(wms_bytes, pgou_crop_native_rgb, snap_poly_pixels) → (dxdy, info)

Guardrails (si no se cumplen → no aplica, devuelve dxdy=(0,0) y reason):
  - matches >= 200
  - inliers / matches > 0.4
  - |dx|, |dy| < 150 (en píxeles PGOU display)
"""
from __future__ import annotations

import io
import threading
import time
from typing import Optional

import cv2
import numpy as np
import torch
from PIL import Image

# carga lazy (al primer refine())
_LOCK = threading.Lock()
_STATE: dict = {"ready": False}


def _load():
    if _STATE["ready"]: return
    with _LOCK:
        if _STATE["ready"]: return
        from lightglue import LightGlue, SuperPoint
        from lightglue.utils import rbd
        device = "mps" if torch.backends.mps.is_available() else "cpu"
        t0 = time.time()
        extractor = SuperPoint(max_num_keypoints=4096).eval().to(device)
        matcher = LightGlue(features="superpoint", filter_threshold=0.3).eval().to(device)
        _STATE.update(extractor=extractor, matcher=matcher, rbd=rbd, device=device)
        _STATE["ready"] = True
        _STATE["load_ms"] = int((time.time() - t0) * 1000)


def _canny_rgb(img: np.ndarray, low=80, high=180) -> np.ndarray:
    gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY) if img.ndim == 3 else img
    edges = cv2.Canny(gray, low, high)
    edges = cv2.dilate(edges, np.ones((2, 2), np.uint8), iterations=1)
    return cv2.cvtColor(edges, cv2.COLOR_GRAY2RGB)


def _to_tensor(img_pil: Image.Image, device: str) -> torch.Tensor:
    arr = np.array(img_pil).astype(np.float32) / 255.0
    return torch.from_numpy(arr).permute(2, 0, 1).unsqueeze(0).to(device)


# Guardrails configurables
MIN_MATCHES = 200
MIN_INLIER_RATIO = 0.4
MAX_OFFSET = 150  # px en display


def refine(wms_png_bytes: bytes, pgou_crop_native_rgb: np.ndarray,
           snap_poly_display: list[list[float]],
           wms_size_px: int = 1200,
           display_crop_px: int = 1800) -> dict:
    """Refina snap → corrección (dx, dy) en px display.

    pgou_crop_native_rgb: el crop YA en display size (1800x1800) RGB ndarray.
    snap_poly_display: polígono catastral tras snap, coords display [[x,y],...].
    wms_size_px: tamaño nativo del WMS PNG (e.g. 1200 = 300m).

    Devuelve dict con:
      applied (bool), dx, dy, reason, matches, inliers, ms
    """
    _load()
    t0 = time.time()

    wms_arr = np.array(Image.open(io.BytesIO(wms_png_bytes)).convert("RGB"))
    if wms_arr.shape[0] != wms_size_px:
        # adaptar
        wms_arr = cv2.resize(wms_arr, (wms_size_px, wms_size_px), interpolation=cv2.INTER_AREA)
    wms_canny = _canny_rgb(wms_arr)

    # Downscale PGOU 1800→900 para velocidad
    pgou_small = cv2.resize(pgou_crop_native_rgb, (900, 900), interpolation=cv2.INTER_AREA)
    scale = display_crop_px / 900

    with torch.inference_mode():
        f0 = _STATE["extractor"].extract(_to_tensor(Image.fromarray(wms_canny), _STATE["device"]))
        f1 = _STATE["extractor"].extract(_to_tensor(Image.fromarray(pgou_small), _STATE["device"]))
        m = _STATE["matcher"]({"image0": f0, "image1": f1})
    rbd = _STATE["rbd"]
    f0, f1, m = [rbd(x) for x in [f0, f1, m]]
    idx = m["matches"].cpu().numpy()
    n_m = int(len(idx))

    if n_m < MIN_MATCHES:
        return {"applied": False, "dx": 0, "dy": 0, "matches": n_m,
                "reason": f"matches<{MIN_MATCHES}", "ms": int((time.time()-t0)*1000)}

    kp0 = f0["keypoints"].cpu().numpy()[idx[:, 0]]
    kp1 = f1["keypoints"].cpu().numpy()[idx[:, 1]] * scale

    H, mask = cv2.findHomography(kp0, kp1, cv2.RANSAC, ransacReprojThreshold=5.0)
    if H is None:
        return {"applied": False, "dx": 0, "dy": 0, "matches": n_m,
                "reason": "no_homography", "ms": int((time.time()-t0)*1000)}
    n_inl = int(mask.sum())
    ratio = n_inl / n_m

    if ratio < MIN_INLIER_RATIO:
        return {"applied": False, "dx": 0, "dy": 0, "matches": n_m,
                "inliers": n_inl, "inlier_ratio": ratio,
                "reason": f"inlier_ratio<{MIN_INLIER_RATIO}",
                "ms": int((time.time()-t0)*1000)}

    pred = cv2.perspectiveTransform(
        np.array([[[wms_size_px / 2, wms_size_px / 2]]], dtype=np.float32), H)[0, 0]
    snap_center = np.array(snap_poly_display).mean(axis=0)
    dx = int(pred[0] - snap_center[0])
    dy = int(pred[1] - snap_center[1])

    if abs(dx) > MAX_OFFSET or abs(dy) > MAX_OFFSET:
        return {"applied": False, "dx": 0, "dy": 0, "matches": n_m,
                "inliers": n_inl, "inlier_ratio": ratio,
                "reason": f"offset>{MAX_OFFSET}",
                "raw_dx": dx, "raw_dy": dy,
                "ms": int((time.time()-t0)*1000)}

    return {"applied": True, "dx": dx, "dy": dy, "matches": n_m,
            "inliers": n_inl, "inlier_ratio": round(ratio, 2),
            "ms": int((time.time()-t0)*1000)}
