"""
Locator API · FastAPI sobre el paquete oviedo_rc.

Endpoints (Bearer auth):
  GET  /health             estado
  GET  /locate/{rc}        ejecuta pipeline y devuelve JSON con URLs a /img/<sha>.png
  GET  /img/{sha}.png      sirve PNG cacheado
  GET  /docs               markdown
"""
import hashlib
import hmac
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("locator")

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel

ROOT = Path.home() / "oviedo-rc-locator"
CACHE_DIR = Path("/tmp/locator_cache")
IMG_CACHE = CACHE_DIR / "img"
IMG_CACHE.mkdir(parents=True, exist_ok=True)
ENV_FILE = ROOT / ".env"
README_PATH = ROOT / "API.md"

# import oviedo_rc del venv del proyecto
import sys
sys.path.insert(0, str(ROOT / "src"))
from oviedo_rc import process_rc, RCError  # noqa: E402
from oviedo_rc import catastro, geom, snu as snu_mod, render as render_mod, wms as wms_mod  # noqa: E402
from oviedo_rc import fichas as fichas_mod  # noqa: E402
from oviedo_rc import planeamiento as plan_mod  # noqa: E402

RC_RE = re.compile(r"^[0-9A-Z]{20}$")


_LAST_PRUNE = 0.0


