#!/usr/bin/env python3
"""Pre-fetch del WMS catastral en mosaico local.

Descarga el bbox urbano de Oviedo en tiles a 0.5 m/px (40 tiles, ~100-200 MB)
y los guarda en `~/.cache/oviedo_rc/wms/`. Después, render local de cualquier
zona se hace recortando del mosaico sin tocar la red.

Uso:
    python3 prefetch_wms.py status      Ver estado del mosaico
    python3 prefetch_wms.py fetch       Descargar tiles que faltan
    python3 prefetch_wms.py fetch --resolution 1.0   Resolución alternativa
    python3 prefetch_wms.py verify      Verificar integridad
"""
import argparse, sys, time, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from _compat import L

CACHE = L.CACHE_DIR / "wms"
CACHE.mkdir(parents=True, exist_ok=True)

# Bbox urbano de Oviedo (igual que en prefetch.py)
URBAN_BBOX = (260000, 4801000, 275000, 4810000)  # 15 km × 9 km

# Tile size: WMS catastral acepta hasta ~4000×4000. Usamos 2000×2000 para más
# resilencia y mejor granularidad de caché.
TILE_SIZE_PX = 2000
HEADERS = {"User-Agent": "Mozilla/5.0 (oviedo_rc-wms)"}


def tile_filename(xmin, ymin, xmax, ymax, mpp):
    return CACHE / f"wms_{int(xmin)}_{int(ymin)}_{int(xmax)}_{int(ymax)}_{mpp:.2f}.png"


def gen_tiles(bbox, mpp):
    """Genera la lista de tiles necesarios para cubrir bbox a m/px dado."""
    xmin, ymin, xmax, ymax = bbox
    tile_m = TILE_SIZE_PX * mpp  # m que cubre cada tile
    tiles = []
    y = ymin
    while y < ymax:
        x = xmin
        while x < xmax:
            tx_max = min(x + tile_m, xmax)
            ty_max = min(y + tile_m, ymax)
            tiles.append((x, y, tx_max, ty_max))
            x += tile_m
        y += tile_m
    return tiles


def fetch_one(xmin, ymin, xmax, ymax, mpp):
    """Descarga un tile WMS y lo guarda. Devuelve (path, status)."""
    out = tile_filename(xmin, ymin, xmax, ymax, mpp)
    if out.exists() and out.stat().st_size > 1000:
        return out, "skip"
    w = int((xmax - xmin) / mpp)
    h = int((ymax - ymin) / mpp)
    url = ("https://ovc.catastro.meh.es/Cartografia/WMS/ServidorWMS.aspx?"
           "SERVICE=WMS&REQUEST=GetMap&VERSION=1.1.1&LAYERS=Catastro&"
           f"SRS=EPSG:25830&BBOX={xmin},{ymin},{xmax},{ymax}"
           f"&WIDTH={w}&HEIGHT={h}&FORMAT=image/png&STYLES=")
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        data = urllib.request.urlopen(req, timeout=60).read()
        if len(data) < 500:
            return out, "small"
        if not data.startswith(b"\x89PNG"):
            return out, "non-png"
        out.write_bytes(data)
        return out, "ok"
    except Exception as e:
        return out, f"err:{e}"


def cmd_status(args):
    mpp = args.resolution
    tiles = gen_tiles(URBAN_BBOX, mpp)
    have = sum(1 for t in tiles if tile_filename(*t, mpp).exists()
               and tile_filename(*t, mpp).stat().st_size > 1000)
    size_mb = sum(tile_filename(*t, mpp).stat().st_size for t in tiles
                  if tile_filename(*t, mpp).exists()) / 1e6
    print(f"Resolución {mpp} m/px:")
    print(f"  Tiles esperados: {len(tiles)}  (cada uno {TILE_SIZE_PX}×{TILE_SIZE_PX} px)")
    print(f"  Descargados: {have}/{len(tiles)} ({size_mb:.0f} MB)")
    print(f"  Cache total mosaicos: "
          f"{sum(p.stat().st_size for p in CACHE.glob('*.png'))/1e6:.0f} MB")


def cmd_fetch(args):
    mpp = args.resolution
    tiles = gen_tiles(URBAN_BBOX, mpp)
    todo = [t for t in tiles
            if not tile_filename(*t, mpp).exists()
            or tile_filename(*t, mpp).stat().st_size < 1000]
    print(f"{len(todo)}/{len(tiles)} tiles a {mpp} m/px por descargar")
    if not todo:
        return

    done = ok = errs = skip = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(fetch_one, *t, mpp): t for t in todo}
        for fut in as_completed(futs):
            path, st = fut.result()
            done += 1
            if st == "ok": ok += 1
            elif st == "skip": skip += 1
            else:
                errs += 1
                print(f"  ERR {path.name}: {st}")
            if done % 5 == 0 or done == len(todo):
                rate = done / (time.time()-t0)
                eta = (len(todo)-done) / max(0.1, rate)
                size = sum(p.stat().st_size for p in CACHE.glob('*.png')) / 1e6
                print(f"  {done}/{len(todo)}  ok={ok} err={errs}  "
                      f"{rate:.1f}/s  ETA {eta:.0f}s  cache {size:.0f}MB")


def cmd_verify(args):
    mpp = args.resolution
    tiles = gen_tiles(URBAN_BBOX, mpp)
    bad = []
    for t in tiles:
        p = tile_filename(*t, mpp)
        if not p.exists():
            bad.append((p.name, "missing"))
            continue
        head = p.read_bytes()[:4]
        if head != b"\x89PNG":
            bad.append((p.name, f"bad header {head!r}"))
        elif p.stat().st_size < 1000:
            bad.append((p.name, "too small"))
    print(f"Verificados: {len(tiles) - len(bad)}/{len(tiles)} OK")
    if bad and args.fix:
        print(f"Re-descargando {len(bad)}...")
        for n, _ in bad:
            (CACHE / n).unlink(missing_ok=True)
        cmd_fetch(args)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in [("status", cmd_status), ("fetch", cmd_fetch), ("verify", cmd_verify)]:
        sp = sub.add_parser(name)
        sp.add_argument("--resolution", type=float, default=0.5,
                        help="Metros/pixel (default 0.5)")
        if name == "fetch":
            sp.add_argument("--workers", type=int, default=4)
        if name == "verify":
            sp.add_argument("--fix", action="store_true")
            sp.add_argument("--workers", type=int, default=4)
        sp.set_defaults(func=fn)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
