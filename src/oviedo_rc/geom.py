"""Modelo geométrico: RC → cell + sub-cuadrante + body_relative.

El modelo está calibrado por LSQ con 71 RCs reales (mediana 4.5 m, p90 7.75 m).
Las constantes están en `config.py`.
"""
from .config import (
    MALLA_X0, MALLA_YMAX, MALLA_CELL_W, MALLA_CELL_H,
    MALLA_MARG_X, MALLA_MARG_Y, BODY_W_M, BODY_H_M,
    SUB_CONVENTION, NS_THRESHOLD, EW_THRESHOLD,
    BBOX_OVIEDO, RC_RE,
)
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


def _check_in_oviedo(X, Y, addr):
    xmin, ymin, xmax, ymax = BBOX_OVIEDO
    if not (xmin <= X <= xmax and ymin <= Y <= ymax):
        raise RCError(
            f"RC fuera del bbox del suelo urbano de Oviedo: UTM=({X:.0f},{Y:.0f}). "
            f"Dirección: {addr or '(desconocida)'}"
        )


def utm_to_body_relative(X, Y, col, row_idx, compass):
    """Mapeo UTM → body_relative (rx, ry) ∈ [0,1]² dentro del body del plano."""
    sub_x_off = 0 if "W" in compass else MALLA_CELL_W / 2
    sub_y_off = 0 if "N" in compass else MALLA_CELL_H / 2
    body_x_min = MALLA_X0 + col * MALLA_CELL_W + sub_x_off - MALLA_MARG_X
    body_y_max = MALLA_YMAX - row_idx * MALLA_CELL_H - sub_y_off + MALLA_MARG_Y
    rx = (X - body_x_min) / BODY_W_M
    ry = (body_y_max - Y) / BODY_H_M
    return rx, ry


def locate(rc):
    """RC → diccionario con plano, posición y warnings.

    Returns: dict con rc, address, utm, cell, sub_quadrant, sub_compass,
    sheet_name, sheet_url, intra_cell_position, body_relative, warnings.
    Lanza RCError si la entrada o resultado no son válidos.
    """
    rc14 = validate_rc(rc)
    X, Y, addr = catastro.rc_to_utm(rc14)
    _check_in_oviedo(X, Y, addr)

    col = int((X - MALLA_X0) // MALLA_CELL_W)
    row_idx = int((MALLA_YMAX - Y) // MALLA_CELL_H)
    if not (0 <= row_idx < 25):
        raise RCError(f"Fila fuera de rango: row_idx={row_idx} (UTM Y={Y})")
    letter = "ABCDEFGHIJKLMNOPQRSTUVWXY"[row_idx]

    x_in = (X - (MALLA_X0 + col * MALLA_CELL_W)) / MALLA_CELL_W
    y_in = (MALLA_YMAX - row_idx * MALLA_CELL_H - Y) / MALLA_CELL_H
    compass = ("N" if y_in < NS_THRESHOLD else "S") + \
              ("W" if x_in < EW_THRESHOLD else "E")
    sub = SUB_CONVENTION[compass]

    warnings = []
    border_m = min(min(x_in, 1 - x_in) * MALLA_CELL_W,
                   min(y_in, 1 - y_in) * MALLA_CELL_H)
    if border_m < 50:
        x_to_internal = abs(x_in - 0.5) * MALLA_CELL_W
        y_to_internal = abs(y_in - 0.5) * MALLA_CELL_H
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
    sheets = pgou.get_sheet_listing()
    sheet_url = sheets.get(name)
    if not sheet_url:
        raise RCError(
            f"No se encontró hoja '{name}' en el listado del Ayuntamiento."
        )

    rx, ry = utm_to_body_relative(X, Y, col, row_idx, compass)

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