def _prune_img_cache_if_needed():
    """Soft-cap on the /img cache: keep < 5000 files AND < 3 GB.

    When either threshold is exceeded, evict the oldest 20% (by mtime).
    Runs at most once per minute to avoid hammering the FS.
    """
    global _LAST_PRUNE
    now = time.time()
    if now - _LAST_PRUNE < 60:
        return
    _LAST_PRUNE = now
    try:
        files = list(IMG_CACHE.glob("*.png"))
    except OSError:
        return
    if len(files) < 5000:
        try:
            total = sum(f.stat().st_size for f in files)
        except OSError:
            return
        if total < 3 * 1024 * 1024 * 1024:
            return
    try:
        files.sort(key=lambda f: f.stat().st_mtime)
    except OSError:
        return
    to_remove = files[: max(1, len(files) // 5)]
    for f in to_remove:
        try:
            f.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def load_env():
    env = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    for k, v in os.environ.items():
        if k.startswith("LOCATOR_"):
            env[k] = v
    return env


ENV = load_env()
TOKEN = ENV.get("LOCATOR_TOKEN", "")
HOST = ENV.get("LOCATOR_HOST", "127.0.0.1")
PORT = int(ENV.get("LOCATOR_PORT", "9102"))
PUBLIC_BASE = ENV.get("LOCATOR_PUBLIC_BASE", "https://locator.iarquitectos.com")

if not TOKEN:
    raise RuntimeError("LOCATOR_TOKEN no definido en .env")


app = FastAPI(
    title="Oviedo RC Locator API",
    version="1.0",
    docs_url="/api/swagger",
    redoc_url=None,
    openapi_url="/api/openapi.json",
)


async def auth(request: Request, token: Optional[str] = Query(None)):
    candidate = None
    h = request.headers.get("authorization", "")
    if h.lower().startswith("bearer "):
        candidate = h.split(None, 1)[1].strip()
    elif token:
        candidate = token
    elif request.cookies.get("iarq_locator"):
        candidate = request.cookies["iarq_locator"]
    if not hmac.compare_digest(candidate or "", TOKEN):
        raise HTTPException(401, "unauthorized — use Authorization: Bearer <token>, ?token=<token>, or cookie iarq_locator=<token>")
    return True


@app.get("/health")
def health():
    return {"ok": True, "service": "locator", "cache_imgs": len(list(IMG_CACHE.glob("*.png")))}


@app.get("/docs", include_in_schema=False)
def docs():
    if not README_PATH.exists():
        return PlainTextResponse("docs unavailable", status_code=404)
    return PlainTextResponse(README_PATH.read_text(encoding="utf-8"),
                             media_type="text/markdown; charset=utf-8")


def _cache_png(src: Path) -> str:
    """Copia el PNG al cache con nombre SHA y devuelve sha."""
    data = src.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    dst = IMG_CACHE / f"{sha}.png"
    if not dst.exists():
        dst.write_bytes(data)
        _prune_img_cache_if_needed()
    return sha


class LocateResp(BaseModel):
    rc: str
    address: Optional[str] = None
    sheet: Optional[str] = None
    sheet_url: Optional[str] = None
    cell: Optional[str] = None
    sub_quadrant: Optional[str] = None
    utm: Optional[list[float]] = None
    body_relative: Optional[dict] = None
    polygon_area_m2: Optional[float] = None
    n_units: Optional[int] = None
    # imágenes
    plan_full_url: Optional[str] = None
    plan_zoom_url: Optional[str] = None
    polygon_url: Optional[str] = None
    wms_url: Optional[str] = None
    # snap
    snap_dx: Optional[int] = None
    snap_dy: Optional[int] = None
    snap_score: Optional[float] = None
    snap_confident: Optional[bool] = None
    edge_override: Optional[str] = None
    # calibración
    reliability: Optional[str] = None
    expected_residual_m: Optional[float] = None
    n_labels: Optional[int] = None
    # diagnóstico
    warnings: list[str] = []
    took_ms: int = 0


SNAP_SCORE_THRESHOLD = 0.30  # por debajo → snap_confident=False


@app.get("/locate/{rc}", response_model=LocateResp)
def locate(rc: str, _=Depends(auth)):
    rc = rc.upper().strip()
    if not RC_RE.match(rc):
        raise HTTPException(422, "RC inválido — formato esperado: 20 chars alfanuméricos")

    t0 = time.time()
    try:
        bundle = process_rc(rc)
    except RCError as e:
        raise HTTPException(404, f"RC no resoluble: {e}")
    except Exception:
        log.exception("locate pipeline failed for rc=%s", rc)
        raise HTTPException(503, detail={"error": "internal", "service": "pipeline"})

    meta = json.loads(Path(bundle.metadata_json).read_text(encoding="utf-8"))
    # cachear PNGs y dar URL pública
    def _url(p: Optional[str]) -> Optional[str]:
        if not p:
            return None
        path = Path(p)
        if not path.exists():
            return None
        sha = _cache_png(path)
        return f"{PUBLIC_BASE}/img/{sha}.png"

    snap = meta.get("snap", {}) or {}
    cal = meta.get("calibration_quality", {}) or {}
    snap_score = snap.get("score")
    snap_confident = (snap_score is not None and snap_score >= SNAP_SCORE_THRESHOLD)

    return LocateResp(
        rc=rc,
        address=meta.get("address"),
        sheet=meta.get("sheet_name"),
        sheet_url=meta.get("sheet_url"),
        cell=meta.get("cell"),
        sub_quadrant=meta.get("sub_quadrant"),
        utm=meta.get("utm"),
        body_relative=meta.get("body_relative"),
        polygon_area_m2=meta.get("polygon_area_m2"),
        n_units=meta.get("n_units"),
        plan_full_url=_url(bundle.plan_full_png),
        plan_zoom_url=_url(bundle.plan_zoom_png),
        polygon_url=_url(bundle.polygon_png),
        wms_url=_url(bundle.wms_png),
        snap_dx=snap.get("dx"),
        snap_dy=snap.get("dy"),
        snap_score=snap_score,
        snap_confident=snap_confident,
        edge_override=meta.get("edge_override"),
        reliability=cal.get("reliability"),
        expected_residual_m=cal.get("expected_residual_m"),
        n_labels=cal.get("n_labels"),
        warnings=meta.get("warnings") or [],
        took_ms=int((time.time() - t0) * 1000),
    )


class InfoResp(BaseModel):
    """Respuesta unificada: catastro + locate SU + planeamiento + patrimonio + SNU fallback."""
    rc: str
    address: Optional[str] = None
    utm: Optional[list[float]] = None
    # Suelo Urbano (si aplica)
    locate: Optional[dict] = None
    # Planeamiento
    ambito: Optional[str] = None
    uso_predominante: Optional[str] = None
    edificabilidad: Optional[float] = None
    densidad_viv_ha: Optional[float] = None
    sistema_actuacion: Optional[str] = None
    fichas_match: list = []
    # Patrimonio
    patrimonio: list = []
    # SNU (rurales)
    snu_sheet: Optional[str] = None
    snu_url: Optional[str] = None
    snu_polygon_url: Optional[str] = None
    # Diagnóstico
    notes: list[str] = []
    took_ms: int = 0


@app.get("/info/{rc}", response_model=InfoResp)
def info(rc: str, _=Depends(auth)):
    """Endpoint unificado: SU + planeamiento + patrimonio + SNU fallback en una sola request."""
    rc = rc.upper().strip()
    if not re.fullmatch(r"[0-9A-Z]{14}|[0-9A-Z]{20}", rc):
        raise HTTPException(422, "RC inválido (14 o 20 chars alfanuméricos)")
    t0 = time.time()
    notes: list[str] = []

    # Catastro (una sola vez)
    try:
        rc14 = rc[:14]
        X, Y, addr = catastro.rc_to_utm(rc14)
    except RCError as e:
        raise HTTPException(404, f"RC no resoluble: {e}")
    except Exception:
        log.exception("catastro rc_to_utm failed for rc=%s", rc)
        raise HTTPException(503, detail={"error": "upstream_unavailable", "service": "catastro"})

    # Address rural: si vacía, intenta componer desde DNPRC (paraje + pol/parc)
    if not addr:
        try:
            import urllib.request as _ur
            from oviedo_rc.config import HTTP_HEADERS as _H
            _url = ("https://ovc.catastro.meh.es/OVCServWeb/OVCWcfCallejero/"
                    f"COVCCallejero.svc/json/Consulta_DNPRC?RefCat={rc14}")
            _req = _ur.Request(_url, headers=_H)
            _d = json.loads(_ur.urlopen(_req, timeout=10).read())
            _bi = _d.get("consulta_dnprcResult", {}).get("bico", {}).get("bi", {})
            _dt = _bi.get("dt", {})
            _lorus = _dt.get("locs", {}).get("lors", {}).get("lorus", {})
            _npa = _lorus.get("npa", "").strip()
            _cpo = _lorus.get("cpp", {}).get("cpo", "")
            _cpa = _lorus.get("cpp", {}).get("cpa", "")
            _nm = _dt.get("nm", "").strip()
            parts = []
            if _npa:
                parts.append(_npa)
            if _cpo or _cpa:
                parts.append(f"Pol {_cpo} Parc {_cpa}")
            if _nm:
                parts.append(_nm)
            if parts:
                addr = " · ".join(parts)
        except Exception:
            pass

    locate_dict = None
    snu_sheet = None
    snu_url = None
    snu_polygon_url = None
    bundle = None
    bundle_err: Optional[Exception] = None

    # Paraleliza pipeline SU + planeamiento WFS (ambos I/O bound)
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=2) as pool:
        f_bundle = pool.submit(process_rc, rc)
        f_plan = pool.submit(plan_mod.lookup, X, Y)
        plan = f_plan.result()
        try:
            bundle = f_bundle.result()
        except Exception as e:
            bundle_err = e

    # 1) Pipeline SU si funcionó
    if bundle is not None:
        meta = json.loads(Path(bundle.metadata_json).read_text(encoding="utf-8"))
        snap = meta.get("snap", {}) or {}
        cal = meta.get("calibration_quality", {}) or {}

        def _url(p):
            if not p:
                return None
            path = Path(p)
            if not path.exists():
                return None
            return f"{PUBLIC_BASE}/img/{_cache_png(path)}.png"

        locate_dict = {
            "sheet": meta.get("sheet_name"),
            "cell": meta.get("cell"),
            "sub_quadrant": meta.get("sub_quadrant"),
            "polygon_area_m2": meta.get("polygon_area_m2"),
            "plan_zoom_url": _url(bundle.plan_zoom_png),
            "polygon_url": _url(bundle.polygon_png),
            "wms_url": _url(bundle.wms_png),
            "snap_score": snap.get("score"),
            "reliability": cal.get("reliability"),
        }
    else:
        # 2) Sin SU: fallback SNU + WMS catastral con polígono parcela
        err_str = str(bundle_err)
        # Errores típicos de RC rural ("Formato de RC inválido", "No se encontró hoja PLANO_...")
        # son esperados — no los exponemos como aviso al usuario, sólo si son raros.
        if "Formato de RC inválido" not in err_str and "No se encontró hoja" not in err_str:
            notes.append(f"sin SU: {err_str}")
        try:
            sheet = snu_mod.resolve_snu_sheet(X, Y)
            if sheet:
                pdf_path = snu_mod.fetch_snu_sheet_pdf(sheet)
                png_path = CACHE_DIR / f"snu_{sheet}.png"
                if not png_path.exists():
                    img, _, _ = render_mod.render_pdf_page(pdf_path, dpi=120)
                    import cv2
                    cv2.imwrite(str(png_path), img)
                snu_sheet = sheet
                snu_url = f"{PUBLIC_BASE}/img/{_cache_png(png_path)}.png"
        except Exception as e2:
            notes.append(f"snu fail: {type(e2).__name__}")

        # Polígono catastral sobre WMS (rural pipeline mínimo)
        try:
            poly = catastro.get_parcel_polygon(rc14)
            if poly and poly.get("polygon_utm"):
                import cv2
                pu = poly["polygon_utm"]
                xs = [p[0] for p in pu]
                ys = [p[1] for p in pu]
                pad = max(50.0, 0.4 * max(max(xs) - min(xs), max(ys) - min(ys)))
                xmin = min(xs) - pad; xmax = max(xs) + pad
                ymin = min(ys) - pad; ymax = max(ys) + pad
                img_bytes = wms_mod.get(xmin, ymin, xmax, ymax, w=900)
                if img_bytes:
                    import numpy as np
                    arr = np.frombuffer(img_bytes, dtype=np.uint8)
                    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                else:
                    img = None
                if img is not None:
                    h, w = img.shape[:2]
                    poly_px = [
                        (int((px - xmin) / (xmax - xmin) * w),
                         int((ymax - py) / (ymax - ymin) * h))
                        for px, py in pu
                    ]
                    annotated = render_mod.draw_polygon(img.copy(), poly_px,
                                                         color=(0, 0, 255), thickness=3)
                    out = CACHE_DIR / f"rural_{rc14}.png"
                    cv2.imwrite(str(out), annotated)
                    rural_url = f"{PUBLIC_BASE}/img/{_cache_png(out)}.png"
                    locate_dict = {
                        "sheet": None,
                        "cell": None,
                        "sub_quadrant": None,
                        "polygon_area_m2": poly.get("area_m2"),
                        "plan_zoom_url": None,
                        "polygon_url": rural_url,
                        "wms_url": rural_url,
                        "snap_score": None,
                        "reliability": "rural",
                    }

                # Polígono sobre plano SNU (calidad ~aproximada, grid bbox)
                if snu_sheet:
                    try:
                        annotated_snu = snu_mod.overlay_polygon(snu_sheet, pu)
                        if annotated_snu is not None:
                            out2 = CACHE_DIR / f"rural_snu_{rc14}.png"
                            cv2.imwrite(str(out2), annotated_snu)
                            snu_polygon_url = f"{PUBLIC_BASE}/img/{_cache_png(out2)}.png"
                    except Exception as e4:
                        notes.append(f"snu overlay fail: {type(e4).__name__}")
        except Exception as e3:
            notes.append(f"rural pipeline fail: {type(e3).__name__}: {str(e3)[:60]}")

    ug = plan.get("ug") or {}

    return InfoResp(
        rc=rc, address=addr, utm=[X, Y],
        locate=locate_dict,
        ambito=plan.get("ambito"),
        uso_predominante=(ug.get("Uso_predominante")
                           or (plan.get("layers", {}).get("n12_USOS_PORMENORIZADOS") or [{}])[0].get("Uso_Predominante")),
        edificabilidad=ug.get("Edificabilidad_(m.2/m.2)"),
        densidad_viv_ha=ug.get("Densidad_(Viv./Ha.)"),
        sistema_actuacion=ug.get("Sistema_de_Actuación"),
        fichas_match=plan.get("fichas_match", []),
        patrimonio=plan.get("patrimonio", []),
        snu_sheet=snu_sheet, snu_url=snu_url,
        snu_polygon_url=snu_polygon_url,
        notes=notes,
        took_ms=int((time.time() - t0) * 1000),
    )


class SNUResp(BaseModel):
    rc: str
    address: Optional[str] = None
    utm: Optional[list[float]] = None
    snu_sheet: Optional[str] = None
    snu_url: Optional[str] = None
    note: str = ""
    took_ms: int = 0


@app.get("/snu/{rc}", response_model=SNUResp)
def snu_endpoint(rc: str, _=Depends(auth)):
    """Fallback para RCs en Suelo No Urbanizable (sin hoja SU).

    Devuelve la hoja SNU (PLANO_<letra>_<num>.pdf) más probable según el bbox
    UTM del Mapa Guía SNU + el PNG renderizado de esa hoja. No georeferencia
    el polígono sobre la hoja (calibración SNU pendiente).
    """
    rc = rc.upper().strip()
    if not re.fullmatch(r"[0-9A-Z]{14}|[0-9A-Z]{20}", rc):
        raise HTTPException(422, "RC inválido (14 o 20 chars alfanuméricos)")
    t0 = time.time()
    try:
        rc14 = rc[:14]
        X, Y, addr = catastro.rc_to_utm(rc14)
    except RCError as e:
        raise HTTPException(404, f"RC no resoluble: {e}")
    except Exception:
        log.exception("snu catastro rc_to_utm failed for rc=%s", rc)
        raise HTTPException(503, detail={"error": "upstream_unavailable", "service": "catastro"})

    sheet_name = snu_mod.resolve_snu_sheet(X, Y)
    if not sheet_name:
        return SNUResp(
            rc=rc, address=addr, utm=[X, Y],
            note="UTM fuera del grid SNU calibrado",
            took_ms=int((time.time() - t0) * 1000),
        )

    snu_url: Optional[str] = None
    note = ""
    try:
        pdf_path = snu_mod.fetch_snu_sheet_pdf(sheet_name)
        png_path = CACHE_DIR / f"snu_{sheet_name}.png"
        if not png_path.exists():
            img, _, _ = render_mod.render_pdf_page(pdf_path, dpi=120)
            import cv2
            cv2.imwrite(str(png_path), img)
        sha = _cache_png(png_path)
        snu_url = f"{PUBLIC_BASE}/img/{sha}.png"
    except Exception as e:
        note = f"render falló: {type(e).__name__}: {str(e)[:80]}"

    return SNUResp(
        rc=rc, address=addr, utm=[X, Y],
        snu_sheet=sheet_name, snu_url=snu_url,
        note=note,
        took_ms=int((time.time() - t0) * 1000),
    )


class PlanResp(BaseModel):
    rc: str
    address: Optional[str] = None
    utm: Optional[list[float]] = None
    ambito: Optional[str] = None
    ug: Optional[dict] = None
    layers: dict = {}
    fichas_match: list = []
    took_ms: int = 0


@app.get("/planeamiento/{rc}", response_model=PlanResp)
def planeamiento_rc(rc: str, _=Depends(auth)):
    """Info de planeamiento PGOU por RC: ámbito (UG/AU/PE), uso predominante, ficha sugerida.

    Combina catastro (RC→UTM) + GeoServer Asturias (UTM→ámbito) + fichas locales.
    """
    rc = rc.upper().strip()
    if not re.fullmatch(r"[0-9A-Z]{14}|[0-9A-Z]{20}", rc):
        raise HTTPException(422, "RC inválido (14 o 20 chars alfanuméricos)")
    t0 = time.time()
    try:
        rc14 = rc[:14]
        X, Y, addr = catastro.rc_to_utm(rc14)
    except RCError as e:
        raise HTTPException(404, f"RC no resoluble: {e}")
    except Exception:
        log.exception("planeamiento catastro rc_to_utm failed for rc=%s", rc)
        raise HTTPException(503, detail={"error": "upstream_unavailable", "service": "catastro"})

    info = plan_mod.lookup(X, Y)
    return PlanResp(
        rc=rc, address=addr, utm=[X, Y],
        ambito=info.get("ambito"),
        ug=info.get("ug"),
        layers={k: v for k, v in info.get("layers", {}).items() if v},
        fichas_match=info.get("fichas_match", []),
        took_ms=int((time.time() - t0) * 1000),
    )


@app.get("/fichas")
def fichas_list(tipo: Optional[str] = Query(None), _=Depends(auth)):
    """Lista de fichas de ámbitos. Filtra por tipo: UG, UG1, UG2, AU, AUS, AA, PE, PP."""
    items = fichas_mod.list_fichas(tipo=tipo)
    return {"total": len(items), "items": items}


@app.get("/fichas/search")
def fichas_search(q: str = Query(..., min_length=1), _=Depends(auth)):
    """Busca por código (AIN, ASM…), número de ficha (506) o substring del nombre."""
    hits = fichas_mod.find_ficha(q)
    return {"total": len(hits), "items": hits[:50]}


@app.get("/fichas/{filename}")
def fichas_pdf(filename: str, _=Depends(auth)):
    """Descarga el PDF de una ficha (debe acabar en .pdf)."""
    if not re.fullmatch(r"[A-Za-z0-9_\-]+\.pdf", filename):
        raise HTTPException(422, "filename inválido")
    p = fichas_mod.get_ficha_path(filename)
    if not p:
        raise HTTPException(404, f"ficha no encontrada: {filename}")
    return FileResponse(p, media_type="application/pdf", filename=filename)


@app.get("/v/{rc}", response_class=HTMLResponse)
def view_rc(rc: str, token: Optional[str] = Query(None)):
    """Dashboard HTML standalone de la info completa de un RC.

    No requiere Bearer: el token se pasa como `?token=` para los fetch JS.
    """
    rc_safe = re.sub(r"[^0-9A-Z]", "", rc.upper())[:20]
    if len(rc_safe) not in (14, 20):
        raise HTTPException(422, "RC inválido")
    tok = token or ""
    html = _VIEW_HTML.replace("{{RC}}", rc_safe).replace("{{TOKEN}}", tok)
    return HTMLResponse(content=html)


_VIEW_HTML = """<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{RC}} · Info PGOU Oviedo</title>
<style>
body{margin:0;font-family:-apple-system,BlinkMacSystemFont,system-ui,sans-serif;background:#f5f5f7;color:#1d1d1f}
header{background:#1d1d1f;color:#fff;padding:14px 20px;display:flex;justify-content:space-between;align-items:center}
header h1{margin:0;font-size:16px;font-weight:500}
header .addr{font-size:13px;opacity:.7}
main{max-width:1400px;margin:0 auto;padding:20px;display:grid;grid-template-columns:1fr 1fr;gap:20px}
.card{background:#fff;border-radius:12px;padding:18px;box-shadow:0 1px 3px rgba(0,0,0,.08)}
.card h2{margin:0 0 12px;font-size:14px;font-weight:600;text-transform:uppercase;letter-spacing:.5px;color:#6e6e73}
.card.wide{grid-column:span 2}
.kv{display:grid;grid-template-columns:160px 1fr;gap:6px 12px;font-size:14px}
.kv .k{color:#6e6e73}
img.plan{width:100%;height:auto;border-radius:8px;background:#eee;display:block}
.imgs{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.pill{display:inline-block;padding:2px 8px;border-radius:10px;font-size:12px;background:#eef;color:#226}
.pill.warn{background:#fee;color:#a30}
.pill.ok{background:#dfd;color:#070}
ul{margin:0;padding-left:18px;font-size:14px}
.muted{color:#6e6e73;font-size:13px}
a{color:#0066cc;text-decoration:none}
a:hover{text-decoration:underline}
.loading{text-align:center;padding:60px;color:#6e6e73}
.err{background:#fee;color:#a30;padding:10px;border-radius:8px;margin:10px 0}
</style></head><body>
<header><h1 id="title">Cargando {{RC}}…</h1><span class="addr" id="addr"></span></header>
<main id="root"><div class="loading">Consultando catastro + planeamiento + patrimonio…</div></main>
<script>
const RC = "{{RC}}";
const TOKEN = "{{TOKEN}}";
const H = TOKEN ? {Authorization: "Bearer " + TOKEN} : {};

function fmt(v){return v==null||v===""?"—":v}
function authUrl(u){if(!u||!TOKEN)return u;return u + (u.indexOf("?")>-1?"&":"?") + "token=" + encodeURIComponent(TOKEN);}
function card(title, body, wide){
  return `<div class="card${wide?' wide':''}"><h2>${title}</h2>${body}</div>`;
}
function kv(rows){
  return `<div class="kv">${rows.map(([k,v])=>`<span class="k">${k}</span><span>${fmt(v)}</span>`).join("")}</div>`;
}

fetch(`/info/${RC}`,{headers:H}).then(r=>{
  if(!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}).then(d=>{
  document.getElementById("title").textContent = d.rc;
  document.getElementById("addr").textContent = d.address || "";

  const root = document.getElementById("root");
  const out = [];

  // Resumen
  const ambito = d.ambito ? `<span class="pill">${d.ambito}</span>` : '<span class="pill warn">sin ámbito</span>';
  const patBadge = (d.patrimonio||[]).length>0 ? `<span class="pill warn">${d.patrimonio.length} afección/es</span>` : '<span class="pill ok">sin afecciones</span>';
  out.push(card("Resumen", kv([
    ["RC", d.rc],
    ["Dirección", d.address],
    ["UTM (ETRS89 30N)", d.utm?d.utm.map(x=>x.toFixed(1)).join(", "):""],
    ["Ámbito", ambito],
    ["Uso predominante", d.uso_predominante],
    ["Edificabilidad", d.edificabilidad!=null?`${d.edificabilidad} m²/m²`:null],
    ["Densidad", d.densidad_viv_ha!=null?`${d.densidad_viv_ha} viv/ha`:null],
    ["Sistema de actuación", d.sistema_actuacion],
    ["Patrimonio", patBadge],
    ["Latencia", `${d.took_ms} ms`],
  ])));

  // Plano SU (si)
  if(d.locate){
    const imgs = [];
    if(d.locate.plan_zoom_url){const u=authUrl(d.locate.plan_zoom_url); imgs.push(`<a href="${u}" target="_blank"><img class="plan" src="${u}" alt="plano"></a>`);}
    if(d.locate.polygon_url){const u=authUrl(d.locate.polygon_url); imgs.push(`<a href="${u}" target="_blank"><img class="plan" src="${u}" alt="polígono"></a>`);}
    if(d.locate.wms_url){const u=authUrl(d.locate.wms_url); imgs.push(`<a href="${u}" target="_blank"><img class="plan" src="${u}" alt="wms"></a>`);}
    out.push(card(`Plano PGOU · ${d.locate.sheet||""} · cell ${d.locate.cell||""}-${d.locate.sub_quadrant||""}`,
      `<div class="imgs">${imgs.join("")}</div><div class="muted" style="margin-top:8px">snap_score: ${fmt(d.locate.snap_score)} · reliability: ${fmt(d.locate.reliability)} · área: ${fmt(d.locate.polygon_area_m2)} m²</div>`, true));
  } else if(d.snu_url){
    out.push(card(`Hoja SNU · ${d.snu_sheet}`,
      `<a href="${authUrl(d.snu_url)}" target="_blank"><img class="plan" src="${authUrl(d.snu_url)}" alt="snu"></a><div class="muted" style="margin-top:8px">RC en Suelo No Urbanizable — sin plano SU</div>`, true));
  }

  // Patrimonio
  if((d.patrimonio||[]).length>0){
    const items = d.patrimonio.map(p=>`<li><strong>${p.nombre||"?"}</strong> <span class="muted">${p.tipo_patrimonio||""} · ${p.nivel_proteccion||""}</span></li>`).join("");
    out.push(card("Afecciones patrimonio / dominio público", `<ul>${items}</ul>`));
  }

  // Ficha sugerida
  if((d.fichas_match||[]).length>0){
    const top = d.fichas_match[0];
    const link = `/fichas/${encodeURIComponent(top.filename)}${TOKEN?`?token=${TOKEN}`:""}`;
    const otros = d.fichas_match.slice(1).map(f=>`<li><a href="/fichas/${encodeURIComponent(f.filename)}${TOKEN?`?token=${TOKEN}`:""}" target="_blank">${f.filename}</a> <span class="muted">(score ${f.score})</span></li>`).join("");
    out.push(card("Ficha de Ámbito sugerida",
      `<div><a href="${link}" target="_blank" style="font-size:15px;font-weight:500">${top.filename}</a> <span class="muted">score ${top.score}</span></div>`+
      (otros?`<details style="margin-top:8px"><summary class="muted" style="cursor:pointer">Otros candidatos (${d.fichas_match.length-1})</summary><ul>${otros}</ul></details>`:"")));
  }

  // Notas
  if((d.notes||[]).length>0){
    out.push(card("Notas", `<ul>${d.notes.map(n=>`<li class="muted">${n}</li>`).join("")}</ul>`));
  }

  root.innerHTML = out.join("");
}).catch(e=>{
  document.getElementById("root").innerHTML = `<div class="err">Error: ${e.message}. Si el endpoint es privado, añade ?token=&lt;tu_token&gt; a la URL.</div>`;
});
</script></body></html>
"""


@app.get("/img/{sha}.png")
def img(sha: str, _=Depends(auth)):
    if not re.fullmatch(r"[a-f0-9]{64}", sha):
        raise HTTPException(404)
    path = IMG_CACHE / f"{sha}.png"
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type="image/png")


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
