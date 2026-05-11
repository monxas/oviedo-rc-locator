"""
Locator API · FastAPI sobre el paquete oviedo_rc.

Endpoints (Bearer auth):
  GET  /health             estado
  GET  /locate/{rc}        ejecuta pipeline y devuelve JSON con URLs a /img/<sha>.png
  GET  /img/{sha}.png      sirve PNG cacheado
  GET  /docs               markdown
"""
import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
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
from oviedo_rc import catastro, geom, snu as snu_mod, render as render_mod  # noqa: E402
from oviedo_rc import fichas as fichas_mod  # noqa: E402
from oviedo_rc import planeamiento as plan_mod  # noqa: E402

RC_RE = re.compile(r"^[0-9A-Z]{20}$")


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
    if candidate != TOKEN:
        raise HTTPException(401, "unauthorized — use Authorization: Bearer <token>, ?token=<token>, or cookie iarq_locator=<token>")
    return True


@app.get("/health")
async def health():
    return {"ok": True, "service": "locator", "cache_imgs": len(list(IMG_CACHE.glob("*.png")))}


@app.get("/docs", include_in_schema=False)
async def docs():
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
async def locate(rc: str, _=Depends(auth)):
    rc = rc.upper().strip()
    if not RC_RE.match(rc):
        raise HTTPException(422, "RC inválido — formato esperado: 20 chars alfanuméricos")

    t0 = time.time()
    try:
        bundle = process_rc(rc)
    except RCError as e:
        raise HTTPException(404, f"RC no resoluble: {e}")
    except Exception as e:
        raise HTTPException(500, f"pipeline error: {type(e).__name__}: {e}")

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
    # Diagnóstico
    notes: list[str] = []
    took_ms: int = 0


@app.get("/info/{rc}", response_model=InfoResp)
async def info(rc: str, _=Depends(auth)):
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
    except Exception as e:
        raise HTTPException(500, f"catastro: {type(e).__name__}: {e}")

    locate_dict = None
    snu_sheet = None
    snu_url = None
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
        # 2) Sin SU: fallback SNU
        notes.append(f"sin SU: {bundle_err}")
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
async def snu_endpoint(rc: str, _=Depends(auth)):
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
    except Exception as e:
        raise HTTPException(500, f"catastro error: {type(e).__name__}: {e}")

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
async def planeamiento_rc(rc: str, _=Depends(auth)):
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
    except Exception as e:
        raise HTTPException(500, f"catastro error: {type(e).__name__}: {e}")

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
async def fichas_list(tipo: Optional[str] = Query(None), _=Depends(auth)):
    """Lista de fichas de ámbitos. Filtra por tipo: UG, UG1, UG2, AU, AUS, AA, PE, PP."""
    items = fichas_mod.list_fichas(tipo=tipo)
    return {"total": len(items), "items": items}


@app.get("/fichas/search")
async def fichas_search(q: str = Query(..., min_length=1), _=Depends(auth)):
    """Busca por código (AIN, ASM…), número de ficha (506) o substring del nombre."""
    hits = fichas_mod.find_ficha(q)
    return {"total": len(hits), "items": hits[:50]}


@app.get("/fichas/{filename}")
async def fichas_pdf(filename: str, _=Depends(auth)):
    """Descarga el PDF de una ficha (debe acabar en .pdf)."""
    if not re.fullmatch(r"[A-Za-z0-9_\-]+\.pdf", filename):
        raise HTTPException(422, "filename inválido")
    p = fichas_mod.get_ficha_path(filename)
    if not p:
        raise HTTPException(404, f"ficha no encontrada: {filename}")
    return FileResponse(p, media_type="application/pdf", filename=filename)


@app.get("/img/{sha}.png")
async def img(sha: str, _=Depends(auth)):
    if not re.fullmatch(r"[a-f0-9]{64}", sha):
        raise HTTPException(404)
    path = IMG_CACHE / f"{sha}.png"
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type="image/png")


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
