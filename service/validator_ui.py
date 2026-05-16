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
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path.home() / "oviedo-rc-locator"
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(ROOT / "src"))
from validator.labels import (  # noqa: E402
    LABELS_PATH,
    FICHA_LABELS_PATH,
    _LABELS_LOCK,
    load_labels,
    save_labels,
    labeled_rcs,
    load_ficha_labels,
    save_ficha_labels,
)
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

app.mount(
    "/static",
    StaticFiles(directory=str(Path(__file__).parent / "validator" / "static")),
    name="static",
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


_TEMPLATE_PATH = Path(__file__).parent / "validator" / "templates" / "index.html"
INDEX_HTML = _TEMPLATE_PATH.read_text(encoding="utf-8")


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
