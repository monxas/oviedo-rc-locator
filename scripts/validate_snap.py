#!/usr/bin/env python3
"""Web app local para etiquetar snap ground-truth.

Flujo:
  - Cargas un RC (manualmente o random)
  - Ves un zoom 900x900 del plano con polígono en posición MODEL (rojo)
    y posición SNAP si aplica (azul)
  - Marcas:
      * "model OK" → la posición del modelo geométrico es correcta
      * "snap OK" → la posición del snap es correcta
      * Click en la imagen → marca el centro real del polígono ahí
      * "skip"
  - Guarda en data/snap_labels.json. Etiquetar 10-15 puntos para tunear.

Uso:
  python3 scripts/validate_snap.py
  → abrir http://127.0.0.1:8765
"""
import io
import json
import os
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import cv2
import numpy as np
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel

from oviedo_rc import calibration, catastro, geom, pgou, render, snap as snap_mod, wms
from oviedo_rc.config import (
    BODY_W_M, BODY_H_M,
    MALLA_X0, MALLA_YMAX, MALLA_CELL_W, MALLA_CELL_H,
    MALLA_MARG_X, MALLA_MARG_Y,
    COORDS_FILE,
)

LABELS_PATH = ROOT / "data" / "snap_labels.json"
LABELS_PATH.parent.mkdir(exist_ok=True, parents=True)
CROP_SIZE = 900
CROP_SIZE_WIDE = 1800


def _anchor_utm(col, row_idx, compass):
    sub_x_off = 0 if "W" in compass else MALLA_CELL_W / 2
    sub_y_off = 0 if "N" in compass else MALLA_CELL_H / 2
    body_x_min = MALLA_X0 + col * MALLA_CELL_W + sub_x_off - MALLA_MARG_X
    body_y_max = MALLA_YMAX - row_idx * MALLA_CELL_H - sub_y_off + MALLA_MARG_Y
    return body_x_min, body_y_max


def _load_labels():
    if not LABELS_PATH.exists():
        return []
    return json.loads(LABELS_PATH.read_text())


def _save_labels(labels):
    LABELS_PATH.write_text(json.dumps(labels, indent=2, ensure_ascii=False))


