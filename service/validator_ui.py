"""
Validator UI · entry point FastAPI app.

Servicio FastAPI: corre en VM puerto 9103, expuesto como validator.iarquitectos.com.

Pantalla única para corregir snap del PGOU:
- WMS catastral (referencia) · PGOU + polígono · ficha de ámbito · panel info.
- Snap siempre activo; banner rojo si snap_score < 0.30.
- Atajos: A = aceptar, X = error_grande, S = skip.
- Auto-recal cada 30 aceptaciones (sentinel out-of-band).

Estructura del paquete `service/validator/`:
- env.py       · carga ~/.validator.env, TOKEN/HOST/PORT
- labels.py    · IO de data/validator_labels.json (+ ficha)
- recal.py    · contador + sentinel data/.recal_pending
- routes.py   · APIRouter con todos los endpoints
- templates/  · index.html
- static/     · validator.js, validator.css
"""
import sys
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

ROOT = Path.home() / "oviedo-rc-locator"
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(ROOT / "src"))

from validator.env import HOST, PORT  # noqa: E402
from validator.routes import router  # noqa: E402

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

app.include_router(router)


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
