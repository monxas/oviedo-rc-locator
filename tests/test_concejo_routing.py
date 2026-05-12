"""Tests para `get_concejo_for_utm`: routing por bbox y override por id_ine.

Cubre el caso de bboxes solapados Oviedo/Gijón (~13.9 km² en X[270921, 277708]
× Y[4813038, 4815000]). Sin `id_ine`, gana el primero registrado (OVIEDO);
con `id_ine`, se respeta el argumento.
"""
import pytest

from oviedo_rc.concejo import (
    GIJON,
    OVIEDO,
    get_concejo_for_utm,
)


def test_oviedo_centro_sin_id_ine():
    # Punto claramente dentro de OVIEDO bbox, fuera de GIJON
    c = get_concejo_for_utm(264600.0, 4805400.0)
    assert c is not None
    assert c.id_ine == OVIEDO.id_ine


def test_gijon_centro_sin_id_ine():
    # Punto claramente dentro de GIJON bbox, fuera de OVIEDO
    c = get_concejo_for_utm(284900.0, 4824800.0)
    assert c is not None
    assert c.id_ine == GIJON.id_ine


def test_bbox_solapado_sin_id_ine_devuelve_oviedo_legacy():
    # X[270921, 277708] × Y[4813038, 4815000] solapan ambos bboxes.
    # Comportamiento legacy: primer match en orden REGISTRY = OVIEDO.
    c = get_concejo_for_utm(274000.0, 4814000.0)
    assert c is not None
    assert c.id_ine == OVIEDO.id_ine


def test_bbox_solapado_con_id_ine_gijon():
    # Mismo punto del solape, ahora forzado a GIJON por id_ine.
    c = get_concejo_for_utm(274000.0, 4814000.0, id_ine=GIJON.id_ine)
    assert c is not None
    assert c.id_ine == GIJON.id_ine


def test_id_ine_no_registrado_devuelve_none():
    # Si pasamos un id_ine no presente en REGISTRY, devolvemos None
    # (no fallback al bbox).
    c = get_concejo_for_utm(264600.0, 4805400.0, id_ine=99999)
    assert c is None


def test_fuera_de_todos_los_concejos_devuelve_none():
    # Madrid: coords no dentro de ningún concejo asturiano.
    c = get_concejo_for_utm(440000.0, 4475000.0)
    assert c is None
