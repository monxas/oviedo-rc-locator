"""
Validator UI · pantalla única para corregir snap del PGOU.

Servicio FastAPI: corre en VM puerto 9103, expuesto como validator.iarquitectos.com.

Diseño:
- Layout horizontal 3 zonas, sin scroll.
- Izquierda: WMS catastral (referencia visual).
- Centro: PGOU zoom con polígono catastral rojo (snap aplicado) + verde arrastrable.
- Derecha: ficha (RC, dirección, snap_score, reliability) + 3 botones grandes.
- Snap siempre activo. Si snap_score < 0.30 → banner rojo "snap incierto".
- Atajos: A = aceptar, X = error_grande, S = skip.

Acciones → JSON en data/validator_labels.json:
  accept (sin mover): {action:accept, dxdy:[0,0]}
  accept (movido):    {action:accept, dxdy:[Δx,Δy]}  <- nueva corrección
  reject:             {action:reject_unfixable}      <- flagged para análisis
  skip:               {action:skip}                  <- decisión diferida
"""
import io
import json
import os
import random
import sys
import time
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel, Field

ROOT = Path.home() / "oviedo-rc-locator"
sys.path.insert(0, str(ROOT / "src"))
import cv2  # noqa: E402
import numpy as np  # noqa: E402

from oviedo_rc import calibration, catastro, geom, pgou, render, snap as snap_mod, wms  # noqa: E402
from oviedo_rc.config import (  # noqa: E402
    BODY_W_M, BODY_H_M,
    MALLA_X0, MALLA_YMAX, MALLA_CELL_W, MALLA_CELL_H,
    MALLA_MARG_X, MALLA_MARG_Y,
    COORDS_FILE,
)


def _anchor_utm(col, row_idx, compass):
    sub_x_off = 0 if "W" in compass else MALLA_CELL_W / 2
    sub_y_off = 0 if "N" in compass else MALLA_CELL_H / 2
    body_x_min = MALLA_X0 + col * MALLA_CELL_W + sub_x_off - MALLA_MARG_X
    body_y_max = MALLA_YMAX - row_idx * MALLA_CELL_H - sub_y_off + MALLA_MARG_Y
    return body_x_min, body_y_max

ENV_FILE = ROOT / ".validator.env"
LABELS_PATH = ROOT / "data" / "validator_labels.json"
LABELS_PATH.parent.mkdir(exist_ok=True, parents=True)
NATIVE_CROP = 3600   # crop nativo en px PGOU (cubre ~317 m a 0.088 m/px)
DISPLAY_CROP = 1800  # tras downscale 2× → tamaño PNG transferido

# auto-recalibración cada N aceptaciones (drag != 0 o exact)
RECAL_THRESHOLD = 30
_RECAL_COUNTER_FILE = ROOT / "data" / ".recal_counter"

SNAP_CONFIDENT_THRESHOLD = 0.30


# ---------- env ----------
def load_env():
    env = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line: continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    for k, v in os.environ.items():
        if k.startswith("VALIDATOR_"):
            env[k] = v
    return env


ENV = load_env()
TOKEN = ENV.get("VALIDATOR_TOKEN", "")
HOST = ENV.get("VALIDATOR_HOST", "127.0.0.1")
PORT = int(ENV.get("VALIDATOR_PORT", "9103"))

if not TOKEN:
    raise RuntimeError("VALIDATOR_TOKEN no definido en .validator.env")


