"""Path setup compartido para scripts de este experimento.

Cada script empieza con:
    from _setup import setup_paths
    setup_paths()
y a partir de ahí puede `from oviedo_rc import ...` y `import body_detect`.
"""
import sys
from pathlib import Path


def setup_paths():
    """Pone src/ y este directorio en sys.path."""
    here = Path(__file__).resolve().parent
    repo_root = here.parents[1]
    src = repo_root / "src"
    for p in (str(src), str(here)):
        if p not in sys.path:
            sys.path.insert(0, p)
    return repo_root


def repo_root():
    return Path(__file__).resolve().parents[2]