def detect_edge_neighbors(info_dict, X, Y):
    """Si el RC está cerca del borde de cell (<50m), devuelve cells vecinas
    candidatas y sus respectivos sheets si existen en el listado del PGOU."""
    from oviedo_rc.config import (
        MALLA_X0, MALLA_YMAX, MALLA_CELL_W, MALLA_CELL_H, SUB_CONVENTION,
    )
    col = int((X - MALLA_X0) // MALLA_CELL_W)
    row = int((MALLA_YMAX - Y) // MALLA_CELL_H)
    x_in = (X - (MALLA_X0 + col * MALLA_CELL_W)) / MALLA_CELL_W
    y_in = (MALLA_YMAX - row * MALLA_CELL_H - Y) / MALLA_CELL_H
    EDGE_M = 50
    candidates = set()
    # bordes externos de cell
    if x_in * MALLA_CELL_W < EDGE_M:
        candidates.add((col - 1, row))
    if (1 - x_in) * MALLA_CELL_W < EDGE_M:
        candidates.add((col + 1, row))
    if y_in * MALLA_CELL_H < EDGE_M:
        candidates.add((col, row - 1))
    if (1 - y_in) * MALLA_CELL_H < EDGE_M:
        candidates.add((col, row + 1))

    sheets = pgou.get_sheet_listing()
    out = []
    for c, r in candidates:
        if not (0 <= r < 25):
            continue
        letter = "ABCDEFGHIJKLMNOPQRSTUVWXY"[r]
        # cuando vamos al cell vecino, el sub_quadrant del RC en ese
        # plano es el complementario lateral
        nx_in = x_in - (c - col)  # >1 o <0 si en otra cell, normalizar
        ny_in = y_in - (r - row)
        sub_compass = ("N" if ny_in < 0.5 else "S") + ("W" if nx_in < 0.5 else "E")
        sub = SUB_CONVENTION[sub_compass]
        sheet = f"PLANO_{c}_{letter}_{sub}.pdf"
        if sheet in sheets:
            out.append({"cell": f"{c}-{letter}", "sub_quadrant": sub, "sheet_name": sheet})
    return out, x_in, y_in


def render_for_validation(rc: str, wide: bool = False, override_sheet: str | None = None):
    """Procesa el RC y devuelve datos para la UI: PNG bytes, model/snap coords.

    `wide=True` devuelve un crop el doble de grande (1800×1800) para casos
    donde el polígono se sale del crop normal.
    """
    crop_target = CROP_SIZE_WIDE if wide else CROP_SIZE
    info = geom.locate(rc)
    rc14 = geom.validate_rc(rc)
    sheet_to_use = override_sheet or info["sheet_name"]
    pdf_path = pgou.fetch_sheet_pdf(sheet_to_use)
    img, _, _ = render.render_pdf_page(pdf_path)
    body_rect = render.detect_body_rect(img)

    poly = catastro.get_parcel_polygon(rc14)
    if not poly or not poly.get("polygon_utm"):
        raise ValueError(f"sin polígono para {rc}")

    # Si hay override, usar el cell/sub del sheet alternativo para el anchor
    if override_sheet:
        parts = override_sheet.replace("PLANO_", "").replace(".pdf", "").split("_")
        col = int(parts[0])
        row_idx = "ABCDEFGHIJKLMNOPQRSTUVWXY".index(parts[1])
        sub_compass_map = {"I": "NW", "II": "NE", "III": "SW", "IV": "SE"}
        compass = sub_compass_map[parts[2]]
        eff_cell = f"{col}-{parts[1]}"
        eff_sub = parts[2]
    else:
        col_letter = info["cell"].split("-")
        col = int(col_letter[0])
        row_idx = "ABCDEFGHIJKLMNOPQRSTUVWXY".index(col_letter[1])
        compass = info["sub_compass"]
        eff_cell = info["cell"]
        eff_sub = info["sub_quadrant"]
    anchor = _anchor_utm(col, row_idx, compass)
    poly_px_model = render.utm_polygon_to_pixel(
        poly["polygon_utm"], body_rect, anchor, compass
    )
    # Validator APLICA cal al polígono mostrado (vuelta atrás). El meta
    # incluye `cal_applied_dxdy` para que en api_label podamos convertir
    # truth_dxdy_drag → bare_truth = drag + cal_applied.
    cal_dx, cal_dy = calibration.offset_for(eff_cell, eff_sub)
    poly_px_model = [(x + cal_dx, y + cal_dy) for x, y in poly_px_model]
    cal_offset_for_display = (cal_dx, cal_dy)

    # snap candidato (aunque por default esté off)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    dx_snap, dy_snap, snap_score = snap_mod.snap(gray, poly_px_model)
    poly_px_snap = [(x + dx_snap, y + dy_snap) for x, y in poly_px_model]

    # centro de modelo (para crop)
    model_pts = np.array(poly_px_model)
    cx_model, cy_model = model_pts.mean(axis=0)

    # crop
    H, W = img.shape[:2]
    x0 = int(max(0, cx_model - crop_target // 2))
    y0 = int(max(0, cy_model - crop_target // 2))
    x1 = min(W, x0 + crop_target)
    y1 = min(H, y0 + crop_target)
    crop = img[y0:y1, x0:x1].copy()
    crop_h, crop_w = crop.shape[:2]

    # NO dibujamos los polígonos en el PNG. La UI los dibuja como SVG
    # superpuesto para que se puedan arrastrar.
    def shift_to_crop(pts, off_x, off_y):
        return [[int(x - off_x), int(y - off_y)] for x, y in pts]

    poly_model_in_crop = shift_to_crop(poly_px_model, x0, y0)
    poly_snap_in_crop = shift_to_crop(poly_px_snap, x0, y0)

    ok, buf = cv2.imencode(".png", crop)
    if not ok:
        raise RuntimeError("png encode failed")

    # Detectar candidatos de plano vecino si el RC está cerca del borde
    X, Y = info["utm"]
    neighbors, x_in, y_in = detect_edge_neighbors(info, X, Y)

    # poly_model_in_crop YA tiene la cal aplicada (es la posición que ve el usuario)
    cdx, cdy = cal_offset_for_display
    poly_calibrated_in_crop = poly_model_in_crop  # alias para retrocompat de la UI

    return {
        "rc": rc,
        "address": info["address"],
        "sheet_name": sheet_to_use,
        "sheet_assigned": info["sheet_name"],
        "cell": eff_cell,
        "sub_quadrant": eff_sub,
        "warnings": info["warnings"],
        "crop_origin_px": [x0, y0],
        "crop_size_px": [crop_w, crop_h],
        "model_center_px": [int(cx_model), int(cy_model)],
        "snap_dxdy": [int(dx_snap), int(dy_snap)],
        "snap_score": float(snap_score),
        "polygon_model_in_crop": poly_model_in_crop,
        "polygon_snap_in_crop": poly_snap_in_crop,
        "polygon_calibrated_in_crop": poly_calibrated_in_crop,
        "cal_applied_dxdy": [int(cdx), int(cdy)],
        "polygon_label": poly.get("label"),
        "polygon_area_m2": poly.get("area_m2"),
        "intra_cell_x": round(x_in, 3),
        "intra_cell_y": round(y_in, 3),
        "edge_neighbors": neighbors,
        "is_override": bool(override_sheet),
    }, buf.tobytes()


_URBAN_RCS_CACHE = None
_RCS_BY_CELL_CACHE: dict[str, list[str]] = {}
_COVERED_CELLS_CACHE = None
_COVERED_CSUB_CACHE = None


def _covered_cells():
    """Cells con al menos UN sub-quadrant publicado."""
    global _COVERED_CELLS_CACHE
    if _COVERED_CELLS_CACHE is None:
        sheets = _covered_csub()
        _COVERED_CELLS_CACHE = {cs.rsplit('-', 1)[0] for cs in sheets}
    return _COVERED_CELLS_CACHE


def _covered_csub():
    """Set de (cell-sub) keys que tienen hoja PGOU específica.
    Ej: {'14-K-I', '14-K-II', '15-J-IV', ...}.
    Esto es lo que importa para asegurar que un RC se renderice en el plano correcto."""
    global _COVERED_CSUB_CACHE
    if _COVERED_CSUB_CACHE is None:
        try:
            sheets = pgou.get_sheet_listing()
        except Exception:
            sheets = {}
        out = set()
        for k in sheets:
            parts = k.replace('PLANO_', '').replace('.pdf', '').split('_')
            if len(parts) >= 3:
                out.add(f"{parts[0]}-{parts[1]}-{parts[2]}")
        _COVERED_CSUB_CACHE = out
    return _COVERED_CSUB_CACHE


def _rc_cellsub(rc_x, rc_y):
    """Compute (cell, sub) del RC sin invocar Catastro.
    Devuelve 'col-letter-sub' o None si fuera de rango."""
    from oviedo_rc.config import (
        MALLA_X0, MALLA_YMAX, MALLA_CELL_W, MALLA_CELL_H,
        NS_THRESHOLD, EW_THRESHOLD, SUB_CONVENTION,
    )
    col = int((rc_x - MALLA_X0) // MALLA_CELL_W)
    row = int((MALLA_YMAX - rc_y) // MALLA_CELL_H)
    if not (0 <= row < 25):
        return None, None
    letter = "ABCDEFGHIJKLMNOPQRSTUVWXY"[row]
    x_in = (rc_x - (MALLA_X0 + col * MALLA_CELL_W)) / MALLA_CELL_W
    y_in = (MALLA_YMAX - row * MALLA_CELL_H - rc_y) / MALLA_CELL_H
    compass = ("N" if y_in < NS_THRESHOLD else "S") + ("W" if x_in < EW_THRESHOLD else "E")
    sub = SUB_CONVENTION[compass]
    return f"{col}-{letter}", sub


def _rc_cell(rc_x, rc_y):
    """Compatibilidad: solo cell (col-letter)."""
    cell, _ = _rc_cellsub(rc_x, rc_y)
    return cell


def _all_urban_rcs():
    """RCs urbanos cuyo (cell, sub_quadrant) específico tiene hoja PGOU.
    Antes filtraba solo por cell — eso permitía RCs en sub-cuadrantes sin plano,
    causando 'plano equivocado' cuando se intentaba cargar."""
    global _URBAN_RCS_CACHE
    if _URBAN_RCS_CACHE is None:
        if not COORDS_FILE.exists():
            _URBAN_RCS_CACHE = []
        else:
            coords = json.loads(COORDS_FILE.read_text())
            covered_csub = _covered_csub()
            out = []
            for k, e in coords.items():
                if len(k) != 14 or k[7:10] != "TP6":
                    continue
                cell, sub = _rc_cellsub(e["x"], e["y"])
                if cell is None:
                    continue
                if f"{cell}-{sub}" in covered_csub:
                    out.append(k)
            _URBAN_RCS_CACHE = out
    return _URBAN_RCS_CACHE


def _label_counts_per_cell() -> dict[str, int]:
    """Cuenta labels manuales útiles por cell."""
    if not LABELS_PATH.exists():
        return {}
    labels = json.loads(LABELS_PATH.read_text())
    counts: dict[str, int] = {}
    for l in labels:
        if l.get('action') != 'manual' or not l.get('truth_dxdy'):
            continue
        parts = l.get('sheet_name', '').replace('PLANO_', '').replace('.pdf', '').split('_')
        if len(parts) >= 2:
            cell = f"{parts[0]}-{parts[1]}"
            counts[cell] = counts.get(cell, 0) + 1
    return counts


def random_rc():
    """Smart random: pondera cells inversamente por nº de labels existentes,
    para priorizar zonas poco calibradas. Sigue alcanzando cells saturadas
    con probabilidad pequeña.
    """
    rcs = _all_urban_rcs()
    if not rcs:
        return None
    covered = _covered_cells()
    counts = _label_counts_per_cell()
    # Peso por cell: 1 / (1 + count)^2  → 0 labels=1.0, 1=0.25, 2=0.11, 7=0.015
    weights = {c: 1.0 / (1 + counts.get(c, 0)) ** 2 for c in covered}
    cells_list = list(weights.keys())
    weight_list = [weights[c] for c in cells_list]
    target_cell = random.choices(cells_list, weights=weight_list, k=1)[0]
    rc = random_rc_in_cell(target_cell)
    return rc or random.choice(rcs)


def random_rc_in_cell(cell: str):
    """Random RC en la cell pedida, restringiendo a sub-quadrants con hoja PGOU."""
    if cell in _RCS_BY_CELL_CACHE:
        bucket = _RCS_BY_CELL_CACHE[cell]
        return random.choice(bucket) if bucket else None

    coords = json.loads(COORDS_FILE.read_text())
    target_col, target_letter = cell.split("-")
    target_col = int(target_col)
    target_row = "ABCDEFGHIJKLMNOPQRSTUVWXY".index(target_letter)
    covered_csub = _covered_csub()
    bucket: list[str] = []
    for rc, e in coords.items():
        if len(rc) != 14 or rc[7:10] != "TP6":
            continue
        c, sub = _rc_cellsub(e["x"], e["y"])
        if c == cell and f"{c}-{sub}" in covered_csub:
            bucket.append(rc)
            if len(bucket) >= 200:
                break
    _RCS_BY_CELL_CACHE[cell] = bucket
    return random.choice(bucket) if bucket else None


# ---------- FastAPI app ----------

app = FastAPI(title="snap-validator")


PAGE = """<!DOCTYPE html>
<html lang="es"><head>
<meta charset="UTF-8">
<title>snap validator</title>
<style>
*,*::before,*::after { box-sizing: border-box; }
:root {
  --bg:#050810; --bg2:#0a1024; --fg:#cfe5ff; --fg-bright:#e8f2ff;
  --border:rgba(168,211,255,0.18); --border-strong:rgba(168,211,255,0.4);
  --accent:#a8d3ff; --accent2:#79b8ff;
  --ok:#7eff9c; --warn:#ffb000; --err:#ff8c8c;
}
body { margin:0; background:var(--bg); color:var(--fg); font:13px ui-monospace,Menlo,monospace; min-height:100vh; }
button { background:rgba(168,211,255,0.08); border:1px solid var(--border); color:var(--fg); padding:6px 12px; border-radius:6px; cursor:pointer; font:inherit; font-size:12px; transition:all .12s; }
button:hover { background:rgba(168,211,255,0.18); border-color:var(--accent); }
button.primary { background:var(--accent2); color:var(--bg); border-color:var(--accent2); font-weight:600; }
button.success { background:var(--ok); color:var(--bg); border-color:var(--ok); font-weight:600; }
button.danger  { border-color:rgba(255,140,140,0.4); color:var(--err); }
button.tiny    { padding:2px 6px; font-size:11px; }
input[type=text] { background:var(--bg2); border:1px solid var(--border-strong); color:var(--fg); padding:6px 10px; border-radius:6px; font:inherit; font-size:12px; }
.k { color:rgba(168,211,255,0.5); }

/* layout */
.app {
  display:grid; grid-template-columns: minmax(540px, 1fr) 360px;
  gap:1rem; padding:0.8rem; min-height:100vh;
}
@media (max-width: 1100px) { .app { grid-template-columns: 1fr; } }

header.top {
  grid-column: 1 / -1;
  display:flex; gap:0.8rem; align-items:center; flex-wrap:wrap;
  padding:0.4rem 0.2rem;
  border-bottom:1px solid var(--border);
}
.brand { font-weight:600; letter-spacing:0.18em; text-transform:lowercase; color:var(--fg-bright); }
.brand::before { content:"●"; color:var(--accent2); margin-right:0.3rem; }

#stats-bar {
  flex:1; display:flex; gap:1.2rem; flex-wrap:wrap;
  font-size:11px; letter-spacing:0.05em;
}
#stats-bar .pill { padding:3px 10px; border-radius:999px; background:rgba(168,211,255,0.06); border:1px solid var(--border); }
#stats-bar .pill b { color:var(--fg-bright); font-weight:600; }
#stats-bar .pill.ok b   { color:var(--ok); }
#stats-bar .pill.warn b { color:var(--warn); }

/* Left column: plan + WMS */
.viewer { display:flex; flex-direction:column; gap:0.8rem; }
#image-wrap { position:relative; border:1px solid var(--border); border-radius:8px; overflow:hidden; line-height:0; background:var(--bg2); }
#image-wrap img { display:block; width:100%; height:auto; max-height:70vh; object-fit:contain; }
#overlay { position:absolute; top:0; left:0; width:100%; height:100%; pointer-events:none; }
#overlay polygon { vector-effect:non-scaling-stroke; }
#overlay .truth { pointer-events:auto; cursor:move; }
#overlay .truth:hover { filter:brightness(1.25); }

#wms-wrap {
  display:flex; align-items:center; gap:0.8rem;
  border:1px solid var(--border); border-radius:8px; overflow:hidden;
  background:var(--bg2);
}
#wms-wrap img { display:block; width:240px; height:auto; }
#wms-wrap .wms-info { padding:0 1rem; flex:1; font-size:11px; color:rgba(168,211,255,0.6); }
#wms-wrap .wms-info h4 { margin:0 0 0.4rem; color:var(--fg-bright); font-size:12px; letter-spacing:0.15em; text-transform:uppercase; font-weight:400; }

/* Right column: controls */
.panel { display:flex; flex-direction:column; gap:0.6rem; }
.card { background:rgba(10,16,36,0.55); border:1px solid var(--border); border-radius:8px; padding:0.7rem 0.8rem; }
.card h3 { margin:0 0 0.4rem; font-size:10px; letter-spacing:0.18em; text-transform:uppercase; color:rgba(168,211,255,0.5); font-weight:400; }
.row-tight { display:flex; gap:0.4rem; flex-wrap:wrap; align-items:center; }
.row-tight > * { flex-shrink:0; }
.kvs { display:grid; grid-template-columns: 60px 1fr; gap:3px 0.6rem; font-size:11px; }
.kvs .k { text-align:right; }

.legend { display:flex; gap:0.8rem; font-size:11px; flex-wrap:wrap; }
.legend > span { display:flex; align-items:center; gap:5px; }
.legend .swatch { display:inline-block; width:12px; height:12px; border-radius:2px; }

.shortcuts { font-size:10px; color:rgba(168,211,255,0.4); line-height:1.6; }
.shortcuts kbd { background:rgba(168,211,255,0.1); border:1px solid var(--border); border-radius:3px; padding:1px 5px; font-family:inherit; color:var(--fg-bright); }

#status { font-size:11px; min-height:16px; color:var(--fg); padding:0.4rem 0.6rem; background:rgba(168,211,255,0.04); border-radius:6px; border:1px solid var(--border); }
#status.success { color:var(--ok); border-color:rgba(126,255,156,0.3); }
#status.error   { color:var(--err); border-color:rgba(255,140,140,0.3); }

.actions-grid { display:grid; grid-template-columns:1fr 1fr; gap:0.4rem; }
.actions-grid button { width:100%; padding:8px 0; }

.delta-display {
  font-size:14px; text-align:center; padding:0.4rem;
  background:rgba(126,255,156,0.07); border:1px solid rgba(126,255,156,0.25);
  border-radius:6px; color:var(--ok);
}
.delta-display b { font-size:18px; }

.tag {
  display:inline-block; padding:1px 6px; border-radius:3px;
  font-size:10px; letter-spacing:0.05em;
  background:rgba(168,211,255,0.1); border:1px solid var(--border);
}
.tag.warn { background:rgba(255,176,0,0.12); border-color:rgba(255,176,0,0.4); color:var(--warn); }
.tag.err  { background:rgba(255,140,140,0.12); border-color:rgba(255,140,140,0.4); color:var(--err); }
.tag.ok   { background:rgba(126,255,156,0.12); border-color:rgba(126,255,156,0.4); color:var(--ok); }
</style></head>
<body>

<header class="top">
  <span class="brand">snap validator</span>
  <div id="stats-bar"></div>
</header>

<div class="app">
  <div class="viewer">
    <div id="image-wrap">
      <img id="img" alt="" />
      <svg id="overlay" preserveAspectRatio="xMidYMid meet">
        <polygon id="poly-model" fill="none" stroke="#ff4444" stroke-width="2" stroke-dasharray="6,3" opacity="0.5"/>
        <polygon id="poly-cal" fill="none" stroke="#ffaa44" stroke-width="3" stroke-dasharray="3,3" opacity="0.85"/>
        <polygon id="poly-snap" fill="none" stroke="#3bb3ff" stroke-width="3" stroke-dasharray="6,3" opacity="0.7"/>
        <polygon id="poly-truth" class="truth" fill="rgba(126,255,156,0.15)" stroke="#7eff9c" stroke-width="4"/>
      </svg>
    </div>
    <div id="wms-wrap">
      <img id="wms" alt="catastro" />
      <div class="wms-info">
        <h4>catastro · referencia</h4>
        Si el polígono PGOU está mal alineado, mira aquí donde está realmente la parcela según el catastro.
      </div>
    </div>
  </div>

  <div class="panel">
    <!-- Cargar RC -->
    <div class="card">
      <h3>cargar</h3>
      <div class="row-tight" style="margin-bottom:0.4rem">
        <input id="rc" type="text" placeholder="RC (14 chars)" autocomplete="off" style="flex:1; min-width:120px"/>
        <button onclick="loadRC()">↵</button>
      </div>
      <div class="row-tight">
        <button class="primary" onclick="randomRC()" style="flex:1">🎲 random</button>
        <input id="cellFilter" type="text" placeholder="13-M" style="width:60px"/>
        <button onclick="randomInCell()">↵</button>
      </div>
    </div>

    <!-- Info del RC actual -->
    <div class="card" id="rc-info" style="display:none">
      <h3>RC actual</h3>
      <div class="kvs" id="rc-kvs"></div>
      <div id="rc-tags" style="margin-top:0.4rem; display:flex; gap:0.3rem; flex-wrap:wrap"></div>
      <div id="rc-neighbors" style="margin-top:0.4rem"></div>
    </div>

    <!-- Truth offset display -->
    <div class="card" id="delta-card" style="display:none">
      <h3>truth offset</h3>
      <div class="delta-display">
        <div><b id="delta-px">0,0</b> px</div>
        <div style="font-size:11px; opacity:0.7" id="delta-m">0.0, 0.0 m</div>
      </div>
    </div>

    <!-- Acciones principales -->
    <div class="card">
      <h3>guardar</h3>
      <div class="actions-grid">
        <button class="success" onclick="saveTruth()">⏎ truth · T</button>
        <button class="primary" onclick="label('snap_ok')">snap OK · S</button>
        <button class="primary" onclick="label('model_ok')">modelo OK · M</button>
        <button class="danger" onclick="label('skip')">SKIP</button>
      </div>
      <div class="row-tight" style="margin-top:0.4rem">
        <button onclick="resetTruth()" class="tiny">↺ reset</button>
        <button onclick="toggleWide()" id="wideBtn" class="tiny">📐 ampliar</button>
        <button onclick="randomRC()" class="tiny">⏭ siguiente · R</button>
      </div>
    </div>

    <!-- Lista -->
    <div class="card">
      <h3>lista (opcional)</h3>
      <textarea id="rcList" rows="3" style="width:100%; background:var(--bg2); border:1px solid var(--border); color:var(--fg); padding:6px; border-radius:6px; font:11px ui-monospace,Menlo,monospace; resize:vertical" placeholder="RCs separados por línea o coma"></textarea>
      <div class="row-tight" style="margin-top:0.4rem">
        <button onclick="startList()" class="tiny">▶ iniciar</button>
        <button onclick="prevInList()" class="tiny">←</button>
        <button onclick="nextInList()" class="tiny">→</button>
        <span class="k" id="listStatus" style="font-size:10px"></span>
      </div>
    </div>

    <!-- Leyenda + atajos -->
    <div class="card">
      <h3>leyenda</h3>
      <div class="legend">
        <span><span class="swatch" style="background:#ff4444"></span>modelo + cal</span>
        <span><span class="swatch" style="background:#3bb3ff"></span>snap auto</span>
        <span><span class="swatch" style="background:#7eff9c"></span>truth (arrastra)</span>
      </div>
      <div class="shortcuts" style="margin-top:0.6rem">
        <kbd>T</kbd> truth · <kbd>M</kbd> model · <kbd>S</kbd> snap · <kbd>R</kbd> random · <kbd>↺</kbd> reset<br>
        <kbd>←↑→↓</kbd> 1px · <kbd>shift</kbd>+flecha 10px
      </div>
    </div>

    <div id="status">listo</div>
  </div>
</div>

<script>
const state = { rc: null, meta: null, dxdy: [0, 0], dragging: false, dragStart: null, dragStartDxdy: null };

const overlay = document.getElementById('overlay');
const polyModel = document.getElementById('poly-model');
const polySnap = document.getElementById('poly-snap');
const polyTruth = document.getElementById('poly-truth');

function fmtPoints(pts) { return pts.map(p => p.join(',')).join(' '); }
function applyDxdy() {
    const [dx, dy] = state.dxdy;
    polyTruth.setAttribute('transform', `translate(${dx} ${dy})`);
    document.getElementById('delta-card').style.display = '';
    document.getElementById('delta-px').textContent = `${dx},${dy}`;
    document.getElementById('delta-m').textContent = `${(dx/11.81).toFixed(2)}, ${(dy/11.81).toFixed(2)} m`;
    setStatus(`drag desde modelo+cal: ${dx},${dy} px (${(dx/11.81).toFixed(1)}, ${(dy/11.81).toFixed(1)} m)`, '');
}

function setStatus(msg, kind) {
    const s = document.getElementById('status');
    s.textContent = msg;
    s.className = kind || '';
}
function resetTruth() {
    state.dxdy = [0, 0];
    applyDxdy();
}

let wideMode = false;

function toggleWide() {
    wideMode = !wideMode;
    document.getElementById('wideBtn').textContent = wideMode ? '📐 normal' : '📐 ampliar área';
    if (state.rc) loadRC(state.rc);
}

async function loadRC(rc, _retry = 0, overrideSheet = null) {
    rc = rc || document.getElementById('rc').value.trim().toUpperCase();
    if (!rc) return;
    document.getElementById('rc').value = rc;
    setStatus(overrideSheet ? `cargando override ${overrideSheet}…` : 'cargando...', '');
    const params = [];
    if (overrideSheet) params.push('sheet=' + encodeURIComponent(overrideSheet));
    if (wideMode) params.push('wide=true');
    const qs = params.length ? '?' + params.join('&') : '';
    const r = await fetch('/api/load/' + rc + qs);
    if (!r.ok) {
        const msg = await r.text();
        if (msg.includes('No se encontró hoja') || msg.includes('PGOU')) {
            if (listState.rcs.length > 0 && listState.idx + 1 < listState.rcs.length) {
                setStatus(`${rc}: sin hoja PGOU, saltando...`, '');
                setTimeout(nextInList, 200); return;
            }
            if (_retry < 5) {
                setStatus(`${rc}: sin hoja, intentando otro...`, '');
                setTimeout(() => randomRC(), 200); return;
            }
        }
        setStatus('ERROR: ' + msg, 'error');
        return;
    }
    const m = state.meta = await r.json();
    state.rc = rc;
    state.dxdy = [0, 0];

    // load image and configure overlay
    const img = document.getElementById('img');
    img.src = '/api/image/' + rc + '?t=' + Date.now();
    document.getElementById('wms').src = '/api/wms/' + rc + '?t=' + Date.now();
    img.onload = () => {
        const [w, h] = m.crop_size_px;
        overlay.setAttribute('viewBox', `0 0 ${w} ${h}`);
        // poly_model_in_crop ya tiene cal aplicada (es lo que ve el usuario)
        polyModel.setAttribute('points', fmtPoints(m.polygon_model_in_crop));
        // Ocultar poly-cal: ahora poly-model YA es bare+cal. Sería redundante.
        document.getElementById('poly-cal').style.display = 'none';
        if (m.snap_score > 0) {
            polySnap.setAttribute('points', fmtPoints(m.polygon_snap_in_crop));
            polySnap.style.display = '';
        } else {
            polySnap.style.display = 'none';
        }
        // truth empieza en posición del modelo (que YA tiene cal). dxdy=0,0.
        polyTruth.setAttribute('points', fmtPoints(m.polygon_model_in_crop));
        state.dxdy = [0, 0];
        applyDxdy();
    };

    // RC info card
    document.getElementById('rc-info').style.display = '';
    const overrideTag = m.is_override ? '<span class="tag warn">OVERRIDE</span>' : '';
    document.getElementById('rc-kvs').innerHTML =
        `<span class="k">RC</span><span>${m.rc}</span>` +
        `<span class="k">addr</span><span>${m.address || '<i style="opacity:.5">(sin dir.)</i>'}</span>` +
        `<span class="k">cell</span><span>${m.cell} · sub ${m.sub_quadrant}</span>` +
        `<span class="k">sheet</span><span>${m.sheet_name} ${overrideTag}</span>` +
        `<span class="k">parcela</span><span>label ${m.polygon_label || '?'} · ${m.polygon_area_m2 || '?'} m²</span>` +
        `<span class="k">snap</span><span>dx=${m.snap_dxdy[0]} dy=${m.snap_dxdy[1]} <span class="k">score</span> ${m.snap_score.toFixed(2)}</span>`;

    // Tags / status
    const stats = await fetch('/api/stats').then(r => r.json());
    const cov = await fetch('/api/coverage').then(r => r.json());
    let tagsHtml = '';
    if (m && m.cell) {
        const cellInfo = cov.cells.find(c => c.cell === m.cell);
        if (cellInfo) {
            if (cellInfo.priority === 'vacía') tagsHtml += '<span class="tag err">🆕 cell virgen · alto valor</span>';
            else if (cellInfo.priority === 'escasa') tagsHtml += `<span class="tag warn">⚠ cell escasa · ${cellInfo.labels} labels</span>`;
            else tagsHtml += `<span class="tag ok">✓ cell sólida · ${cellInfo.labels} labels</span>`;
        }
    }
    document.getElementById('rc-tags').innerHTML = tagsHtml;

    // Edge neighbors
    let neighborsHtml = '';
    if (m.edge_neighbors && m.edge_neighbors.length) {
        neighborsHtml = '<div style="font-size:10px; color:rgba(168,211,255,0.5); margin-bottom:0.3rem">borde detectado — probar plano vecino:</div>' +
            '<div class="row-tight">' +
            m.edge_neighbors.map(n =>
                `<button class="tiny" onclick="loadOverride('${m.rc}', '${n.sheet_name}')">${n.cell}-${n.sub_quadrant}</button>`
            ).join('') +
            (m.is_override ? `<button class="tiny" onclick="loadRC('${m.rc}')">↺ original</button>` : '') +
            '</div>';
    }
    document.getElementById('rc-neighbors').innerHTML = neighborsHtml;

    // Stats bar
    const useful = stats.manual;
    const total_cells = cov.n_covered;
    const cells_touched = cov.n_ok + cov.n_sparse;
    const pct = Math.round(cells_touched / total_cells * 100);
    document.getElementById('stats-bar').innerHTML =
        `<span class="pill"><b>${useful}</b> labels</span>` +
        `<span class="pill ok"><b>${cov.n_ok}</b> cells sólidas</span>` +
        `<span class="pill warn"><b>${cov.n_sparse}</b> escasas</span>` +
        `<span class="pill"><b>${cov.n_unlabeled}</b> sin tocar · ${pct}% calibrado</span>`;

    setStatus('OK · arrastra el verde para alinear (o pulsa M/S si modelo/snap ya cuadran)', '');
}

function loadOverride(rc, sheet) {
    return loadRC(rc, 0, sheet);
}

async function randomRC() {
    const r = await fetch('/api/random');
    const j = await r.json();
    if (j.rc) loadRC(j.rc);
}

async function randomInCell() {
    const cell = document.getElementById('cellFilter').value.trim().toUpperCase();
    if (!cell) return document.getElementById('status').textContent = 'pon una cell, ej 13-M';
    const r = await fetch('/api/random_in_cell?cell=' + encodeURIComponent(cell));
    const j = await r.json();
    if (j.rc) loadRC(j.rc);
    else document.getElementById('status').textContent = `no encontré RCs en cell ${cell}`;
}

// === lista de RCs ===
let listState = { rcs: [], idx: -1 };

function parseList() {
    const txt = document.getElementById('rcList').value;
    const tokens = txt.split(/[\s,;]+/).map(s => s.trim().toUpperCase()).filter(s => s.length === 14);
    return tokens;
}

function refreshListStatus() {
    if (listState.rcs.length === 0) {
        document.getElementById('listStatus').textContent = '';
        return;
    }
    document.getElementById('listStatus').textContent =
        `lista: ${listState.idx + 1}/${listState.rcs.length}`;
}

function startList() {
    listState.rcs = parseList();
    listState.idx = -1;
    if (listState.rcs.length === 0) {
        document.getElementById('listStatus').textContent = 'lista vacía o todos los RCs son inválidos';
        return;
    }
    nextInList();
}

function nextInList() {
    if (listState.rcs.length === 0) return;
    listState.idx = Math.min(listState.idx + 1, listState.rcs.length - 1);
    loadRC(listState.rcs[listState.idx]);
    refreshListStatus();
}

function prevInList() {
    if (listState.rcs.length === 0) return;
    listState.idx = Math.max(listState.idx - 1, 0);
    loadRC(listState.rcs[listState.idx]);
    refreshListStatus();
}

// drag del polígono verde
function getMouseInImageCoords(e) {
    const img = document.getElementById('img');
    const rect = img.getBoundingClientRect();
    const [w, h] = state.meta.crop_size_px;
    return {
        x: (e.clientX - rect.left) * w / rect.width,
        y: (e.clientY - rect.top) * h / rect.height,
    };
}

polyTruth.addEventListener('mousedown', e => {
    e.preventDefault();
    state.dragging = true;
    state.dragStart = getMouseInImageCoords(e);
    state.dragStartDxdy = [...state.dxdy];
});

window.addEventListener('mousemove', e => {
    if (!state.dragging) return;
    const cur = getMouseInImageCoords(e);
    state.dxdy = [
        Math.round(state.dragStartDxdy[0] + (cur.x - state.dragStart.x)),
        Math.round(state.dragStartDxdy[1] + (cur.y - state.dragStart.y)),
    ];
    applyDxdy();
});

window.addEventListener('mouseup', () => {
    state.dragging = false;
});

// flechas para precisión fina
document.addEventListener('keydown', e => {
    if (e.target.tagName === 'INPUT') return;
    const step = e.shiftKey ? 10 : 1;
    if (e.key === 'ArrowLeft')  { state.dxdy[0] -= step; applyDxdy(); e.preventDefault(); }
    else if (e.key === 'ArrowRight') { state.dxdy[0] += step; applyDxdy(); e.preventDefault(); }
    else if (e.key === 'ArrowUp')    { state.dxdy[1] -= step; applyDxdy(); e.preventDefault(); }
    else if (e.key === 'ArrowDown')  { state.dxdy[1] += step; applyDxdy(); e.preventDefault(); }
    else if (e.key === 'm' || e.key === 'M') label('model_ok');
    else if (e.key === 's' || e.key === 'S') label('snap_ok');
    else if (e.key === 't' || e.key === 'T') saveTruth();
    else if (e.key === 'r' || e.key === 'R') randomRC();
});

async function saveTruth() {
    label('manual');
}

async function label(action) {
    if (!state.rc) return;
    const body = { action };
    if (action === 'manual') body.truth_dxdy = state.dxdy;
    const r = await fetch('/api/label/' + state.rc, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
    });
    if (r.ok) {
        const j = await r.json();
        document.getElementById('status').textContent =
            `✓ ${action} dx=${j.truth_dxdy?.[0] ?? '-'} dy=${j.truth_dxdy?.[1] ?? '-'} → siguiente`;
        // Si hay lista activa, avanza en ella; si no, random.
        const hasNext = listState.rcs.length > 0 && listState.idx + 1 < listState.rcs.length;
        setTimeout(hasNext ? nextInList : randomRC, 400);
    } else {
        document.getElementById('status').textContent = 'ERROR: ' + (await r.text());
    }
}

randomRC();
</script>
</body></html>
"""


@app.get("/", response_class=HTMLResponse)
def index():
    return PAGE


_CACHE: dict[str, dict] = {}  # rc → meta (también guarda model_px etc.)
_IMG_CACHE: dict[str, bytes] = {}


class LabelBody(BaseModel):
    action: str  # model_ok | snap_ok | manual | skip
    truth_dxdy: list[float] | None = None


@app.get("/api/load/{rc}")
def api_load(rc: str, wide: bool = False, sheet: str | None = None):
    rc = rc.strip().upper()
    try:
        meta, png = render_for_validation(rc, wide=wide, override_sheet=sheet)
    except Exception as e:
        raise HTTPException(400, str(e))
    cache_key = f"{rc}__{sheet}" if sheet else rc
    _CACHE[cache_key] = meta
    _IMG_CACHE[cache_key] = png
    # tambien actualizar la cache base para /api/image y /api/label
    _CACHE[rc] = meta
    _IMG_CACHE[rc] = png
    return JSONResponse(meta)


@app.get("/api/image/{rc}")
def api_image(rc: str):
    rc = rc.strip().upper()
    if rc not in _IMG_CACHE:
        raise HTTPException(404)
    return Response(_IMG_CACHE[rc], media_type="image/png")


@app.get("/api/wms/{rc}")
def api_wms(rc: str):
    """WMS catastral del bbox de la parcela. Útil cuando el polígono PGOU
    está mal posicionado y necesitas referencia visual de dónde está
    realmente la parcela."""
    rc = rc.strip().upper()
    rc14 = rc[:14]
    try:
        poly = catastro.get_parcel_polygon(rc14)
        if not poly or not poly.get("polygon_utm"):
            raise HTTPException(404, "sin polígono catastral")
        pts = poly["polygon_utm"]
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        pad = 60
        xmin = min(xs) - pad; xmax = max(xs) + pad
        ymin = min(ys) - pad; ymax = max(ys) + pad
        png = wms.get(xmin, ymin, xmax, ymax, w=500)
        return Response(png, media_type="image/png")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/random")
def api_random():
    rc = random_rc()
    return {"rc": rc}


@app.get("/api/random_in_cell")
def api_random_in_cell(cell: str):
    rc = random_rc_in_cell(cell.strip().upper())
    return {"rc": rc, "cell": cell}


@app.get("/api/coverage")
def api_coverage():
    """Estado de calibración. Cuenta por cell (no por sub) para compatibilidad."""
    covered = _covered_cells()
    counts = _label_counts_per_cell()
    items = []
    for c in sorted(covered):
        items.append({
            "cell": c,
            "labels": counts.get(c, 0),
            "priority": "vacía" if counts.get(c, 0) == 0
                       else "escasa" if counts.get(c, 0) <= 2
                       else "ok",
        })
    csub_total = len(_covered_csub())
    return {
        "n_covered": len(covered),
        "n_csub_total": csub_total,
        "n_unlabeled": sum(1 for x in items if x["labels"] == 0),
        "n_sparse": sum(1 for x in items if 0 < x["labels"] <= 2),
        "n_ok": sum(1 for x in items if x["labels"] >= 3),
        "cells": items,
    }


@app.get("/api/stats")
def api_stats():
    labels = _load_labels()
    return {
        "labeled": len(labels),
        "model_ok": sum(1 for l in labels if l.get("action") == "model_ok"),
        "snap_ok": sum(1 for l in labels if l.get("action") == "snap_ok"),
        "manual": sum(1 for l in labels if l.get("action") == "manual"),
        "skip": sum(1 for l in labels if l.get("action") == "skip"),
    }


@app.post("/api/label/{rc}")
def api_label(rc: str, body: LabelBody):
    rc = rc.strip().upper()
    if rc not in _CACHE:
        raise HTTPException(400, "rc no cargado, llama /api/load primero")
    meta = _CACHE[rc]

    label = {
        "rc": rc,
        "address": meta["address"],
        "sheet_name": meta["sheet_name"],
        "model_center_px": meta["model_center_px"],
        "snap_dxdy": meta["snap_dxdy"],
        "snap_score": meta["snap_score"],
        "cal_applied_dxdy": meta.get("cal_applied_dxdy", [0, 0]),
        "action": body.action,
        "truth_dxdy": None,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }

    if body.action == "model_ok":
        label["truth_dxdy"] = [0, 0]
    elif body.action == "snap_ok":
        label["truth_dxdy"] = list(meta["snap_dxdy"])
    elif body.action == "manual":
        if not body.truth_dxdy:
            raise HTTPException(400, "manual requires truth_dxdy")
        label["truth_dxdy"] = [int(round(v)) for v in body.truth_dxdy]
    elif body.action == "skip":
        pass
    else:
        raise HTTPException(400, f"unknown action: {body.action}")

    # Replace existing label for this RC if any
    labels = _load_labels()
    labels = [l for l in labels if l["rc"] != rc]
    labels.append(label)
    _save_labels(labels)
    return {"ok": True, "truth_dxdy": label["truth_dxdy"]}


if __name__ == "__main__":
    import uvicorn
    print(f"\n  snap validator listo en http://127.0.0.1:8765")
    print(f"  labels → {LABELS_PATH}\n")
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="warning")
