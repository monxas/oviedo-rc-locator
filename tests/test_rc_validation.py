"""Fuzz del regex de RC contra el endpoint /info en prod.

Comprueba que el endpoint público:
  - Acepta RCs válidos (14 o 20 chars) → 200.
  - Acepta minúsculas (se normalizan vía .upper()) → 200.
  - Rechaza con 422 longitudes y caracteres fuera del regex.
  - No traga inputs maliciosos (path traversal, SQL).

Marcado `integration` para que `pytest -m 'not integration'` lo salte.
Requiere `OVIEDO_LOCATOR_TOKEN` en el entorno.
"""
import os

import pytest

requests = pytest.importorskip("requests")


BASE = os.environ.get("OVIEDO_LOCATOR_URL", "https://locator.iarquitectos.com")
TOKEN = os.environ.get("OVIEDO_LOCATOR_TOKEN")


@pytest.fixture(scope="module")
def session():
    if not TOKEN:
        pytest.skip("OVIEDO_LOCATOR_TOKEN no definido")
    s = requests.Session()
    s.headers.update({"Authorization": f"Bearer {TOKEN}"})
    return s


@pytest.mark.integration
@pytest.mark.parametrize("rc, expected_status", [
    # válidos (200)
    ("9651017TP6095S0001IT", 200),
    ("33900A10600069", 200),
    ("7960006TP6075N", 200),
    # válido tras normalizar a uppercase
    ("9651017tp6095s0001it", 200),
    # inválidos por longitud (422)
    ("AAAA", 422),
    ("A" * 13, 422),
    ("A" * 15, 422),
    ("A" * 21, 422),
    # inválidos por caracteres / símbolos (422)
    ("9651017TP6095S 0001IT", 422),
    ("9651017TP6095S0001I!", 422),
    # ataques (422 — el regex no permite '/', "'", espacios)
    ("../../etc/passwd", 422),
    ("'; DROP TABLE", 422),
])
def test_rc_validation_via_info(session, rc, expected_status):
    # El espacio en el path debe URL-encodearse correctamente por requests.
    resp = session.get(f"{BASE}/info/{rc}", timeout=30, allow_redirects=False)
    # Algunos clouds reescriben 422→400; aceptamos cualquiera 4xx con coherencia.
    if expected_status == 422:
        assert 400 <= resp.status_code < 500, (
            f"RC {rc!r}: esperaba rechazo 4xx, got {resp.status_code}: {resp.text[:200]}"
        )
    else:
        assert resp.status_code == expected_status, (
            f"RC {rc!r}: esperaba {expected_status}, got {resp.status_code}: {resp.text[:200]}"
        )
