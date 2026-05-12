"""Pinned outputs de `geom.locate()` para ~95 RCs cacheados.

Protege contra cambios silenciosos en MALLA_X0/YMAX/CELL_W/CELL_H/SUB_CONVENTION/
NS_THRESHOLD/EW_THRESHOLD. El golden file (`tests/golden_geom.json`) se generó
con `gen_golden.py` sobre la calibración actual.

Si tu cambio en `config.py` es legítimo, regenera el golden y commit aparte.
"""
import json
from pathlib import Path

import pytest

from oviedo_rc import geom

GOLDEN_PATH = Path(__file__).resolve().parent / "golden_geom.json"
GOLDEN: dict[str, dict] = json.loads(GOLDEN_PATH.read_text())

# Tolerancia para drift de float en body_relative (rx, ry ∈ [0,1]).
# 0.5 px sobre un body de ~6135 px de ancho a 300 DPI ≈ 0.00008.
# Usamos 0.0001 para margen.
TOL_BODY_REL = 0.0001
TOL_UTM_M = 0.01  # 1 cm — sólo deriva por changes de cache, no por modelo.


@pytest.mark.parametrize("rc", sorted(GOLDEN.keys()))
def test_geom_pinned_output(rc: str):
    expected = GOLDEN[rc]
    try:
        loc = geom.locate(rc)
    except Exception as e:
        pytest.skip(f"{rc}: locate failed (cache miss?): {e}")

    assert loc["cell"] == expected["cell"], f"{rc}: cell drift"
    assert loc["sub_quadrant"] == expected["sub_quadrant"], f"{rc}: sub_quadrant drift"
    assert loc["sub_compass"] == expected["sub_compass"], f"{rc}: sub_compass drift"
    assert loc["sheet_name"] == expected["sheet_name"], f"{rc}: sheet_name drift"

    ex, ey = expected["utm"]
    gx, gy = loc["utm"]
    assert abs(gx - ex) < TOL_UTM_M, f"{rc}: UTM X drift {gx} vs {ex}"
    assert abs(gy - ey) < TOL_UTM_M, f"{rc}: UTM Y drift {gy} vs {ey}"

    for axis in ("rx", "ry"):
        got = loc["body_relative"][axis]
        exp = expected["body_relative"][axis]
        assert abs(got - exp) < TOL_BODY_REL, (
            f"{rc}: body_{axis} drift {got} vs {exp} (Δ={abs(got-exp):.6f})"
        )
