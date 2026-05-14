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
import hmac
import io
import json
import os
import random
import sys
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_LABELS_LOCK = threading.Lock()

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
FICHA_LABELS_PATH = ROOT / "data" / "validator_labels_fichas.json"
NATIVE_CROP = 3600   # crop nativo en px PGOU (cubre ~317 m a 0.088 m/px)
DISPLAY_CROP = 1800  # tras downscale 2× → tamaño PNG transferido

# auto-recalibración cada N aceptaciones (drag != 0 o exact)
RECAL_THRESHOLD = 30
_RECAL_COUNTER_FILE = ROOT / "data" / ".recal_counter"
_RECAL_PENDING_FILE = ROOT / "data" / ".recal_pending"
# Cap for the in-memory _CACHE of generated RC bundles. Each entry holds a few MB
# of PNG bytes (crop + WMS), so without a cap the process keeps growing forever.
_CACHE_MAX = 200

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
    if not hmac.compare_digest(candidate or "", TOKEN):
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


def load_ficha_labels() -> list:
    if not FICHA_LABELS_PATH.exists(): return []
    try: return json.loads(FICHA_LABELS_PATH.read_text(encoding="utf-8"))
    except Exception: return []


def save_ficha_labels(labels):
    tmp = FICHA_LABELS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(labels, indent=2, ensure_ascii=False))
    tmp.replace(FICHA_LABELS_PATH)


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


_AMBITO_RCS: Optional[set] = None
_AMBITO_RCS_MTIME: float = 0.0


def _rcs_in_ambito() -> set:
    """RC14 dentro de polígono real de un ámbito con ficha PDF asociada.

    Lee la lista precomputada desde
    `~/.cache/oviedo_rc/priority_rcs_fichas.json`, generada por
    `scripts/build_priority_rcs.py` (point-in-polygon contra polígonos WFS).

    Reload mtime-aware: si re-ejecutas build_priority_rcs.py, el siguiente
    `_rcs_in_ambito()` detecta el mtime nuevo y recarga (sin reiniciar el
    servicio).
    """
    global _AMBITO_RCS, _AMBITO_RCS_MTIME
    cache_file = Path.home() / ".cache" / "oviedo_rc" / "priority_rcs_fichas.json"
    if not cache_file.exists():
        _AMBITO_RCS = set()
        _AMBITO_RCS_MTIME = 0.0
        return _AMBITO_RCS
    try:
        mtime = cache_file.stat().st_mtime
    except OSError:
        return _AMBITO_RCS or set()
    if _AMBITO_RCS is not None and mtime == _AMBITO_RCS_MTIME:
        return _AMBITO_RCS
    try:
        _AMBITO_RCS = set(json.loads(cache_file.read_text(encoding="utf-8")))
        _AMBITO_RCS_MTIME = mtime
    except Exception:
        if _AMBITO_RCS is None:
            _AMBITO_RCS = set()
    return _AMBITO_RCS


def _random_rc() -> str:
    """RC aleatorio del cache, urbano válido + cell-sub con hoja PGOU.

    Prioriza RCs dentro de algún ámbito PGOU (Phase 2c: cal por ficha),
    cae a RCs fuera de ámbito cuando los priorizados están agotados.
    """
    global _COVERED_CSUB
    if _COVERED_CSUB is None:
        _COVERED_CSUB = _covered_csub()

    from oviedo_rc.geom import validate_rc
    from oviedo_rc.errors import RCError
    ambito_set = _rcs_in_ambito()
    all_keys = list(COORDS.keys())
    priority = [k for k in all_keys if k in ambito_set]
    rest     = [k for k in all_keys if k not in ambito_set]
    random.shuffle(priority)
    random.shuffle(rest)
    keys = priority + rest
    done = labeled_rcs()
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

    # Polígono catastral proyectado en pixels del WMS (600x600 cubre 300m
    # centrado en (X,Y), bbox=(X-150,Y-150,X+150,Y+150), 0.5 m/px).
    # Ayuda visual: marca el edificio buscado también en la referencia, no
    # sólo en el plano PGOU central, para distinguir entre parcelas parecidas.
    wms_bbox_xmin = X - 150
    wms_bbox_ymax = Y + 150
    poly_wms = [
        [int((ux - wms_bbox_xmin) * 2), int((wms_bbox_ymax - uy) * 2)]
        for ux, uy in poly["polygon_utm"]
    ]


    # m/px del PGOU nativo
    body_w_px = body_rect[2] - body_rect[0]
    pgou_native_m_per_px = float(BODY_W_M) / float(body_w_px) if body_w_px else 0.088
    # m/px del crop transferido (downscale 2×)
    crop_m_per_px = pgou_native_m_per_px / scale  # ≈ 0.17

    # ---- Plano de ficha de ámbito (cuarto panel cuando aplica) ----
    ficha_png = b""
    ficha_size_px = 0
    ficha_size_px_h = 0
    poly_ficha = []
    ficha_etiqueta = None
    ficha_filename = None
    ficha_m_per_px = 0.127
    ficha_scale = 1000
    try:
        from oviedo_rc import ficha_plano as fp_mod
        from oviedo_rc import planeamiento as plan_mod_local
        plan_info = plan_mod_local.lookup(X, Y)
        matches = plan_info.get("fichas_match") or []
        if matches:
            top_filename = matches[0].get("filename", "")
            # PNG limpio (sin polígono dibujado) — el cliente pinta SVG drag-able encima.
            ficha_render = fp_mod.render_with_overlay(
                top_filename, poly["polygon_utm"], draw_polygon=False
            )
            if ficha_render:
                ficha_png = ficha_render["png_bytes"]
                ficha_size_px_w = ficha_render["width"]
                ficha_size_px_h = ficha_render["height"]
                ficha_size_px = ficha_size_px_w   # ancho como referencia del viewport
                ficha_etiqueta = ficha_render["ambito_etiqueta"]
                ficha_filename = top_filename
                ficha_m_per_px = ficha_render.get("m_per_px", 0.127)
                ficha_scale = ficha_render.get("scale", 1000)
                poly_ficha = [[int(p[0]), int(p[1])] for p in ficha_render["poly_px"]]
    except Exception:
        pass

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
        "poly_wms": poly_wms,
        "crop_png": crop_png,
        "wms_png": wms_png,
        "ficha_png": ficha_png,
        "ficha_size_px": ficha_size_px,
        "ficha_size_px_h": ficha_size_px_h,
        "poly_ficha": poly_ficha,
        "ficha_etiqueta": ficha_etiqueta,
        "ficha_filename": ficha_filename,
        "ficha_m_per_px": ficha_m_per_px,
        "ficha_scale": ficha_scale,
    }


