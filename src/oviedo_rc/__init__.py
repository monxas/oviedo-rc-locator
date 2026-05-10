"""oviedo_rc — pipeline production-ready: RC catastral → bundle completo.

API pública:

    from oviedo_rc import process_rc, RCBundle, OviedoError, locate

    bundle = process_rc("9651017TP6095S0001IT", output_dir="data/")
    print(bundle.plano["sheet_name"])  # "PLANO_15_K_IV.pdf"
    print(bundle.parcel["area_m2"])    # 425.0
    for u in bundle.units: print(u["use"], u["area_m2"], "m²")

CLI:

    python -m oviedo_rc <RC> --out-dir data/
    oviedo-rc <RC>            # tras instalar con pip
"""
from .errors import OviedoError, RCError
from .geom import locate, validate_rc
from .pipeline import process_rc, RCBundle

__version__ = "1.0.0"

__all__ = [
    "process_rc", "RCBundle",
    "locate", "validate_rc",
    "OviedoError", "RCError",
    "__version__",
]
