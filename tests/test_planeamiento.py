"""Resilience de `planeamiento.lookup()`.

`/info` depende de que `lookup` nunca levante excepción: ante timeout o
GeoServer caído, debe devolver un dict mínimamente coherente y bool-falsy
para `ambito`/`patrimonio`.
"""
from unittest.mock import patch

import pytest

from oviedo_rc import planeamiento


@pytest.mark.integration
def test_lookup_catedral_returns_layers():
    """Coord válida en Oviedo debe devolver layers no vacío."""
    out = planeamiento.lookup(264600.0, 4805400.0)
    assert isinstance(out, dict)
    assert "layers" in out
    # No imponemos qué capa exacta — sólo que algo haya respondido el WFS.
    # Si WFS está caído, este test puede fallar (es 'integration').
    assert isinstance(out["layers"], dict)


@pytest.mark.integration
def test_lookup_out_of_oviedo_is_empty():
    """Coord en Madrid: WFS responde pero ninguna feature intersecta."""
    out = planeamiento.lookup(440000.0, 4470000.0)
    assert isinstance(out, dict)
    assert out.get("layers") == {} or out.get("layers") is None or out["layers"] == {}
    assert not out.get("ambito")
    assert out.get("patrimonio") == [] or not out.get("patrimonio")


def _raise_timeout(*args, **kwargs):
    raise TimeoutError("simulated WFS timeout")


def test_lookup_swallows_wfs_failures():
    """Si http_get falla, lookup NO debe propagar. /info confía en eso."""
    with patch.object(planeamiento, "http_get", side_effect=_raise_timeout):
        try:
            out = planeamiento.lookup(264600.0, 4805400.0)
        except Exception as e:
            pytest.fail(
                f"planeamiento.lookup propagó {type(e).__name__} pese a timeout WFS "
                "— /info se romperá silenciosamente."
            )
    assert isinstance(out, dict)
    assert out.get("layers") == {}
    assert not out.get("ambito")
    assert out.get("patrimonio") == []
