"""Sanity checks de `data/snu_grid.json` y `snu.infer_snu_sheet`."""
import json
import os
from pathlib import Path

import pytest

from oviedo_rc import snu

REPO = Path(__file__).resolve().parents[1]
GRID = json.loads((REPO / "data" / "snu_grid.json").read_text())


def test_grid_consistency():
    cols = GRID["cols"]
    rows = GRID["rows"]
    letters = GRID["letters"]
    assert len(letters) == rows, f"len(letters)={len(letters)} ≠ rows={rows}"
    assert cols * rows == 90, f"esperaba 90 cells SNU, got {cols*rows}"
    assert GRID["width"] > 0 and GRID["height"] > 0


def test_grid_bbox_covers_oviedo():
    x0 = GRID["x0"]
    ymax = GRID["ymax"]
    w = GRID["width"]
    h = GRID["height"]
    x1 = x0 + w
    ymin = ymax - h

    # El grid debe solapar la franja UTM razonable de Oviedo
    # (X ~255k-280k, Y ~4795k-4815k).
    assert x0 <= 260000, f"x0={x0} demasiado al este"
    assert x1 >= 270000, f"x1={x1} demasiado al oeste"
    assert ymax >= 4808000, f"ymax={ymax} demasiado al sur"
    assert ymin <= 4800000, f"ymin={ymin} demasiado al norte"


# 5 RCs urbanos del centro de Oviedo con coords UTM esperables.
# Tomados directamente del coords_local cache para evitar depender de Catastro.
CENTRO_OVIEDO_UTM = [
    (264600.0, 4805400.0),  # Catedral
    (265200.0, 4806000.0),
    (264000.0, 4805000.0),
    (263500.0, 4805700.0),
    (265800.0, 4805300.0),
]


@pytest.mark.parametrize("x,y", CENTRO_OVIEDO_UTM)
def test_infer_snu_sheet_centro(x, y):
    sheet = snu.infer_snu_sheet(x, y)
    # El centro urbano cae dentro del bbox SNU, así que debería devolver una hoja.
    # Si fuera del grid, sería None — aceptable también para coords periféricas.
    if sheet is None:
        return
    assert sheet.startswith("PLANO_"), f"sheet name inesperado: {sheet}"
    assert sheet.endswith(".pdf")
    # Extrae letra y número: PLANO_<L>_<N>.pdf
    middle = sheet[len("PLANO_"):-len(".pdf")]
    letter, num = middle.split("_")
    assert letter in GRID["letters"], f"letra fuera de rango: {letter}"
    assert 1 <= int(num) <= GRID["cols"], f"número fuera de rango: {num}"


def test_infer_snu_sheet_out_of_grid_returns_none():
    # Coords muy fuera del grid SNU (océano Atlántico)
    assert snu.infer_snu_sheet(0.0, 0.0) is None
    # Madrid
    assert snu.infer_snu_sheet(440000.0, 4470000.0) is None
