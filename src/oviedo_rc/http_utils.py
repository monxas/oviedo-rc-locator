"""HTTP helpers: GET con retry, validación de Content-Type y magic bytes."""
import time
import requests
from requests.adapters import HTTPAdapter

from .config import HTTP_HEADERS, HTTP_TIMEOUT, HTTP_RETRIES

_SESSION = requests.Session()
_adapter = HTTPAdapter(pool_connections=20, pool_maxsize=20)
_SESSION.mount("http://", _adapter)
_SESSION.mount("https://", _adapter)

MAGIC_BYTES = {
    "application/pdf": b"%PDF-",
    "image/png": b"\x89PNG\r\n\x1a\n",
}


def _check_content_type(ct, expected):
    return ct.split(";")[0].strip().lower() == expected.lower()


def _check_magic(data, expected_type):
    magic = MAGIC_BYTES.get(expected_type)
    return magic is None or data[: len(magic)] == magic


def http_get(url, *, headers=None, timeout=HTTP_TIMEOUT, retries=HTTP_RETRIES,
             expected_type=None):
    """GET con retry exponencial. Si expected_type, valida Content-Type y
    magic bytes del payload."""
    last = None
    for attempt in range(retries):
        try:
            r = _SESSION.get(url, headers=headers or HTTP_HEADERS, timeout=timeout)
            r.raise_for_status()
            if expected_type:
                ct = r.headers.get("Content-Type", "")
                if not _check_content_type(ct, expected_type):
                    raise RuntimeError(
                        f"Content-Type inesperado para {url}: '{ct}'"
                    )
                if not _check_magic(r.content, expected_type):
                    raise RuntimeError(
                        f"Magic bytes incorrectos para {url}"
                    )
            return r
        except (requests.RequestException, RuntimeError) as e:
            last = e
            if attempt < retries - 1:
                time.sleep(1.5 ** attempt)
    raise RuntimeError(f"GET {url} falló tras {retries} intentos: {last}")


def fetch(url, dest, *, headers=None, expected_type=None):
    """Descarga url -> dest si no existe ya en caché. Reusa cache solo si los
    magic bytes son del tipo correcto (evita reusar HTML de error cacheado)."""
    if dest.exists() and dest.stat().st_size > 100:
        if expected_type is None or _check_magic(dest.read_bytes()[:16], expected_type):
            return dest
        dest.unlink()
    r = http_get(url, headers=headers, timeout=60, expected_type=expected_type)
    dest.write_bytes(r.content)
    return dest
