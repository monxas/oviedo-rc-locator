"""Registry de concejos asturianos para PR3 multi-concejo.

Cada `Concejo` agrupa los parámetros geométricos (malla 1:1000), los
portlets PDF del Ayuntamiento (Suelo Urbano, Suelo No Urbanizable, Fichas
de Ámbitos), el bbox UTM municipal y el workspace WFS del Principado.

Para añadir un nuevo concejo (e.g. Gijón id_ine=33024):

1. Crear instancia `Concejo` con sus `MallaParams`, `PortletSource` SU/SNU/
   fichas, `bbox_utm` y `wfs_workspace`. Sin malla → `malla=None` (algunos
   concejos no publican plano 1:1000 paginado).
2. Añadir a `REGISTRY[33024] = GIJON`.
3. Descargar PDFs SU/SNU/Fichas al cache (`scripts/fetch_*.py`
   parametrizados por concejo).
4. Calibrar la malla (LSQ con labels manuales vía `validator_ui`) y guardar
   offsets en `data/calibration/<ine>_<slug>.json`.
5. Smoke test con un RC conocido del concejo:
   `from oviedo_rc.concejo import get_concejo_for_utm; ...`
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class MallaParams:
    """Modelo geométrico del PGOU 1:1000 para un concejo.

    Body físico de cada subcuadrante (1 hoja PDF):
        ancho_m = cell_w/2 + 2*marg_x
        alto_m  = cell_h/2 + 2*marg_y

    `sub_convention` mapea el compás NW/NE/SW/SE → I/II/III/IV; algunos
    concejos pueden usar otra letra (verificar siempre con 3-4 RCs reales).
    """
    x0: float
    ymax: float
    cell_w: float
    cell_h: float
    marg_x: float
    marg_y: float
    ns_threshold: float = 0.5
    ew_threshold: float = 0.5
    sub_convention: dict = field(
        default_factory=lambda: {"NW": "I", "NE": "II", "SW": "III", "SE": "IV"}
    )

    @property
    def sub_w(self) -> float:
        return self.cell_w / 2

    @property
    def sub_h(self) -> float:
        return self.cell_h / 2

    @property
    def body_w_m(self) -> float:
        return self.sub_w + 2 * self.marg_x

    @property
    def body_h_m(self) -> float:
        return self.sub_h + 2 * self.marg_y


@dataclass(frozen=True)
class PortletSource:
    """Listado PDF Liferay: URL + nombre del cursor portlet + nº páginas."""
    url: str
    instance: str
    pages: int


@dataclass(frozen=True)
class Concejo:
    """Configuración de un concejo asturiano para el pipeline."""
    id_ine: int                          # 33044, 33024, 33004...
    slug: str                            # "oviedo", "gijon", "aviles"
    nombre: str                          # "Oviedo"
    bbox_utm: tuple                      # (xmin, ymin, xmax, ymax) total municipal
    urban_bbox: tuple                    # bbox suelo urbano (más restringido)
    malla: Optional[MallaParams] = None  # None si no tiene plano 1:1000 paginado
    pgou_su: Optional[PortletSource] = None
    snu: Optional[PortletSource] = None
    fichas: Optional[PortletSource] = None
    wfs_workspace: str = "E79_ENTIDADES_URBANISTICAS"
    snu_grid: Optional[dict] = None      # grid SNU (estructura snu_grid.json)


# ---------- Instancias ----------

OVIEDO = Concejo(
    id_ine=33044,
    slug="oviedo",
    nombre="Oviedo",
    bbox_utm=(253000, 4798000, 278000, 4815000),
    urban_bbox=(260000, 4801000, 275000, 4810000),
    malla=MallaParams(
        x0=253338.0196,
        ymax=4812335.9516,
        cell_w=1036.1505,
        cell_h=695.3860,
        marg_x=12.5165,
        marg_y=10.9430,
    ),
    pgou_su=PortletSource(
        url="https://www.oviedo.es/vive/urbanismo-e-infraestructuras/pgou/ficheros-pdf-suelo-urbano",
        instance="_com_liferay_document_library_web_portlet_IGDisplayPortlet_INSTANCE_7ckYazyK22lW_cur",
        pages=8,
    ),
    snu=PortletSource(
        url="https://www.oviedo.es/vive/urbanismo-e-infraestructuras/pgou/ficheros-pdf-suelo-no-urbanizable",
        instance="_com_liferay_document_library_web_portlet_IGDisplayPortlet_INSTANCE_J556oMSZtTY5_cur",
        pages=4,
    ),
    fichas=PortletSource(
        url="https://www.oviedo.es/vive/urbanismo-e-infraestructuras/pgou/fichas-de-ambitos",
        instance="_com_liferay_document_library_web_portlet_IGDisplayPortlet_INSTANCE_OrjqGhMJUXbr_cur",
        pages=12,
    ),
    wfs_workspace="E79_ENTIDADES_URBANISTICAS",
    snu_grid={
        "x0": 252290.93,
        "ymax": 4811487.92,
        "width": 29018.81,
        "height": 16194.95,
        "cols": 9,
        "rows": 10,
        "letters": "ABCDEFGHIJ",
    },
)


GIJON = Concejo(
    id_ine=33024,
    slug="gijon",
    nombre="Gijón",
    # Bbox UTM 30N tomado del KML PGOU (513 ámbitos). Sin malla 1:1000
    # paginada — Gijón usa polígonos vectoriales directos en gijon.py.
    bbox_utm=(270921.0, 4813038.0, 292773.0, 4829451.0),
    urban_bbox=(273000.0, 4815000.0, 290000.0, 4828000.0),
    malla=None,
    pgou_su=None,
    snu=None,
    fichas=None,
    wfs_workspace="E79_ENTIDADES_URBANISTICAS",
    snu_grid=None,
)

# TODO PR4: añadir Avilés, Siero, etc.

REGISTRY: dict[int, Concejo] = {
    33044: OVIEDO,
    33024: GIJON,
}


def get_concejo(id_ine: int) -> Concejo:
    """Devuelve el `Concejo` registrado para un INE.

    Lanza KeyError si el concejo no está en el registry.
    """
    if id_ine not in REGISTRY:
        raise KeyError(f"concejo {id_ine} no registrado")
    return REGISTRY[id_ine]


def get_concejo_for_utm(
    x: float, y: float, *, id_ine: Optional[int] = None
) -> Optional[Concejo]:
    """Busca el `Concejo` cuyo `bbox_utm` contiene el punto (x, y).

    Si se pasa `id_ine` (típicamente desde `catastro.consulta_dnprc().dt.loine`
    = cp+cm), devuelve directamente ese concejo. Esto es CORRECTO incluso
    cuando los bboxes de varios concejos solapan (Oviedo / Gijón se solapan
    en ~13.9 km²). Sin `id_ine`, fallback a iteración por bbox en orden de
    inserción del REGISTRY — ambiguo cuando solapan.

    Devuelve `None` si:
      - `id_ine` se pasó pero no está en el REGISTRY, o
      - las coords caen fuera de todos los concejos registrados.
    """
    if id_ine is not None:
        return REGISTRY.get(id_ine)
    for c in REGISTRY.values():
        x0, y0, x1, y1 = c.bbox_utm
        if x0 <= x <= x1 and y0 <= y <= y1:
            return c
    return None


def default_concejo() -> Concejo:
    """Concejo por defecto cuando no se puede inferir (legacy / scripts)."""
    return OVIEDO


__all__ = [
    "MallaParams",
    "PortletSource",
    "Concejo",
    "OVIEDO",
    "GIJON",
    "REGISTRY",
    "get_concejo",
    "get_concejo_for_utm",
    "default_concejo",
]
