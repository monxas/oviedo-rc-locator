"""Fichas de Ámbitos del PGOU: catálogo + lookup por código/nombre.

234 PDFs descargados en ~/.cache/oviedo_rc/fichas/. Mapeo RC→ámbito
pendiente (requiere polígonos UTM por ámbito o un dataset externo).

Por ahora ofrece búsqueda por código/nombre/número.

Multi-concejo (PR3): para no romper a `recalibrate.py` ni a usuarios
externos, OVIEDO mantiene el path legacy `~/.cache/oviedo_rc/fichas/`.
Concejos nuevos usarán `~/.cache/oviedo_rc/<slug>/fichas/`.
"""
import json
import re
from pathlib import Path

from .config import CACHE_DIR
from .concejo import OVIEDO, Concejo


def _fichas_dir_for(concejo: Concejo) -> Path:
    """Carpeta de fichas para el concejo. OVIEDO mantiene path legacy."""
    if concejo.id_ine == OVIEDO.id_ine:
        return CACHE_DIR / "fichas"
    return CACHE_DIR / concejo.slug / "fichas"


def _list_file_for(concejo: Concejo) -> Path:
    if concejo.id_ine == OVIEDO.id_ine:
        return CACHE_DIR / "fichas_listing.json"
    return CACHE_DIR / concejo.slug / "fichas_listing.json"


FICHAS_DIR = _fichas_dir_for(OVIEDO)
LIST_FILE = _list_file_for(OVIEDO)

# Patrón ampliado: TIPO puede llevar dígitos (UG1, UG2, UGE), CODIGO mezclado.
_META_RE = re.compile(
    r"_(UG2?[E]?|UG1|AU[SE]?|AA|PE|PP|SUNC|API|SR|AM\d?|ASM)"
    r"_([A-Z0-9]+(?:_\d+)?)_Ficha_n_(\d+)",
    re.IGNORECASE,
)


def _parse(filename: str) -> dict:
    m = _META_RE.search(filename)
    if not m:
        # Fallback: extrae sólo el número de ficha
        n = re.search(r"_Ficha_n_(\d+)", filename)
        return {"num": int(n.group(1))} if n else {}
    return {
        "tipo": m.group(1).upper(),
        "codigo": m.group(2).upper(),
        "num": int(m.group(3)),
    }


def load_listing(concejo: Concejo | None = None) -> dict[str, dict]:
    """Carga el JSON de listado (escrito por scripts/fetch_fichas.py)."""
    list_file = _list_file_for(concejo or OVIEDO)
    if not list_file.exists():
        return {}
    data = json.loads(list_file.read_text(encoding="utf-8"))
    # Reanaliza meta con regex extendido (no rompe entradas viejas)
    for fname, info in data.items():
        if "url" in info:
            info.update(_parse(fname))
    return data


def list_fichas(tipo: str | None = None,
                 concejo: Concejo | None = None) -> list[dict]:
    """Lista de fichas como [{filename, tipo, codigo, num}, ...]."""
    listing = load_listing(concejo)
    out = []
    for fname, info in sorted(listing.items()):
        if tipo and info.get("tipo") != tipo.upper():
            continue
        out.append({"filename": fname, **{k: v for k, v in info.items() if k != "url"}})
    return out


def find_ficha(query: str, concejo: Concejo | None = None) -> list[dict]:
    """Busca por código exacto, número, o substring del nombre."""
    listing = load_listing(concejo)
    q = query.strip().upper()
    hits = []
    # 1) Match exacto código
    for fname, info in listing.items():
        if info.get("codigo") == q:
            hits.append({"filename": fname, **{k: v for k, v in info.items() if k != "url"}})
    if hits:
        return hits
    # 2) Match por número
    if q.isdigit():
        n = int(q)
        for fname, info in listing.items():
            if info.get("num") == n:
                hits.append({"filename": fname, **{k: v for k, v in info.items() if k != "url"}})
        if hits:
            return hits
    # 3) Substring en nombre
    for fname, info in listing.items():
        if q in fname.upper():
            hits.append({"filename": fname, **{k: v for k, v in info.items() if k != "url"}})
    return hits


def get_ficha_path(filename: str, concejo: Concejo | None = None) -> Path | None:
    """Devuelve path del PDF si existe en cache."""
    base = _fichas_dir_for(concejo or OVIEDO)
    p = base / filename
    return p if p.exists() and p.stat().st_size > 1024 else None
