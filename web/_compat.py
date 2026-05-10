"""Shim de compatibilidad: expone L (legacy locate_rc) y P (legacy parcel_layer)
delegando al paquete `oviedo_rc`. Permite reusar los scripts heredados sin
reescribirlos."""
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from oviedo_rc import config, pgou, catastro, http_utils, geom  # noqa: E402

L = SimpleNamespace(
    CACHE_DIR=config.CACHE_DIR,
    PARCELS_DIR=config.PARCELS_DIR,
    COORDS_FILE=config.COORDS_FILE,
    SHEETS_FILE=config.SHEETS_FILE,
    PDF_DPI=config.PDF_DPI,
    URBAN_BBOX=config.URBAN_BBOX,
    BBOX_OVIEDO=config.BBOX_OVIEDO,
    get_sheet_listing=pgou.get_sheet_listing,
    fetch_sheet_pdf=pgou.fetch_sheet_pdf,
    fetch=http_utils.fetch,
    http_get=http_utils.http_get,
    rc_to_utm=catastro.rc_to_utm,
    locate=geom.locate,
    validate_rc=geom.validate_rc,
)

P = SimpleNamespace(
    CACHE_DIR=config.PARCELS_DIR,
    wfs_parcels_bbox=catastro.wfs_parcels_bbox,
    get_parcel_polygon=catastro.get_parcel_polygon,
    consulta_dnprc=catastro.consulta_dnprc,
)