# in-memory cache de las últimas generaciones (LRU). FIFO eviction cuando > _CACHE_MAX.
_CACHE: "OrderedDict[str, dict]" = OrderedDict()


@app.get("/health")
def health():
    return {"ok": True, "service": "validator", "labels_count": len(load_labels())}


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    # 1×1 PNG transparente (silencia el 404 del navegador)
    return Response(
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06"
        b"\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01"
        b"\x01\x00\x05\x00\x01\x0d\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82",
        media_type="image/png",
    )


@app.get("/", response_class=HTMLResponse)
def home():
    """HTML público — el token sólo se requiere para /api/*. La UI gestiona el token via localStorage."""
    return INDEX_HTML


@app.get("/api/next")
def next_rc(rc: Optional[str] = None, _=Depends(auth)):
    """Devuelve datos del siguiente RC (random o el indicado)."""
    if rc:
        rc = rc.strip().upper()
    if not rc:
        rc = _random_rc()
    data = _generate_for_rc(rc)
    # FIFO-evict the oldest entries before adding a new one (cap at _CACHE_MAX).
    _CACHE[rc] = data
    _CACHE.move_to_end(rc)
    while len(_CACHE) > _CACHE_MAX:
        _CACHE.popitem(last=False)
    return {
        "rc": data["rc"],
        "address": data["address"],
        "sheet": data["sheet"],
        "cell": data["cell"],
        "sub_quadrant": data["sub_quadrant"],
        "poly_snap": data["poly_snap"],
        "poly_wms": data.get("poly_wms", []),
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
        "ficha_url": (f"/api/img/{data['rc']}/ficha" if data.get("ficha_png") else None),
        "ficha_size_px": data.get("ficha_size_px", 0),
        "ficha_size_px_h": data.get("ficha_size_px_h", 0),
        "ficha_m_per_px": data.get("ficha_m_per_px", 0.127),
        "ficha_scale": data.get("ficha_scale", 1000),
        "poly_ficha": data.get("poly_ficha", []),
        "ficha_etiqueta": data.get("ficha_etiqueta"),
        "ficha_filename": data.get("ficha_filename"),
    }


@app.get("/api/img/{rc}/{kind}")
def img(rc: str, kind: str, _=Depends(auth)):
    data = _CACHE.get(rc)
    if not data: raise HTTPException(404, "cache miss — call /api/next first")
    if kind == "crop":
        return Response(data["crop_png"], media_type="image/png")
    if kind == "wms":
        return Response(data["wms_png"], media_type="image/png")
    if kind == "ficha":
        png = data.get("ficha_png") or b""
        if not png:
            raise HTTPException(404, "no ficha plano for this RC")
        return Response(png, media_type="image/png")
    raise HTTPException(404)


class LabelReq(BaseModel):
    rc: str
    action: str = Field(..., pattern="^(accept|reject_unfixable|skip)$")
    dxdy: list[int] = Field(default_factory=lambda: [0, 0])
    snap_score: float = 0.0
    snap_dxdy: list[int] = Field(default_factory=lambda: [0, 0])
    cal_dxdy: list[int] = Field(default_factory=lambda: [0, 0])
    comment: str = ""
    # Drag específico del panel "Ficha de ámbito" (Phase 2c).
    # Sólo se guarda si nonzero o si se confirma alineación (drag=0 con accept).
    ficha_dxdy: list[int] = Field(default_factory=lambda: [0, 0])
    ficha_etiqueta: Optional[str] = None
    ficha_filename: Optional[str] = None
    ficha_cal_dxdy: list[int] = Field(default_factory=lambda: [0, 0])


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
    """Deferred recalibration: write a sentinel file that an out-of-band watcher
    picks up. Does NOT spawn `systemctl restart` on ourselves — that killed the
    process and RST'd in-flight requests. The watcher runs `recalibrate.py`,
    rewrites `data/calibration_offsets.json`, and removes the sentinel; the
    locator/validator pick up the new file on next request via mtime-based
    lazy reload (`oviedo_rc.calibration._load`).
    """
    try:
        _RECAL_PENDING_FILE.write_text(datetime.now(timezone.utc).isoformat())
    except OSError:
        pass


