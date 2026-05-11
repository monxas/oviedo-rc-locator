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
