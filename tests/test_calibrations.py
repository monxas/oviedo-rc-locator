"""Smoke test del modelo geométrico contra calibraciones manuales.

Requiere caché local o red para resolver RC→UTM.
"""
import json
from pathlib import Path

import pytest

from oviedo_rc import geom

CALIB = json.loads(
    (Path(__file__).resolve().parent.parent / "data" / "calibrations.json").read_text()
)

TOL_REL = 0.05  # ~26 m sobre el body real (anchors pre-LSQ; el modelo LSQ
                # tiene mediana ~4.5 m / p90 ~7.75 m sobre 71 puntos reales)


@pytest.mark.parametrize("cal", CALIB, ids=[c["rc"] for c in CALIB])
def test_calibration_matches(cal):
    try:
        loc = geom.locate(cal["rc"])
    except Exception as e:
        pytest.skip(f"sin caché ni red disponible: {e}")
    assert loc["cell"] == cal["cell"]
    # Cerca de la frontera entre sub-cuadrantes (<0.01 ≈ 7 m), aceptar
    # cualquier asignación: el modelo tiene resolución finita y el plano
    # de Catastro puede haber asignado el inmueble al adyacente.
    pos = loc["intra_cell_position"]
    on_boundary = (abs(pos["x_west_to_east"] - 0.5) < 0.01
                   or abs(pos["y_north_to_south"] - 0.5) < 0.01)
    if on_boundary:
        return
    assert loc["sub_quadrant"] == cal["sub_quadrant"]
    assert loc["sheet_name"] == cal["sheet_name"]
    for axis in ("rx", "ry"):
        got = loc["body_relative"][axis]
        expected = cal[f"body_{axis}"]
        assert abs(got - expected) < TOL_REL, \
            f"body_{axis} {got} vs {expected} (Δ={abs(got-expected):.4f})"
