"""Validación de formato de RC (offline, sin red)."""
import pytest
from oviedo_rc.geom import validate_rc
from oviedo_rc.errors import RCError


@pytest.mark.parametrize("rc", [
    "8653019TP6085S0002QW",
    "9254317TP6095S0001ZT",
    "9651017TP6095S",  # 14 chars sin sufijo de inmueble
])
def test_valid_rc_returns_14_chars(rc):
    out = validate_rc(rc)
    assert len(out) == 14


@pytest.mark.parametrize("rc", [
    "",
    "ABC",
    "no-es-una-rc",
    "8653019TP6085S00",
    "12345",
    "8653019TP6085S0002Q",  # 19 chars
])
def test_invalid_rc_raises(rc):
    with pytest.raises(RCError):
        validate_rc(rc)


def test_validate_rejects_non_string():
    with pytest.raises(RCError):
        validate_rc(12345)