app = FastAPI(
    title="Oviedo RC Validator",
    version="1.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


async def auth(request: Request, token: Optional[str] = Query(None)):
    candidate = None
    h = request.headers.get("authorization", "")
    if h.lower().startswith("bearer "):
        candidate = h.split(None, 1)[1].strip()
    elif token:
        candidate = token
    elif request.cookies.get("iarq_validator"):
        candidate = request.cookies["iarq_validator"]
    if candidate != TOKEN:
        raise HTTPException(401, "unauthorized")
    return True


# ---------- labels storage ----------
def load_labels() -> list:
    if not LABELS_PATH.exists(): return []
    return json.loads(LABELS_PATH.read_text(encoding="utf-8"))


def save_labels(labels):
    LABELS_PATH.write_text(json.dumps(labels, indent=2, ensure_ascii=False))


def labeled_rcs() -> set:
    return {l["rc"] for l in load_labels()}


# ---------- helpers de render ----------
def _coords_local():
    """Lee coords_local.json del caché de oviedo_rc."""
    cache = Path.home() / ".cache" / "oviedo_rc"
    coords_file = cache / COORDS_FILE.name
    if not coords_file.exists(): return {}
    return json.loads(coords_file.read_text(encoding="utf-8"))


COORDS = _coords_local()


def _covered_csub() -> set:
    """Set de (cell-sub) que tienen hoja PGOU específica."""
    try:
        sheets = pgou.get_sheet_listing()
    except Exception:
        sheets = {}
    out = set()
    for k in sheets:
        parts = k.replace("PLANO_", "").replace(".pdf", "").split("_")
        if len(parts) >= 3:
            out.add(f"{parts[0]}-{parts[1]}-{parts[2]}")
    return out


_COVERED_CSUB: Optional[set] = None


def _rc_cellsub(rc_x, rc_y):
    """(cell, sub_quadrant) del RC desde UTM, sin red."""
    from oviedo_rc.config import (
        MALLA_X0, MALLA_YMAX, MALLA_CELL_W, MALLA_CELL_H,
        NS_THRESHOLD, EW_THRESHOLD, SUB_CONVENTION,
    )
    col = int((rc_x - MALLA_X0) // MALLA_CELL_W)
    row = int((MALLA_YMAX - rc_y) // MALLA_CELL_H)
    if not (0 <= row < 25): return None, None
    letter = "ABCDEFGHIJKLMNOPQRSTUVWXY"[row]
    x_in = (rc_x - (MALLA_X0 + col * MALLA_CELL_W)) / MALLA_CELL_W
    y_in = (MALLA_YMAX - row * MALLA_CELL_H - rc_y) / MALLA_CELL_H
    compass = ("N" if y_in < NS_THRESHOLD else "S") + ("W" if x_in < EW_THRESHOLD else "E")
    sub = SUB_CONVENTION[compass]
    return f"{col}-{letter}", sub


def _random_rc() -> str:
    """RC aleatorio del cache, urbano válido + cell-sub con hoja PGOU."""
    global _COVERED_CSUB
    if _COVERED_CSUB is None:
        _COVERED_CSUB = _covered_csub()

    from oviedo_rc.geom import validate_rc
    from oviedo_rc.errors import RCError
    keys = list(COORDS.keys())
    done = labeled_rcs()
    random.shuffle(keys)
    for k in keys:
        rc = k + "0001AA"
        if rc in done: continue
        try:
            validate_rc(rc)
        except RCError:
            continue
        # ¿está su cell-sub cubierto por hoja PGOU?
        rec = COORDS[k]
        if isinstance(rec, dict):
            x, y = rec.get("x"), rec.get("y")
        else:
            x, y = rec[0], rec[1]
        cell, sub = _rc_cellsub(x, y)
        if cell is None: continue
        if f"{cell}-{sub}" not in _COVERED_CSUB: continue
        return rc
    raise HTTPException(503, "no quedan RCs válidos pendientes")


def _generate_for_rc(rc: str) -> dict:
    """Pipeline copiado de scripts/validate_snap.py:render_for_validation, simplificado."""
    rc = rc.upper().strip()
    info = geom.locate(rc)
    rc14 = geom.validate_rc(rc)
    sheet = info["sheet_name"]
    if not sheet: raise HTTPException(404, "RC sin hoja PGOU asociada")

    pdf_path = pgou.fetch_sheet_pdf(sheet)
    img, _, _ = render.render_pdf_page(pdf_path)
    body_rect = render.detect_body_rect(img)

    poly = catastro.get_parcel_polygon(rc14)
    if not poly or not poly.get("polygon_utm"):
        raise HTTPException(404, f"sin polígono catastral para {rc}")

    col_letter = info["cell"].split("-")
    col = int(col_letter[0])
    row_idx = "ABCDEFGHIJKLMNOPQRSTUVWXY".index(col_letter[1])
    compass = info["sub_compass"]
    eff_cell = info["cell"]; eff_sub = info["sub_quadrant"]
    anchor = _anchor_utm(col, row_idx, compass)
    poly_px_model = render.utm_polygon_to_pixel(poly["polygon_utm"], body_rect, anchor, compass)

    cal_dx, cal_dy = calibration.offset_for(eff_cell, eff_sub)
    poly_px_model = [(x + cal_dx, y + cal_dy) for x, y in poly_px_model]

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    dx_snap, dy_snap, snap_score = snap_mod.snap(gray, poly_px_model)
    poly_px_snap = [(x + dx_snap, y + dy_snap) for x, y in poly_px_model]

    # crop nativo centrado en polígono SNAP (lo que el usuario ve en verde)
    # — así el verde queda siempre centrado en pantalla incluso si snap+cal moveran mucho
    pts = np.array(poly_px_snap)
    cx, cy = pts.mean(axis=0)
    H, W = img.shape[:2]
    x0 = int(max(0, cx - NATIVE_CROP // 2))
    y0 = int(max(0, cy - NATIVE_CROP // 2))
    x1 = min(W, x0 + NATIVE_CROP)
    y1 = min(H, y0 + NATIVE_CROP)
    crop_native = img[y0:y1, x0:x1].copy()
    ch, cw = crop_native.shape[:2]
    if ch < NATIVE_CROP or cw < NATIVE_CROP:
        pad = np.full((NATIVE_CROP, NATIVE_CROP, 3), 255, dtype=np.uint8)
        pad[:ch, :cw] = crop_native
        crop_native = pad

    # downscale a DISPLAY_CROP para transferencia
    crop = cv2.resize(crop_native, (DISPLAY_CROP, DISPLAY_CROP), interpolation=cv2.INTER_AREA)
    scale = DISPLAY_CROP / NATIVE_CROP  # 0.5

    def shift_and_scale(pts, off_x, off_y):
        return [[int((x - off_x) * scale), int((y - off_y) * scale)] for x, y in pts]

    poly_snap_in_crop = shift_and_scale(poly_px_snap, x0, y0)

    ok, buf = cv2.imencode(".png", crop)
    crop_png = buf.tobytes()

    # WMS catastral
    X, Y = info["utm"]
    try:
        wms_png = wms.get(X - 150, Y - 150, X + 150, Y + 150, w=600)
    except Exception:
        wms_png = b""

    # m/px del PGOU nativo
    body_w_px = body_rect[2] - body_rect[0]
    pgou_native_m_per_px = float(BODY_W_M) / float(body_w_px) if body_w_px else 0.088
    # m/px del crop transferido (downscale 2×)
    crop_m_per_px = pgou_native_m_per_px / scale  # ≈ 0.17

    cal = calibration.quality_for(eff_cell, eff_sub)
    return {
        "rc": rc,
        "address": info.get("address", ""),
        "sheet": sheet,
        "cell": eff_cell,
        "sub_quadrant": eff_sub,
        "poly_snap": poly_snap_in_crop,
        "snap_score": float(snap_score),
        "snap_dxdy": [int(dx_snap), int(dy_snap)],
        "cal_dxdy": [int(cal_dx), int(cal_dy)],
        "calibration_quality": cal,
        "snap_confident": float(snap_score) >= SNAP_CONFIDENT_THRESHOLD,
        "crop_size_px": DISPLAY_CROP,
        "crop_m_per_px": crop_m_per_px,
        "wms_size_px": 600,
        "wms_m_per_px": 0.5,  # 300m / 600px
        "crop_png": crop_png,
        "wms_png": wms_png,
    }


# in-memory cache de la última generación
_CACHE: dict = {}


@app.get("/health")
async def health():
    return {"ok": True, "service": "validator", "labels_count": len(load_labels())}


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    # 1×1 PNG transparente (silencia el 404 del navegador)
    return Response(
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06"
        b"\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01"
        b"\x01\x00\x05\x00\x01\x0d\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82",
        media_type="image/png",
    )


@app.get("/", response_class=HTMLResponse)
async def home():
    """HTML público — el token sólo se requiere para /api/*. La UI gestiona el token via localStorage."""
    return INDEX_HTML


@app.get("/api/next")
async def next_rc(rc: Optional[str] = None, _=Depends(auth)):
    """Devuelve datos del siguiente RC (random o el indicado)."""
    if rc:
        rc = rc.strip().upper()
    if not rc:
        rc = _random_rc()
    data = _generate_for_rc(rc)
    _CACHE[rc] = data
    return {
        "rc": data["rc"],
        "address": data["address"],
        "sheet": data["sheet"],
        "cell": data["cell"],
        "sub_quadrant": data["sub_quadrant"],
        "poly_snap": data["poly_snap"],
        "snap_score": data["snap_score"],
        "snap_dxdy": data["snap_dxdy"],
        "cal_dxdy": data["cal_dxdy"],
        "calibration_quality": data["calibration_quality"],
        "snap_confident": data["snap_confident"],
        "crop_size_px": data["crop_size_px"],
        "crop_m_per_px": data["crop_m_per_px"],
        "wms_size_px": data["wms_size_px"],
        "wms_m_per_px": data["wms_m_per_px"],
        "crop_url": f"/api/img/{data['rc']}/crop",
        "wms_url": f"/api/img/{data['rc']}/wms",
    }


@app.get("/api/img/{rc}/{kind}")
async def img(rc: str, kind: str, _=Depends(auth)):
    data = _CACHE.get(rc)
    if not data: raise HTTPException(404, "cache miss — call /api/next first")
    if kind == "crop":
        return Response(data["crop_png"], media_type="image/png")
    if kind == "wms":
        return Response(data["wms_png"], media_type="image/png")
    raise HTTPException(404)


class LabelReq(BaseModel):
    rc: str
    action: str = Field(..., pattern="^(accept|reject_unfixable|skip)$")
    dxdy: list[int] = Field(default_factory=lambda: [0, 0])
    snap_score: float = 0.0
    snap_dxdy: list[int] = Field(default_factory=lambda: [0, 0])
    cal_dxdy: list[int] = Field(default_factory=lambda: [0, 0])
    comment: str = ""


def _accept_counter() -> int:
    if _RECAL_COUNTER_FILE.exists():
        try: return int(_RECAL_COUNTER_FILE.read_text())
        except Exception: return 0
    return 0


def _bump_accept_counter() -> int:
    n = _accept_counter() + 1
    _RECAL_COUNTER_FILE.write_text(str(n))
    return n


def _reset_accept_counter():
    _RECAL_COUNTER_FILE.write_text("0")


def _trigger_recalibration():
    """Spawn recal + restart de servicios. No bloquea el request actual."""
    import subprocess
    cmd = (
        f"sleep 1 && "
        f"{ROOT}/.venv/bin/python {ROOT}/scripts/recalibrate.py > {ROOT}/recal.log 2>&1 && "
        f"systemctl --user restart locator-api validator-ui"
    )
    subprocess.Popen(["bash", "-c", cmd], start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@app.post("/api/label")
async def label(req: LabelReq, _=Depends(auth)):
    labels = load_labels()
    labels = [l for l in labels if l.get("rc") != req.rc]
    labels.append({
        "rc": req.rc,
        "action": req.action,
        "dxdy": req.dxdy,
        "snap_score": req.snap_score,
        "snap_dxdy": req.snap_dxdy,
        "cal_dxdy": req.cal_dxdy,
        "comment": req.comment,
        "ts": time.time(),
    })
    save_labels(labels)
    # incrementar contador sólo en accept (skip y reject no aportan)
    recalibrated = False
    if req.action == "accept":
        n = _bump_accept_counter()
        if n >= RECAL_THRESHOLD:
            _reset_accept_counter()
            _trigger_recalibration()
            recalibrated = True
    return {"ok": True, "total": len(labels),
            "accept_counter": _accept_counter(),
            "recal_threshold": RECAL_THRESHOLD,
            "recalibrated": recalibrated}


@app.get("/api/stats")
async def stats(_=Depends(auth)):
    labels = load_labels()
    by_action = {}
    for l in labels:
        by_action[l["action"]] = by_action.get(l["action"], 0) + 1
    return {"total": len(labels), "by_action": by_action,
            "rcs_in_cache": len(COORDS), "pending": len(COORDS) - len(labels),
            "accept_counter": _accept_counter(),
            "recal_threshold": RECAL_THRESHOLD}


INDEX_HTML = """<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,user-scalable=no,viewport-fit=cover">
<meta name="theme-color" content="#181818">
<title>RC Validator</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; -webkit-tap-highlight-color: transparent; }
html, body { height: 100%; }
body { font-family: -apple-system, system-ui, sans-serif; background: #181818; color: #e8e8e8; overflow: hidden;
       overscroll-behavior: none; touch-action: none; }
header { padding: 8px 14px; background: #222; border-bottom: 1px solid #333; display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }
header h1 { font-size: 13px; font-weight: 600; }
.rc-info { font-size: 12px; color: #aaa; flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.rc-info b { color: #fff; }
.banner { padding: 4px 10px; border-radius: 4px; font-size: 11px; font-weight: 600; white-space: nowrap; }
.banner.ok { background: #1f5d2b; }
.banner.warn { background: #8a2929; }
main { display: grid; grid-template-columns: 1fr 1fr 280px; height: calc(100vh - 88px); gap: 1px; background: #222; }
.zoom-bar { display: flex; align-items: center; gap: 10px; padding: 6px 12px; background: #1d1d1d; border-bottom: 1px solid #333; }
.zoom-bar label { font-size: 10px; color: #888; text-transform: uppercase; letter-spacing: 1px; }
.zoom-bar input[type=range] { flex: 1; accent-color: #1f7a36; height: 24px; }
.zoom-bar .scale-label { font-size: 11px; color: #fff; font-family: ui-monospace, monospace; min-width: 80px; text-align: right; }
.zoom-bar button { padding: 6px 12px; background: #333; border: none; color: #fff; border-radius: 4px; cursor: pointer; font-size: 12px; }
.pane { background: #181818; display: flex; flex-direction: column; overflow: hidden; }
.pane h2 { font-size: 10px; text-transform: uppercase; letter-spacing: 1px; color: #888; padding: 6px 10px; background: #1f1f1f; flex-shrink: 0; }
.canvas-wrap { flex: 1; display: flex; align-items: center; justify-content: center; position: relative; overflow: auto; background: #0c0c0c;
               touch-action: pan-x pan-y; /* permite pan con un dedo, pinch lo capturamos nosotros */ }
.canvas-inner { position: relative; flex-shrink: 0; }
.canvas-inner img { display: block; image-rendering: crisp-edges; user-select: none; -webkit-user-drag: none; pointer-events: none; }
#overlay { position: absolute; top: 0; left: 0; pointer-events: none; }
#overlay polygon.draggable { pointer-events: auto; cursor: grab; touch-action: none; }
.side { padding: 12px; gap: 8px; display: flex; flex-direction: column; }
.kv { display: flex; justify-content: space-between; font-size: 11px; padding: 3px 0; border-bottom: 1px solid #2a2a2a; }
.kv label { color: #888; }
.kv span { color: #fff; font-family: ui-monospace, monospace; }
.btn { width: 100%; padding: 16px; font-size: 16px; font-weight: 600; border: none; border-radius: 8px;
       cursor: pointer; color: #fff; touch-action: manipulation; -webkit-user-select: none; user-select: none; }
.btn:active { transform: scale(0.97); }
.btn-accept { background: #1f7a36; }
.btn-reject { background: #a13030; }
.btn-skip { background: #444; }
.btn small { display: block; font-size: 10px; font-weight: 400; opacity: 0.7; margin-top: 2px; }
.stats { font-size: 10px; color: #666; margin-top: auto; padding-top: 8px; border-top: 1px solid #2a2a2a; }
.dragged-indicator { color: #ffc107; font-weight: 600; }

/* Token prompt */
#token-modal { position: fixed; inset: 0; background: rgba(0,0,0,0.85); display: none; align-items: center; justify-content: center; z-index: 100; }
#token-modal.show { display: flex; }
#token-modal .box { background: #222; padding: 24px; border-radius: 12px; max-width: 90%; width: 400px; }
#token-modal h3 { margin-bottom: 12px; }
#token-modal input { width: 100%; padding: 12px; background: #181818; border: 1px solid #444; border-radius: 6px; color: #fff; font-family: ui-monospace, monospace; }
#token-modal button { margin-top: 12px; }

/* === MOBILE === */
@media (max-width: 820px) {
  header { padding: 6px 10px; gap: 8px; }
  header h1 { display: none; }
  .rc-info { font-size: 11px; }
  .banner { font-size: 10px; padding: 3px 8px; }
  main { grid-template-columns: 1fr; grid-template-rows: 1fr 1fr;
         height: calc(100vh - 72px - 84px - 36px); /* header + actions + zoom-bar */ }
  .pane.crop-pane { order: 1; min-height: 0; }
  .pane.wms-pane { order: 2; min-height: 0; }
  .pane.wms-pane h2, .pane.crop-pane h2 { padding: 4px 8px; font-size: 9px; }
  .pane .canvas-wrap { padding: 2px; }
  .pane.side { display: none; }   /* panel info oculto en móvil */
  .zoom-bar { padding: 4px 10px; }

  /* botones fijos abajo, full-width */
  #mobile-actions { position: fixed; bottom: 0; left: 0; right: 0; display: flex; gap: 6px;
                    padding: 8px; background: #161616; border-top: 1px solid #333; z-index: 50; }
  #mobile-actions .btn { font-size: 14px; padding: 14px 8px; flex: 1; }
  #mobile-actions .btn small { display: none; }
  .desktop-actions { display: none; }
}
@media (min-width: 821px) {
  #mobile-actions { display: none; }
}
</style>
</head>
<body>
<header>
  <h1>RC Validator</h1>
  <div class="rc-info">
    <b id="rc">—</b> · <span id="addr">cargando…</span> · <span id="cell">—</span>
  </div>
  <div id="snap-banner" class="banner ok">snap OK</div>
  <div style="font-size:10px;color:#888;white-space:nowrap"><span id="stats">—</span></div>
</header>

<div id="token-modal">
  <div class="box">
    <h3>Token requerido</h3>
    <p style="color:#aaa;font-size:13px;margin-bottom:10px">Pega tu token de acceso para iniciar.</p>
    <input id="token-input" type="text" placeholder="kosE_xc...">
    <button class="btn btn-accept" onclick="saveToken()">Entrar</button>
  </div>
</div>

<div class="zoom-bar">
  <label>Zoom (m/px)</label>
  <input type="range" id="zoom" min="0.05" max="1.0" step="0.01" value="0.5">
  <span class="scale-label" id="zoom-label">0.50 m/px</span>
  <button type="button" onclick="setZoom(0.5)" style="padding:4px 10px;background:#333;border:none;color:#fff;border-radius:4px;cursor:pointer">fit</button>
</div>

<main>
  <div class="pane wms-pane">
    <h2>WMS Catastral</h2>
    <div class="canvas-wrap">
      <div class="canvas-inner" id="wms-inner">
        <img id="wms" alt="wms">
      </div>
    </div>
  </div>
  <div class="pane crop-pane">
    <h2>PGOU + polígono (verde = arrastrable)</h2>
    <div class="canvas-wrap">
      <div class="canvas-inner" id="crop-inner">
        <img id="crop" alt="crop">
        <svg id="overlay" xmlns="http://www.w3.org/2000/svg" preserveAspectRatio="none"></svg>
      </div>
    </div>
  </div>
  <div class="pane side">
    <div class="kv"><label>snap score</label><span id="snap_score">—</span></div>
    <div class="kv"><label>snap dx,dy</label><span id="snap_dxdy">—</span></div>
    <div class="kv"><label>cal aplicada</label><span id="cal_dxdy">—</span></div>
    <div class="kv"><label>reliability</label><span id="reliability">—</span></div>
    <div class="kv"><label>n_labels bucket</label><span id="n_labels">—</span></div>
    <div class="kv"><label>error esperado</label><span id="err_m">—</span></div>
    <div class="kv"><label>arrastre Δ</label><span id="drag_dxdy">0, 0</span></div>
    <div style="height:6px"></div>
    <div class="desktop-actions">
      <button class="btn btn-accept" id="btn-accept-d">✓ Aceptar  <small>A · Enter</small></button>
      <button class="btn btn-reject" id="btn-reject-d" style="margin-top:6px">✗ Error grande  <small>X</small></button>
      <button class="btn btn-skip" id="btn-skip-d" style="margin-top:6px">⤳ Skip  <small>S</small></button>
    </div>
    <div class="stats" id="footer-stats">—</div>
  </div>
</main>

<div id="mobile-actions">
  <button class="btn btn-reject" id="btn-reject-m">✗ Error</button>
  <button class="btn btn-skip" id="btn-skip-m">⤳ Skip</button>
  <button class="btn btn-accept" id="btn-accept-m" style="flex:1.5">✓ Aceptar</button>
</div>

<script>
// ----- token handling -----
// Si viene en ?token=... lo guardamos en localStorage y limpiamos la URL.
// La URL queda sólo con ?rc=... para compartir.
function readQuery() {
  const q = new URLSearchParams(location.search);
  const qstr = q.get('queue') || '';
  const queue = qstr.split(',').map(s => s.trim().toUpperCase()).filter(Boolean);
  return { token: q.get('token'), rc: (q.get('rc') || '').toUpperCase() || null, queue };
}
let __queue = [];
let __queue_total = 0;
(() => {
  const { token, rc } = readQuery();
  if (token) {
    localStorage.setItem('iarq_validator_token', token);
    const url = new URL(location.href);
    url.searchParams.delete('token');
    history.replaceState({}, '', url.toString());
  }
  if (!localStorage.getItem('iarq_validator_token')) {
    document.getElementById('token-modal').classList.add('show');
  }
})();
function saveToken() {
  const t = document.getElementById('token-input').value.trim();
  if (!t) return;
  localStorage.setItem('iarq_validator_token', t);
  document.getElementById('token-modal').classList.remove('show');
  loadInitial();
}
function getToken() { return localStorage.getItem('iarq_validator_token') || ''; }

let current = null;
let drag = { dx: 0, dy: 0 };
let viewMpx = 0.5;

function api(path, opts={}) {
  opts.headers = opts.headers || {};
  const t = getToken();
  if (t) opts.headers['Authorization'] = 'Bearer ' + t;
  return fetch(path, opts);
}
function imgUrl(url) {
  // las imágenes <img> no llevan headers, así que añadimos token como query
  const t = getToken();
  return url + (t ? (url.includes('?') ? '&' : '?') + 'token=' + encodeURIComponent(t) : '');
}

function applyZoom() {
  if (!current) return;
  // tamaños display tal que m/px en pantalla = viewMpx para ambos
  const wmsW = current.wms_size_px * current.wms_m_per_px / viewMpx;
  const cropW = current.crop_size_px * current.crop_m_per_px / viewMpx;
  const wms = document.getElementById('wms');
  const crop = document.getElementById('crop');
  wms.style.width = wmsW + 'px';
  wms.style.height = wmsW + 'px';
  crop.style.width = cropW + 'px';
  crop.style.height = cropW + 'px';
  document.getElementById('zoom-label').textContent = viewMpx.toFixed(2) + ' m/px';
  renderOverlay();
}

function setZoom(v) {
  viewMpx = v;
  document.getElementById('zoom').value = v;
  applyZoom();
}

document.getElementById('zoom').addEventListener('input', e => {
  viewMpx = parseFloat(e.target.value);
  applyZoom();
});

function renderOverlay() {
  if (!current) return;
  const svg = document.getElementById('overlay');
  const cropImg = document.getElementById('crop');
  const W = current.crop_size_px;
  svg.setAttribute('viewBox', '0 0 ' + W + ' ' + W);
  svg.style.width = cropImg.style.width;
  svg.style.height = cropImg.style.height;

  const poly = current.poly_snap.map(p => p.join(',')).join(' ');
  svg.innerHTML = `
    <polygon points="${poly}" fill="rgba(220,40,40,0.18)" stroke="#dc2828" stroke-width="3" />
    <polygon class="draggable" id="poly-green" points="${poly}" fill="rgba(40,200,80,0.22)" stroke="#22c55e" stroke-width="3"
             transform="translate(${drag.dx} ${drag.dy})" />
  `;
  attachPolygonDrag();
}

window.addEventListener('resize', applyZoom);

// ----- Drag del polígono verde (un dedo / mouse sobre el verde) -----
let dragging = false; let dragStart = null; let pointerId = null;
function attachPolygonDrag() {
  const green = document.getElementById('poly-green');
  if (!green) return;
  green.addEventListener('pointerdown', e => {
    if (activePointers.size >= 2) return; // no drag mientras hay pinch
    dragging = true; pointerId = e.pointerId;
    try { green.setPointerCapture(e.pointerId); } catch {}
    const s = pxScale();
    dragStart = { x: e.clientX - drag.dx * s, y: e.clientY - drag.dy * s };
    e.preventDefault();
  });
  green.addEventListener('pointermove', e => {
    if (!dragging || e.pointerId !== pointerId) return;
    if (activePointers.size >= 2) { dragging = false; pointerId = null; return; }
    const s = pxScale();
    drag.dx = Math.round((e.clientX - dragStart.x) / s);
    drag.dy = Math.round((e.clientY - dragStart.y) / s);
    const el = document.getElementById('drag_dxdy');
    if (el) {
      el.textContent = drag.dx + ', ' + drag.dy;
      el.className = (drag.dx || drag.dy) ? 'dragged-indicator' : '';
    }
    renderOverlay();
    e.preventDefault();
  });
  const endDrag = e => {
    if (e.pointerId !== pointerId) return;
    dragging = false; pointerId = null;
    try { green.releasePointerCapture(e.pointerId); } catch {}
  };
  green.addEventListener('pointerup', endDrag);
  green.addEventListener('pointercancel', endDrag);
}

// ----- Pinch-to-zoom (dos dedos en cualquier parte) -----
const activePointers = new Map();
let pinchStartDist = 0, pinchStartZoom = 0;
function pinchPoints() { return [...activePointers.values()]; }
function pinchDist() {
  const p = pinchPoints();
  if (p.length < 2) return 0;
  return Math.hypot(p[0].x - p[1].x, p[0].y - p[1].y);
}
document.addEventListener('pointerdown', e => {
  if (e.pointerType !== 'touch') return;
  activePointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
  if (activePointers.size === 2) {
    pinchStartDist = pinchDist();
    pinchStartZoom = viewMpx;
    // cancela drag si estaba activo (segundo dedo aborta drag)
    if (dragging) { dragging = false; pointerId = null; }
  }
}, { passive: true });
document.addEventListener('pointermove', e => {
  if (e.pointerType !== 'touch') return;
  if (!activePointers.has(e.pointerId)) return;
  activePointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
  if (activePointers.size >= 2 && pinchStartDist > 0) {
    const d = pinchDist();
    if (d > 0) {
      // separar dedos → d aumenta → ratio<1 → zoom in (menor m/px)
      const ratio = pinchStartDist / d;
      const newZoom = Math.max(0.05, Math.min(1.0, pinchStartZoom * ratio));
      setZoom(newZoom);
    }
    e.preventDefault();
  }
}, { passive: false });
function pinchUp(e) {
  if (e.pointerType !== 'touch') return;
  activePointers.delete(e.pointerId);
  if (activePointers.size < 2) pinchStartDist = 0;
}
document.addEventListener('pointerup', pinchUp, { passive: true });
document.addEventListener('pointercancel', pinchUp, { passive: true });

function pxScale() {
  // px de pantalla por px nativo del crop
  return current ? (current.crop_m_per_px / viewMpx) : 1;
}

async function loadRC(rc) {
  drag.dx = 0; drag.dy = 0;
  const dragEl = document.getElementById('drag_dxdy');
  if (dragEl) { dragEl.textContent = '0, 0'; dragEl.className = ''; }
  document.getElementById('rc').textContent = 'cargando…';
  const r = await api('/api/next' + (rc ? '?rc=' + encodeURIComponent(rc) : ''));
  if (r.status === 401) {
    localStorage.removeItem('iarq_validator_token');
    document.getElementById('token-modal').classList.add('show');
    return;
  }
  if (!r.ok) { alert('error ' + r.status); return; }
  const data = await r.json();
  current = data;
  // actualizar URL con ?rc= (sin recargar)
  const url = new URL(location.href);
  url.searchParams.set('rc', data.rc);
  history.replaceState({}, '', url.toString());

  document.getElementById('rc').textContent = data.rc;
  document.getElementById('addr').textContent = data.address || '—';
  document.getElementById('cell').textContent = data.cell + '-' + data.sub_quadrant;
  // info panel (sólo visible en desktop)
  const setText = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
  setText('snap_score', data.snap_score.toFixed(3));
  setText('snap_dxdy', data.snap_dxdy.join(', '));
  setText('cal_dxdy', data.cal_dxdy.join(', '));
  const q = data.calibration_quality || {};
  setText('reliability', q.reliability || '—');
  setText('n_labels', q.n_labels || '—');
  setText('err_m', q.expected_residual_m ? q.expected_residual_m.toFixed(2) + ' m' : '—');

  const banner = document.getElementById('snap-banner');
  if (data.snap_confident) {
    banner.className = 'banner ok'; banner.textContent = 'snap ' + data.snap_score.toFixed(2);
  } else {
    banner.className = 'banner warn'; banner.textContent = '⚠ INCIERTO ' + data.snap_score.toFixed(2);
  }
  document.getElementById('crop').src = imgUrl(data.crop_url);
  document.getElementById('wms').src = imgUrl(data.wms_url);
  document.getElementById('crop').onload = applyZoom;
  document.getElementById('wms').onload = applyZoom;
  loadStats();
  prefetchNext();
}

// Prefetch del próximo RC de la cola: trigger del backend + warm-up imágenes.
// Sin await — corre en background para no bloquear la UI actual.
function prefetchNext() {
  if (!__queue.length) return;
  const next = __queue[0];  // peek sin shift
  if (window.__prefetched === next) return;
  window.__prefetched = next;
  api('/api/next?rc=' + encodeURIComponent(next))
    .then(r => r.ok ? r.json() : null)
    .then(d => {
      if (!d) return;
      // El navegador cachea las imágenes con esta carga "fantasma"
      const i1 = new Image(); i1.src = imgUrl(d.crop_url);
      const i2 = new Image(); i2.src = imgUrl(d.wms_url);
    })
    .catch(() => { window.__prefetched = null; });
}

function loadInitial() {
  const { rc, queue } = readQuery();
  if (queue && queue.length) {
    __queue = queue.slice();
    __queue_total = __queue.length;
    const first = __queue.shift();
    loadRC(first);
    return;
  }
  loadRC(rc);
}
function queueIndicator() {
  if (!__queue_total) return '';
  const done = __queue_total - __queue.length;
  return ` · cola ${done}/${__queue_total}`;
}

async function loadStats() {
  const r = await api('/api/stats');
  if (!r.ok) return;
  const d = await r.json();
  const next = d.recal_threshold - (d.accept_counter || 0);
  document.getElementById('stats').textContent =
    d.total + ' · ' + d.pending + ' pend. · recal en ' + next + queueIndicator();
  const f = document.getElementById('footer-stats');
  if (f) f.textContent = JSON.stringify(d.by_action || {});
}

async function submit(action) {
  if (!current) return;
  const payload = {
    rc: current.rc,
    action: action,
    dxdy: action === 'accept' ? [drag.dx, drag.dy] : [0, 0],
    snap_score: current.snap_score,
    snap_dxdy: current.snap_dxdy,
    cal_dxdy: current.cal_dxdy,
  };
  const r = await api('/api/label', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  if (!r.ok) { alert('error guardando'); return; }
  const resp = await r.json();
  if (resp.recalibrated) {
    // El servicio se reinicia ~5-15s. Mostrar mensaje y esperar.
    document.getElementById('rc').textContent = '♻ recalibrando…';
    document.getElementById('addr').textContent = 'reiniciando servicio, ~10s';
    setTimeout(() => loadRC(__queue.length ? __queue.shift() : null), 12000);
  } else {
    // Si hay cola, siguiente de la cola; si no, random.
    if (__queue.length) {
      loadRC(__queue.shift());
    } else {
      if (__queue_total > 0) {
        // Acabada la cola — mostrar aviso y volver a random
        const el = document.getElementById('rc');
        if (el) el.textContent = '✓ cola completada (' + __queue_total + ')';
        __queue_total = 0;
      }
      loadRC(null);
    }
  }
}

['btn-accept-d', 'btn-accept-m'].forEach(id => { const el = document.getElementById(id); if (el) el.onclick = () => submit('accept'); });
['btn-reject-d', 'btn-reject-m'].forEach(id => { const el = document.getElementById(id); if (el) el.onclick = () => submit('reject_unfixable'); });
['btn-skip-d', 'btn-skip-m'].forEach(id => { const el = document.getElementById(id); if (el) el.onclick = () => submit('skip'); });

window.addEventListener('keydown', e => {
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
  if (e.key === 'a' || e.key === 'A' || e.key === 'Enter') submit('accept');
  else if (e.key === 'x' || e.key === 'X') submit('reject_unfixable');
  else if (e.key === 's' || e.key === 'S') submit('skip');
});

if (getToken()) loadInitial();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
