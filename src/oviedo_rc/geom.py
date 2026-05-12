"""Modelo geométrico: RC → cell + sub-cuadrante + body_relative.

El modelo está calibrado por LSQ con 71 RCs reales (mediana 4.5 m, p90 7.75 m).
Las constantes específicas de cada concejo viven en `concejo.py`
(`Concejo.malla`); `config.MALLA_*` son aliases backwards-compat de OVIEDO.
"""
from .config import RC_RE
from .concejo import OVIEDO, Concejo, get_concejo_for_utm
from .errors import RCError
from . import catastro
from . import pgou


def validate_rc(rc):
    """Normaliza y valida formato. Devuelve los 14 primeros chars."""
    if not isinstance(rc, str):
        raise RCError("RC debe ser una cadena")
    rc = rc.strip().upper()
    if len(rc) not in (14, 20):
        raise RCError(f"RC debe tener 14 o 20 caracteres, recibido {len(rc)}: {rc!r}")
    if not RC_RE.match(rc):
        raise RCError(f"Formato de RC inválido: {rc!r}")
    return rc[:14]


def _check_in_concejo(X, Y, addr, concejo):
    xmin, ymin, xmax, ymax = concejo.bbox_utm
    if not (xmin <= X <= xmax and ymin <= Y <= ymax):
        raise RCError(
            f"RC fuera del bbox del concejo {concejo.nombre}: UTM=({X:.0f},{Y:.0f}). "
            f"Dirección: {addr or '(desconocida)'}"
        )


def _resolve_concejo(X, Y, concejo):
    """Si concejo es None, intenta inferir vía UTM; si tampoco se puede,
    cae en OVIEDO por compat. Devuelve siempre un Concejo."""
    if concejo is not None:
        return concejo
    auto = get_concejo_for_utm(X, Y)
    return auto or OVIEDO


def utm_to_body_relative(X, Y, col, row_idx, compass, concejo: Concejo | None = None):
    """Mapeo UTM → body_relative (rx, ry) ∈ [0,1]² dentro del body del plano."""
    c = concejo or OVIEDO
    m = c.malla
    if m is None:
        raise RCError(f"Concejo {c.nombre} sin malla 1:1000 definida")
    sub_x_off = 0 if "W" in compass else m.cell_w / 2
    sub_y_off = 0 if "N" in compass else m.cell_h / 2
    body_x_min = m.x0 + col * m.cell_w + sub_x_off - m.marg_x
    body_y_max = m.ymax - row_idx * m.cell_h - sub_y_off + m.marg_y
    rx = (X - body_x_min) / m.body_w_m
    ry = (body_y_max - Y) / m.body_h_m
    return rx, ry


def locate(rc, concejo: Concejo | None = None):
    """RC → diccionario con plano, posición y warnings.

    Si `concejo` es None se infiere desde la UTM tras resolver el RC en
    catastro (fallback a OVIEDO).

    Returns: dict con rc, address, utm, cell, sub_quadrant, sub_compass,
    sheet_name, sheet_url, intra_cell_position, body_relative, warnings.
    Lanza RCError si la entrada o resultado no son válidos.
    """
    rc14 = validate_rc(rc)
    X, Y, addr = catastro.rc_to_utm(rc14)
    concejo = _resolve_concejo(X, Y, concejo)
    _check_in_concejo(X, Y, addr, concejo)

    m = concejo.malla
    if m is None:
        raise RCError(
            f"Concejo {concejo.nombre} no tiene malla 1:1000 — usar SNU fallback"
        )

    col = int((X - m.x0) // m.cell_w)
    row_idx = int((m.ymax - Y) // m.cell_h)
    if not (0 <= row_idx < 25):
        raise RCError(f"Fila fuera de rango: row_idx={row_idx} (UTM Y={Y})")
    letter = "ABCDEFGHIJKLMNOPQRSTUVWXY"[row_idx]

    x_in = (X - (m.x0 + col * m.cell_w)) / m.cell_w
    y_in = (m.ymax - row_idx * m.cell_h - Y) / m.cell_h
    compass = ("N" if y_in < m.ns_threshold else "S") + \
              ("W" if x_in < m.ew_threshold else "E")
    sub = m.sub_convention[compass]

    warnings = []
    border_m = min(min(x_in, 1 - x_in) * m.cell_w,
                   min(y_in, 1 - y_in) * m.cell_h)
    if border_m < 50:
        x_to_internal = abs(x_in - 0.5) * m.cell_w
        y_to_internal = abs(y_in - 0.5) * m.cell_h
        if min(x_to_internal, y_to_internal) < 50:
            warnings.append(
                f"RC a {min(x_to_internal, y_to_internal):.0f} m del borde interno "
                "entre sub-cuadrantes."
            )
        else:
            warnings.append(
                f"RC a {border_m:.0f} m del borde EXTERNO de la cell {col}-{letter}."
            )

    name = f"PLANO_{col}_{letter}_{sub}.pdf"
    sheets = pgou.get_sheet_listing(concejo)
    sheet_url = sheets.get(name)
    if not sheet_url:
        raise RCError(
            f"No se encontró hoja '{name}' en el listado del Ayuntamiento."
        )

    rx, ry = utm_to_body_relative(X, Y, col, row_idx, compass, concejo)

    return {
        "rc": rc.strip().upper(),
        "address": addr,
        "utm": (X, Y),
        "cell": f"{col}-{letter}",
        "sub_quadrant": sub,
        "sub_compass": compass,
        "sheet_name": name,
        "sheet_url": sheet_url,
        "intra_cell_position": {
            "x_west_to_east": round(x_in, 3),
            "y_north_to_south": round(y_in, 3),
        },
        "body_relative": {"rx": round(rx, 4), "ry": round(ry, 4)},
        "warnings": warnings,
    }
