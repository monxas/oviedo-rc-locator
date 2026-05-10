"""CLI: `python -m oviedo_rc <RC> [--out DIR] [--no-snap] [--no-wms] [--json]`."""
import argparse
import json
import sys

from . import __version__
from .errors import OviedoError
from .pipeline import process_rc
from .geom import locate


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="oviedo_rc",
        description="Resolver RC catastral de Oviedo a plano PGOU + datos.",
    )
    p.add_argument("rc", help="Referencia catastral (14 o 20 caracteres)")
    p.add_argument("--out", help="Directorio de salida (default: bundles/<RC>/)")
    p.add_argument("--no-snap", action="store_true",
                   help="No alinear polígono al plano por cross-correlation")
    p.add_argument("--no-wms", action="store_true",
                   help="No generar PNG del WMS catastral")
    p.add_argument("--locate-only", action="store_true",
                   help="Solo localizar (sin descargar PDF/WMS), imprimir JSON")
    p.add_argument("--json", action="store_true",
                   help="Imprimir el bundle como JSON")
    p.add_argument("--version", action="version", version=f"oviedo_rc {__version__}")
    args = p.parse_args(argv)

    try:
        if args.locate_only:
            print(json.dumps(locate(args.rc), ensure_ascii=False, indent=2,
                             default=list))
            return 0
        bundle = process_rc(
            args.rc,
            output_dir=args.out,
            snap_polygon=not args.no_snap,
            draw_wms=not args.no_wms,
        )
        if args.json:
            print(json.dumps(bundle.to_dict(), ensure_ascii=False, indent=2))
        else:
            print(f"OK  {bundle.rc}")
            print(f"  dir:    {bundle.bundle_dir}")
            print(f"  sheet:  {bundle.sheet_name}")
            print(f"  addr:   {bundle.address}")
            print(f"  zoom:   {bundle.plan_zoom_png}")
        return 0
    except OviedoError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