@app.post("/api/label")
def label(req: LabelReq, _=Depends(auth)):
    with _LABELS_LOCK:
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
        counter = _accept_counter()
        total = len(labels)

    # Ficha-plano label (cal por ámbito). Sólo guardamos en accept y si
    # hay metadata de ficha. Drag puede ser 0 (confirmación de alineación
    # ya correcta — sigue siendo señal útil).
    ficha_total = 0
    if req.action == "accept" and req.ficha_etiqueta and req.ficha_filename:
        with _LABELS_LOCK:
            f_labels = load_ficha_labels()
            f_labels = [l for l in f_labels if not (
                l.get("rc") == req.rc and l.get("etiqueta") == req.ficha_etiqueta
            )]
            f_labels.append({
                "rc": req.rc,
                "etiqueta": req.ficha_etiqueta,
                "filename": req.ficha_filename,
                "dxdy": req.ficha_dxdy,
                "cal_dxdy": req.ficha_cal_dxdy,
                "ts": time.time(),
            })
            save_ficha_labels(f_labels)
            ficha_total = len(f_labels)
    return {"ok": True, "total": total, "ficha_total": ficha_total,
            "accept_counter": counter,
            "recal_threshold": RECAL_THRESHOLD,
            "recalibrated": recalibrated}


@app.get("/api/stats")
def stats(_=Depends(auth)):
    with _LABELS_LOCK:
        labels = load_labels()
        counter = _accept_counter()
    by_action = {}
    for l in labels:
        by_action[l["action"]] = by_action.get(l["action"], 0) + 1
    return {"total": len(labels), "by_action": by_action,
            "rcs_in_cache": len(COORDS), "pending": len(COORDS) - len(labels),
            "accept_counter": counter,
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
main.has-ficha { grid-template-columns: 1fr 1fr 1fr 280px; }
.pane.ficha-pane { display: none; }
main.has-ficha .pane.ficha-pane { display: flex; }
.pane.ficha-pane .meta { font-size: 10px; color: #888; padding: 4px 8px; background: #161616; }
.zoom-bar { display: flex; align-items: center; gap: 10px; padding: 6px 12px; background: #1d1d1d; border-bottom: 1px solid #333; }
.zoom-bar label { font-size: 10px; color: #888; text-transform: uppercase; letter-spacing: 1px; }
.zoom-bar input[type=range] { flex: 1; accent-color: #1f7a36; height: 24px; }
.zoom-bar .scale-label { font-size: 11px; color: #fff; font-family: ui-monospace, monospace; min-width: 80px; text-align: right; }
.zoom-bar button { padding: 6px 12px; background: #333; border: none; color: #fff; border-radius: 4px; cursor: pointer; font-size: 12px; }
.pane { background: #181818; display: flex; flex-direction: column; overflow: hidden; }
.pane h2 { font-size: 10px; text-transform: uppercase; letter-spacing: 1px; color: #888; padding: 6px 10px; background: #1f1f1f; flex-shrink: 0; }
.canvas-wrap { flex: 1; position: relative; overflow: hidden; background: #0c0c0c;
               touch-action: none; overscroll-behavior: none;
               -webkit-user-select: none; user-select: none; -webkit-touch-callout: none; }
.canvas-inner { position: absolute; top: 0; left: 0; transform-origin: 0 0; will-change: transform;
                -webkit-user-select: none; user-select: none; -webkit-touch-callout: none; }
.canvas-inner img { display: block; image-rendering: crisp-edges; user-select: none; -webkit-user-select: none; -webkit-user-drag: none; -webkit-touch-callout: none; pointer-events: none; }
#overlay, #overlay-wms, #overlay-ficha { position: absolute; top: 0; left: 0; pointer-events: none; -webkit-user-select: none; user-select: none; -webkit-touch-callout: none; }
#overlay polygon.draggable, #overlay-ficha polygon.draggable { pointer-events: auto; cursor: grab; touch-action: none; -webkit-user-select: none; user-select: none; -webkit-touch-callout: none; }
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
  .pane.ficha-pane { order: 3; min-height: 0; }
  main.has-ficha { grid-template-rows: 1fr 1fr 1fr; }
  .pane.wms-pane h2, .pane.crop-pane h2, .pane.ficha-pane h2 { padding: 4px 8px; font-size: 9px; }
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
<div id="busy-overlay" style="display:none; position:fixed; inset:0; z-index:9999;
     background:rgba(12,12,12,0.55); backdrop-filter:blur(2px);
     align-items:center; justify-content:center; flex-direction:column; gap:14px;
     color:#fff; font-size:14px; pointer-events:all;">
  <div style="width:44px; height:44px; border:4px solid #444; border-top-color:#22c55e;
       border-radius:50%; animation:spin 0.9s linear infinite"></div>
  <div>Guardando…</div>
</div>
<style>@keyframes spin { to { transform: rotate(360deg); } }</style>
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
  <input type="range" id="zoom" min="0.1" max="2.0" step="0.01" value="0.75">
  <span class="scale-label" id="zoom-label">0.50 m/px</span>
  <button type="button" onclick="doubleTapFit()" style="padding:4px 10px;background:#333;border:none;color:#fff;border-radius:4px;cursor:pointer">fit</button>
</div>

<main>
  <div class="pane wms-pane">
    <h2>WMS Catastral</h2>
    <div class="canvas-wrap">
      <div class="canvas-inner" id="wms-inner">
        <img id="wms" alt="wms">
        <svg id="overlay-wms" xmlns="http://www.w3.org/2000/svg" preserveAspectRatio="none"
             style="position:absolute;top:0;left:0;pointer-events:none"></svg>
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
  <div class="pane ficha-pane">
    <h2>Ficha de ámbito · <span id="ficha-etiqueta">—</span> (verde = arrastrable)</h2>
    <div class="canvas-wrap">
      <div class="canvas-inner" id="ficha-inner">
        <img id="ficha" alt="ficha-plano">
        <svg id="overlay-ficha" xmlns="http://www.w3.org/2000/svg" preserveAspectRatio="none"
             style="position:absolute;top:0;left:0;pointer-events:none"></svg>
      </div>
    </div>
    <div class="meta">
      Drag = corregir desalineación de este ámbito.
      Aceptar SIN drag = confirmar que el polígono ya está alineado (señal útil).
      Si NO estás seguro de si está bien, mejor <b>Skip</b> (no contamina la mediana).
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
let viewMpx = 0.75;
// dragVec in CROP-NATIVE pixels (polygon user-correction).
let dragVec = { dx: 0, dy: 0 };
// dragVec separado para el panel ficha (en ficha-native px).
let dragVecFicha = { dx: 0, dy: 0 };
// Per-pane viewport state. scale = mPerPxNative / viewMpx.
const VP = {
  crop:  { panX: 0, panY: 0, scale: 1, inner: null, wrap: null, img: null, mPerPxNative: 0, nativeSize: 0 },
  wms:   { panX: 0, panY: 0, scale: 1, inner: null, wrap: null, img: null, mPerPxNative: 0, nativeSize: 0 },
  ficha: { panX: 0, panY: 0, scale: 1, inner: null, wrap: null, img: null, mPerPxNative: 0, nativeSize: 0, nativeSizeH: 0 },
};

function initVP() {
  VP.crop.wrap  = document.querySelector('.crop-pane .canvas-wrap');
  VP.crop.inner = document.getElementById('crop-inner');
  VP.crop.img   = document.getElementById('crop');
  VP.wms.wrap   = document.querySelector('.wms-pane .canvas-wrap');
  VP.wms.inner  = document.getElementById('wms-inner');
  VP.wms.img    = document.getElementById('wms');
  VP.ficha.wrap  = document.querySelector('.ficha-pane .canvas-wrap');
  VP.ficha.inner = document.getElementById('ficha-inner');
  VP.ficha.img   = document.getElementById('ficha');
}

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

// ----- Transform-based viewport -----
let rafPending = false;
function scheduleTransform() {
  if (rafPending) return;
  rafPending = true;
  requestAnimationFrame(() => {
    rafPending = false;
    applyTransform();
  });
}
function applyTransform() {
  for (const pane of [VP.crop, VP.wms, VP.ficha]) {
    if (!pane.inner || !pane.mPerPxNative) continue;
    pane.scale = pane.mPerPxNative / viewMpx;
    pane.inner.style.transform = `translate(${pane.panX}px, ${pane.panY}px) scale(${pane.scale})`;
  }
  const green = document.getElementById('poly-green');
  if (green) green.setAttribute('transform', `translate(${dragVec.dx} ${dragVec.dy})`);
  const greenF = document.getElementById('poly-green-ficha');
  if (greenF) greenF.setAttribute('transform', `translate(${dragVecFicha.dx} ${dragVecFicha.dy})`);
  const zl = document.getElementById('zoom-label');
  if (zl) zl.textContent = viewMpx.toFixed(2) + ' m/px';
  const dEl = document.getElementById('drag_dxdy');
  if (dEl) {
    dEl.textContent = dragVec.dx + ', ' + dragVec.dy;
    dEl.className = (dragVec.dx || dragVec.dy) ? 'dragged-indicator' : '';
  }
}

function paneScale(pane) {
  return pane.mPerPxNative ? (pane.mPerPxNative / viewMpx) : 1;
}

function centerPaneOnPoint(pane, ptNativeX, ptNativeY) {
  if (!pane.wrap) return;
  const wRect = pane.wrap.getBoundingClientRect();
  const s = paneScale(pane);
  pane.panX = wRect.width / 2 - ptNativeX * s;
  pane.panY = wRect.height / 2 - ptNativeY * s;
}

function polyCentroidNative() {
  if (!current) return { x: 0, y: 0 };
  const pts = current.poly_snap;
  let cx = 0, cy = 0;
  for (const [x, y] of pts) { cx += x; cy += y; }
  return { x: cx / pts.length, y: cy / pts.length };
}

function polyBBoxNative() {
  if (!current) return null;
  let minX=Infinity,maxX=-Infinity,minY=Infinity,maxY=-Infinity;
  for (const [x, y] of current.poly_snap) {
    if (x<minX) minX=x; if (x>maxX) maxX=x;
    if (y<minY) minY=y; if (y>maxY) maxY=y;
  }
  return { minX, maxX, minY, maxY };
}

function renderOverlay() {
  if (!current) return;
  const svg = document.getElementById('overlay');
  const W = current.crop_size_px;
  // SVG overlay sits inside crop-inner; size = native crop. It scales with crop-inner's transform.
  svg.setAttribute('viewBox', '0 0 ' + W + ' ' + W);
  svg.style.width = W + 'px';
  svg.style.height = W + 'px';

  const poly = current.poly_snap.map(p => p.join(',')).join(' ');
  // Build once; we won't rebuild per-frame (drag updates transform attribute only).
  // stroke-width stays in native px so on-screen thickness scales with zoom; that's
  // acceptable (line gets thinner when zoomed in) and avoids extra work per frame.
  svg.innerHTML = `
    <polygon points="${poly}" fill="rgba(220,40,40,0.18)" stroke="#dc2828" stroke-width="3" />
    <polygon class="draggable" id="poly-green" points="${poly}" fill="rgba(40,200,80,0.22)" stroke="#22c55e" stroke-width="3"
             transform="translate(${dragVec.dx} ${dragVec.dy})" />
  `;

  // WMS overlay: dibuja el polígono catastral sobre la referencia para
  // identificar el edificio buscado entre parcelas similares.
  const svgWms = document.getElementById('overlay-wms');
  if (svgWms && current.poly_wms && current.poly_wms.length) {
    const Ww = current.wms_size_px;
    svgWms.setAttribute('viewBox', '0 0 ' + Ww + ' ' + Ww);
    svgWms.style.width = Ww + 'px';
    svgWms.style.height = Ww + 'px';
    const polyWmsStr = current.poly_wms.map(p => p.join(',')).join(' ');
    svgWms.innerHTML = `<polygon points="${polyWmsStr}" fill="rgba(220,40,40,0.22)" stroke="#dc2828" stroke-width="2.5" />`;
  } else if (svgWms) {
    svgWms.innerHTML = '';
  }

  // Ficha overlay (Phase 2c): rojo estático + verde drag-able sobre el plano de la ficha.
  const svgF = document.getElementById('overlay-ficha');
  if (svgF && current.poly_ficha && current.poly_ficha.length) {
    const Wf = current.ficha_size_px;
    const Hf = current.ficha_size_px_h || Wf;
    svgF.setAttribute('viewBox', '0 0 ' + Wf + ' ' + Hf);
    svgF.style.width = Wf + 'px';
    svgF.style.height = Hf + 'px';
    const polyFStr = current.poly_ficha.map(p => p.join(',')).join(' ');
    svgF.innerHTML = `
      <polygon points="${polyFStr}" fill="rgba(220,40,40,0.18)" stroke="#dc2828" stroke-width="3" />
      <polygon class="draggable" id="poly-green-ficha" points="${polyFStr}" fill="rgba(40,200,80,0.22)" stroke="#22c55e" stroke-width="3"
               transform="translate(${dragVecFicha.dx} ${dragVecFicha.dy})" />
    `;
  } else if (svgF) {
    svgF.innerHTML = '';
  }
}

// ----- Initial zoom & centering on RC load -----
function computeInitialZoom() {
  if (!current) return 0.75;
  const bb = polyBBoxNative();
  if (!bb) return 0.75;
  const spanXm = (bb.maxX - bb.minX) * current.crop_m_per_px;
  const spanYm = (bb.maxY - bb.minY) * current.crop_m_per_px;
  const wRect = VP.crop.wrap.getBoundingClientRect();
  const wPx = wRect.width || 1, hPx = wRect.height || 1;
  // viewMpx so polygon fits with ~30% margin (1.3 multiplier).
  const needX = (spanXm * 1.3) / wPx;
  const needY = (spanYm * 1.3) / hPx;
  const need = Math.max(needX, needY, 0.1);
  return Math.max(need, 0.5);  // never zoom in too much initially
}

function polyCentroidFichaNative() {
  if (!current || !current.poly_ficha || !current.poly_ficha.length) return null;
  let cx = 0, cy = 0;
  for (const [x, y] of current.poly_ficha) { cx += x; cy += y; }
  return { x: cx / current.poly_ficha.length, y: cy / current.poly_ficha.length };
}

function recenterFicha() {
  if (!VP.ficha.nativeSize || !VP.ficha.wrap) return;
  const fc = polyCentroidFichaNative();
  if (!fc) return;
  centerPaneOnPoint(VP.ficha, fc.x + dragVecFicha.dx, fc.y + dragVecFicha.dy);
}

function recenterAfterLoad() {
  if (!current) return;
  viewMpx = computeInitialZoom();
  const zr = document.getElementById('zoom');
  if (zr) zr.value = viewMpx;
  const c = polyCentroidNative();
  centerPaneOnPoint(VP.crop, c.x + dragVec.dx, c.y + dragVec.dy);
  if (VP.wms.nativeSize) {
    centerPaneOnPoint(VP.wms, VP.wms.nativeSize / 2, VP.wms.nativeSize / 2);
  }
  recenterFicha();
  scheduleTransform();
}

function setZoom(v) {
  viewMpx = v;
  const zr = document.getElementById('zoom');
  if (zr) zr.value = v;
  scheduleTransform();
}

document.getElementById('zoom').addEventListener('input', e => {
  viewMpx = parseFloat(e.target.value);
  const c = polyCentroidNative();
  centerPaneOnPoint(VP.crop, c.x + dragVec.dx, c.y + dragVec.dy);
  if (VP.wms.nativeSize) {
    centerPaneOnPoint(VP.wms, VP.wms.nativeSize / 2, VP.wms.nativeSize / 2);
  }
  recenterFicha();
  scheduleTransform();
});

window.addEventListener('resize', () => {
  // keep polygon centred when viewport size changes
  if (!current) return;
  const c = polyCentroidNative();
  centerPaneOnPoint(VP.crop, c.x + dragVec.dx, c.y + dragVec.dy);
  if (VP.wms.nativeSize) {
    centerPaneOnPoint(VP.wms, VP.wms.nativeSize / 2, VP.wms.nativeSize / 2);
  }
  recenterFicha();
  scheduleTransform();
});

// ----- Unified pointer state machine -----
// kind ∈ {'drag-poly', 'pan-pane'}
const pointers = new Map();
let pinch = null;   // { startDist, startZoom, midPane, worldX, worldY, midX, midY }
let lastTap = { t: 0, x: 0, y: 0 };

function paneFromPoint(clientX, clientY) {
  for (const key of ['crop', 'wms', 'ficha']) {
    const pane = VP[key];
    if (!pane.wrap) continue;
    const r = pane.wrap.getBoundingClientRect();
    if (clientX >= r.left && clientX <= r.right && clientY >= r.top && clientY <= r.bottom) {
      return pane;
    }
  }
  return null;
}

function startPinch() {
  const pts = [...pointers.values()];
  if (pts.length < 2) return;
  const midX = (pts[0].x + pts[1].x) / 2;
  const midY = (pts[0].y + pts[1].y) / 2;
  const dist = Math.hypot(pts[0].x - pts[1].x, pts[0].y - pts[1].y);
  const pane = paneFromPoint(midX, midY) || VP.crop;
  const r = pane.wrap.getBoundingClientRect();
  const s = paneScale(pane);
  // world coords (native px) of the midpoint in that pane
  const worldX = (midX - r.left - pane.panX) / s;
  const worldY = (midY - r.top  - pane.panY) / s;
  pinch = {
    startDist: dist || 1,
    startZoom: viewMpx,
    midPane: pane,
    worldX, worldY,
    midX, midY,
    // snapshot panX/Y de ambos panes para sync solidario
    startCrop: { panX: VP.crop.panX, panY: VP.crop.panY },
    startWms:  { panX: VP.wms.panX,  panY: VP.wms.panY  },
  };
  for (const p of pointers.values()) {
    if (p.kind === 'drag-poly' || p.kind === 'pan-pane') p.kind = 'pinch';
  }
}

function updatePinch() {
  const pts = [...pointers.values()];
  if (pts.length < 2 || !pinch) return;
  const dx = pts[0].x - pts[1].x, dy = pts[0].y - pts[1].y;
  const dist = Math.hypot(dx, dy) || 1;
  const midX = (pts[0].x + pts[1].x) / 2;
  const midY = (pts[0].y + pts[1].y) / 2;
  const ratio = dist / pinch.startDist;
  // bigger dist → zoom IN → lower viewMpx
  viewMpx = Math.max(0.05, Math.min(2.0, pinch.startZoom / ratio));
  const zr = document.getElementById('zoom');
  if (zr) zr.value = viewMpx;
  // Pane activo: ancla world point bajo midpoint.
  const active = pinch.midPane;
  const r = active.wrap.getBoundingClientRect();
  const sActive = active.mPerPxNative / viewMpx;
  const newActivePanX = (midX - r.left) - pinch.worldX * sActive;
  const newActivePanY = (midY - r.top)  - pinch.worldY * sActive;
  // Delta del pane activo respecto al snapshot inicial del pinch (en pixels display).
  const startActive = (active === VP.crop) ? pinch.startCrop : pinch.startWms;
  const dPanX = newActivePanX - startActive.panX;
  const dPanY = newActivePanY - startActive.panY;
  active.panX = newActivePanX;
  active.panY = newActivePanY;
  // Pane "el otro": replicar exactamente el mismo delta de display px.
  // Como ambos comparten viewMpx, el on-screen movement coincide → sincronización solidaria.
  const other = (active === VP.crop) ? VP.wms : VP.crop;
  const startOther = (other === VP.crop) ? pinch.startCrop : pinch.startWms;
  other.panX = startOther.panX + dPanX;
  other.panY = startOther.panY + dPanY;
  scheduleTransform();
}

function panBoth(dx, dy) {
  VP.crop.panX += dx; VP.crop.panY += dy;
  VP.wms.panX  += dx; VP.wms.panY  += dy;
}

function isOnGreen(target) {
  if (!target) return null;
  if (target.id === 'poly-green' || (target.closest && target.closest('#poly-green'))) return 'crop';
  if (target.id === 'poly-green-ficha' || (target.closest && target.closest('#poly-green-ficha'))) return 'ficha';
  return null;
}

function paneKeyFromWrap(wrap) {
  if (!wrap) return null;
  if (wrap === VP.crop.wrap || wrap === VP.wms.wrap) return 'sync';   // crop+wms solidarios
  if (wrap === VP.ficha.wrap) return 'ficha';
  return null;
}

function onPointerDown(e) {
  // Only handle inside a canvas-wrap; ignore buttons/inputs.
  if (e.target.closest && e.target.closest('button, input, #token-modal, #mobile-actions, .zoom-bar')) return;
  const wrap = e.target.closest && e.target.closest('.canvas-wrap');
  if (!wrap) return;
  try { wrap.setPointerCapture(e.pointerId); } catch {}
  const onGreen = isOnGreen(e.target);
  const paneKey = paneKeyFromWrap(wrap);
  const entry = {
    id: e.pointerId,
    x: e.clientX, y: e.clientY,
    startX: e.clientX, startY: e.clientY,
    startPan: null,
    kind: 'pan-pane',
    wrap,
    paneKey,
  };
  if (onGreen && pointers.size === 0) {
    entry.kind = 'drag-poly';
    entry.dragTarget = onGreen;   // 'crop' o 'ficha'
    entry.startDragVec = (onGreen === 'ficha')
      ? { dx: dragVecFicha.dx, dy: dragVecFicha.dy }
      : { dx: dragVec.dx,      dy: dragVec.dy };
  } else {
    entry.kind = 'pan-pane';
    entry.startPanCrop  = { x: VP.crop.panX,  y: VP.crop.panY  };
    entry.startPanWms   = { x: VP.wms.panX,   y: VP.wms.panY   };
    entry.startPanFicha = { x: VP.ficha.panX, y: VP.ficha.panY };
  }
  pointers.set(e.pointerId, entry);

  if (pointers.size === 2) {
    startPinch();
  } else if (pointers.size === 1 && entry.kind === 'pan-pane') {
    // double-tap detection (only for single pointers, not on polygon)
    const now = Date.now();
    const dt = now - lastTap.t;
    const dx = e.clientX - lastTap.x;
    const dy = e.clientY - lastTap.y;
    if (dt < 300 && Math.hypot(dx, dy) < 40) {
      // double-tap → fit polygon
      doubleTapFit();
      lastTap = { t: 0, x: 0, y: 0 };
    } else {
      lastTap = { t: now, x: e.clientX, y: e.clientY };
    }
  }
  e.preventDefault();
}

function onPointerMove(e) {
  const entry = pointers.get(e.pointerId);
  if (!entry) return;
  entry.x = e.clientX;
  entry.y = e.clientY;

  if (pointers.size >= 2 && pinch) {
    updatePinch();
    e.preventDefault();
    return;
  }

  if (entry.kind === 'drag-poly') {
    const target = entry.dragTarget === 'ficha' ? VP.ficha : VP.crop;
    const s = paneScale(target) || 1;
    const nx = Math.round((e.clientX - entry.startX) / s + entry.startDragVec.dx);
    const ny = Math.round((e.clientY - entry.startY) / s + entry.startDragVec.dy);
    if (entry.dragTarget === 'ficha') { dragVecFicha.dx = nx; dragVecFicha.dy = ny; }
    else                              { dragVec.dx      = nx; dragVec.dy      = ny; }
    scheduleTransform();
    e.preventDefault();
  } else if (entry.kind === 'pan-pane') {
    const dx = e.clientX - entry.startX;
    const dy = e.clientY - entry.startY;
    if (entry.paneKey === 'ficha') {
      VP.ficha.panX = entry.startPanFicha.x + dx;
      VP.ficha.panY = entry.startPanFicha.y + dy;
    } else {
      VP.crop.panX = entry.startPanCrop.x + dx;
      VP.crop.panY = entry.startPanCrop.y + dy;
      VP.wms.panX  = entry.startPanWms.x  + dx;
      VP.wms.panY  = entry.startPanWms.y  + dy;
    }
    scheduleTransform();
    e.preventDefault();
  }
}

function onPointerEnd(e) {
  pointers.delete(e.pointerId);
  if (pointers.size < 2) pinch = null;
  if (pointers.size === 1) {
    // Refresh the surviving pointer's baselines so subsequent move math is correct.
    const [only] = [...pointers.values()];
    only.startX = only.x;
    only.startY = only.y;
    if (only.kind === 'pinch') {
      // demote: choose pan-pane (safer than drag-poly mid-gesture)
      only.kind = 'pan-pane';
    }
    only.startPanCrop  = { x: VP.crop.panX,  y: VP.crop.panY  };
    only.startPanWms   = { x: VP.wms.panX,   y: VP.wms.panY   };
    only.startPanFicha = { x: VP.ficha.panX, y: VP.ficha.panY };
    only.startDragVec  = (only.dragTarget === 'ficha')
      ? { dx: dragVecFicha.dx, dy: dragVecFicha.dy }
      : { dx: dragVec.dx, dy: dragVec.dy };
  }
}

function doubleTapFit() {
  if (!current) return;
  recenterAfterLoad();
}

function attachPointerHandlers() {
  // Attach to both wraps so events are scoped to canvas areas; capture ensures
  // we still get events if the finger drifts out.
  for (const key of ['crop', 'wms', 'ficha']) {
    const wrap = VP[key].wrap;
    if (!wrap) continue;
    wrap.addEventListener('pointerdown', onPointerDown);
    wrap.addEventListener('pointermove', onPointerMove);
    wrap.addEventListener('pointerup', onPointerEnd);
    wrap.addEventListener('pointercancel', onPointerEnd);
  }
}

async function loadRC(rc) {
  dragVec.dx = 0; dragVec.dy = 0;
  dragVecFicha.dx = 0; dragVecFicha.dy = 0;
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
  // pane native sizes & m/px for transform math
  VP.crop.mPerPxNative = data.crop_m_per_px;
  VP.crop.nativeSize   = data.crop_size_px;
  VP.wms.mPerPxNative  = data.wms_m_per_px;
  VP.wms.nativeSize    = data.wms_size_px;
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
  // Reset transform: imgs keep their native size; we only translate/scale via CSS.
  const cropImg = document.getElementById('crop');
  const wmsImg = document.getElementById('wms');
  // ensure imgs are not constrained by old style.width/height
  cropImg.style.width = ''; cropImg.style.height = '';
  wmsImg.style.width  = ''; wmsImg.style.height  = '';
  renderOverlay();

  let loaded = 0;
  const done = () => {
    loaded += 1;
    if (loaded < 2) return;
    requestAnimationFrame(() => requestAnimationFrame(() => {
      recenterAfterLoad();
      if (typeof setBusy === 'function') setBusy(false);
      if (typeof prefetchNext === 'function') prefetchNext();
    }));
  };
  cropImg.onload = done;
  wmsImg.onload = done;
  cropImg.src = imgUrl(data.crop_url);
  wmsImg.src = imgUrl(data.wms_url);

  // Ficha plano (4º panel) — opcional
  const mainEl = document.querySelector('main');
  const fichaImg = document.getElementById('ficha');
  const fichaEt = document.getElementById('ficha-etiqueta');
  if (data.ficha_url) {
    if (mainEl) mainEl.classList.add('has-ficha');
    if (fichaEt) fichaEt.textContent = data.ficha_etiqueta || '—';
    VP.ficha.mPerPxNative = data.ficha_m_per_px || 0.127;
    VP.ficha.nativeSize   = data.ficha_size_px || 0;
    VP.ficha.nativeSizeH  = data.ficha_size_px_h || data.ficha_size_px || 0;
    if (fichaImg) {
      fichaImg.style.width = ''; fichaImg.style.height = '';
      fichaImg.onload = () => {
        // centrar el polígono catastral en el viewport del panel ficha
        if (current && current.poly_ficha && current.poly_ficha.length) {
          let cx = 0, cy = 0;
          for (const [x, y] of current.poly_ficha) { cx += x; cy += y; }
          cx /= current.poly_ficha.length; cy /= current.poly_ficha.length;
          centerPaneOnPoint(VP.ficha, cx, cy);
          scheduleTransform();
        }
      };
      fichaImg.src = imgUrl(data.ficha_url);
    }
  } else {
    if (mainEl) mainEl.classList.remove('has-ficha');
    if (fichaImg) fichaImg.removeAttribute('src');
    if (fichaEt) fichaEt.textContent = '—';
    VP.ficha.nativeSize = 0;
  }
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

let _submitting = false;
function setBusy(on) {
  _submitting = on;
  const overlay = document.getElementById('busy-overlay');
  if (overlay) overlay.style.display = on ? 'flex' : 'none';
  ['btn-accept-d','btn-accept-m','btn-reject-d','btn-reject-m','btn-skip-d','btn-skip-m'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.disabled = on;
  });
}

async function submit(action) {
  if (_submitting) return;             // evita doble-click
  if (!current) return;
  setBusy(true);
  const payload = {
    rc: current.rc,
    action: action,
    dxdy: action === 'accept' ? [dragVec.dx, dragVec.dy] : [0, 0],
    snap_score: current.snap_score,
    snap_dxdy: current.snap_dxdy,
    cal_dxdy: current.cal_dxdy,
    ficha_dxdy: action === 'accept' ? [dragVecFicha.dx, dragVecFicha.dy] : [0, 0],
    ficha_etiqueta: current.ficha_etiqueta || null,
    ficha_filename: current.ficha_filename || null,
    ficha_cal_dxdy: [0, 0],   // por ahora siempre 0; cuando ficha_plano aplique offset, leerlo aquí
  };
  let r;
  try {
    r = await api('/api/label', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
  } catch (e) {
    setBusy(false);
    alert('error de red: ' + e.message);
    return;
  }
  if (!r.ok) { setBusy(false); alert('error guardando (' + r.status + ')'); return; }
  const resp = await r.json();
  if (resp.recalibrated) {
    // Recal corre out-of-band (sentinel data/.recal_pending). Las offsets nuevas
    // se aplican vía mtime-reload sin reiniciar el servicio, así que sólo
    // mostramos un toast breve y seguimos.
    document.getElementById('rc').textContent = '♻ recal en curso (fondo)';
  }
  // siguiente RC. setBusy(false) lo gestiona loadRC al terminar.
  if (__queue.length) {
    loadRC(__queue.shift());
  } else {
    if (__queue_total > 0) {
      const el = document.getElementById('rc');
      if (el) el.textContent = '✓ cola completada (' + __queue_total + ')';
      __queue_total = 0;
    }
    loadRC(null);
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

initVP();
attachPointerHandlers();
if (getToken()) loadInitial();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
