#!/usr/bin/env python3
"""Pre-fetch de todos los datos finitos para uso offline.

Uso:
    python3 prefetch.py status     Muestra estado del caché
    python3 prefetch.py plans      Descarga los 153 PDFs del PGOU (~250 MB)
    python3 prefetch.py parcels    Descarga polígonos catastrales (zona urbana)
    python3 prefetch.py dnprc      Descarga contenido catastral (lento, cientos de calls)
    python3 prefetch.py all        Todo lo anterior

Tras ejecutar `prefetch all`, `oviedo_rc` y `parcel_info` funcionan SIN red para
RCs en la zona urbana de Oviedo.
"""
import argparse, json, os, sys, time, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from _compat import L, P

# Bbox urbano EFECTIVO: zona donde están concentradas las parcelas del PGOU.
# Más estrecho que el municipio entero (que incluye montes/parroquias rurales).
# Cubre las cells centrales (~10 km × 6 km).
URBAN_BBOX = (
    260000,  # xmin (~col 6)
    4801000, # ymin (~row Q)
    275000,  # xmax (~col 21)
    4810000, # ymax (~row D)
)
# WFS por chunks de 500m × 500m (devuelve <100 parcelas por llamada)
WFS_CHUNK_M = 500


# ============================================================ STATUS

def cmd_status(args):
    """Reporta el estado del caché en disco."""
    cache = L.CACHE_DIR
    print(f"Caché: {cache}")
    if not cache.exists():
        print("  (vacío)")
        return

    # PDFs del PGOU
    sheets_json = cache / "sheets.json"
    if sheets_json.exists():
        sheets = json.loads(sheets_json.read_text())
        local = [n for n in sheets if (cache / n).exists()]
        size_mb = sum((cache / n).stat().st_size for n in local) / 1e6
        print(f"  Planos PGOU: {len(local)}/{len(sheets)} ({size_mb:.0f} MB)")
    else:
        print("  Planos PGOU: listado no descargado")

    # Polígonos
    parcels_dir = cache / "parcels"
    polys = list(parcels_dir.glob("poly_*.json")) if parcels_dir.exists() else []
    bboxes = list(parcels_dir.glob("bbox_*.json")) if parcels_dir.exists() else []
    dnprc = list(parcels_dir.glob("dnprc_*.json")) if parcels_dir.exists() else []
    pol_mb = sum(p.stat().st_size for p in polys) / 1e6
    bbox_mb = sum(p.stat().st_size for p in bboxes) / 1e6
    dnprc_mb = sum(p.stat().st_size for p in dnprc) / 1e6
    print(f"  Polígonos parcela: {len(polys)} ({pol_mb:.1f} MB)")
    print(f"  Listas WFS bbox:   {len(bboxes)} ({bbox_mb:.1f} MB)")
    print(f"  Contenidos DNPRC:  {len(dnprc)} ({dnprc_mb:.1f} MB)")

    # Polígono municipal
    poly_npz = cache / "oviedo_polygon_utm.npz"
    if poly_npz.exists():
        print(f"  Polígono municipal: ✓ ({poly_npz.stat().st_size/1e3:.1f} KB)")

    total = sum(p.stat().st_size for p in cache.rglob("*") if p.is_file()) / 1e6
    print(f"\nTOTAL: {total:.0f} MB")


# ============================================================ PLANS

def cmd_plans(args):
    """Descarga los 153 PDFs del PGOU."""
    sheets = L.get_sheet_listing()
    print(f"Listado: {len(sheets)} planos")
    todo = [(name, url) for name, url in sheets.items()
            if not (L.CACHE_DIR / name).exists()
            or (L.CACHE_DIR / name).stat().st_size < 1000]
    print(f"Por descargar: {len(todo)}")
    if not todo:
        return

    def fetch(item):
        name, url = item
        dest = L.CACHE_DIR / name
        try:
            L.fetch(url, dest, expected_type="application/pdf")
            return name, dest.stat().st_size, None
        except Exception as e:
            return name, 0, str(e)

    done = 0; total_bytes = 0; errors = []
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(fetch, it): it for it in todo}
        for fut in as_completed(futs):
            name, size, err = fut.result()
            done += 1
            if err:
                errors.append((name, err))
            else:
                total_bytes += size
            if done % 10 == 0 or done == len(todo):
                rate = done / (time.time() - t0)
                eta = (len(todo) - done) / max(0.1, rate)
                print(f"  {done}/{len(todo)} · {total_bytes/1e6:.0f} MB · "
                      f"{rate:.1f}/s · ETA {eta:.0f}s")
    if errors:
        print(f"\n{len(errors)} errores:")
        for n, e in errors[:5]: print(f"  {n}: {e}")


# ============================================================ PARCELS (polygons)

def cmd_parcels(args):
    """Descarga polígonos catastrales del bbox urbano por chunks."""
    xmin, ymin, xmax, ymax = URBAN_BBOX
    chunks = []
    y = ymin
    while y < ymax:
        x = xmin
        while x < xmax:
            chunks.append((x, y, min(x + WFS_CHUNK_M, xmax),
                           min(y + WFS_CHUNK_M, ymax)))
            x += WFS_CHUNK_M
        y += WFS_CHUNK_M
    print(f"Bbox urbano {(xmax-xmin)/1000:.0f}×{(ymax-ymin)/1000:.0f} km, "
          f"{len(chunks)} chunks de {WFS_CHUNK_M} m")

    def fetch_chunk(c):
        try:
            ps = P.wfs_parcels_bbox(*c)
            return c, len(ps), None
        except Exception as e:
            return c, 0, str(e)

    total_p = 0; done = 0; errs = []
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(fetch_chunk, c): c for c in chunks}
        for fut in as_completed(futs):
            c, n, err = fut.result()
            done += 1
            if err: errs.append((c, err))
            else: total_p += n
            if done % 50 == 0 or done == len(chunks):
                rate = done / (time.time() - t0)
                eta = (len(chunks) - done) / max(0.1, rate)
                print(f"  chunks {done}/{len(chunks)} · {total_p} parcelas · "
                      f"{rate:.1f}/s · ETA {eta:.0f}s")
    if errs:
        print(f"{len(errs)} errores")

    # Bajar polígono individual de cada parcela (los que faltan)
    if args.with_polygons:
        all_refcats = set()
        for jf in (P.CACHE_DIR.glob("bbox_*.json")):
            for p in json.loads(jf.read_text()):
                all_refcats.add(p["refcat"])
        existing = {f.stem.replace("poly_","") for f in P.CACHE_DIR.glob("poly_*.json")}
        todo = list(all_refcats - existing)
        print(f"Polígonos individuales: {len(todo)} pendientes")
        # Ya están en el bbox cache, no hace falta volver a pedir individuales si
        # parcel_layer puede usar los del bbox. Skipping unless explicitly required.


# ============================================================ DNPRC

def cmd_dnprc(args):
    """Descarga contenido catastral de todas las parcelas conocidas."""
    all_refcats = set()
    for jf in P.CACHE_DIR.glob("bbox_*.json"):
        try:
            for p in json.loads(jf.read_text()):
                all_refcats.add(p["refcat"])
        except Exception:
            continue
    if not all_refcats:
        print("No hay parcelas en caché. Ejecuta primero: prefetch.py parcels")
        return

    existing = {f.stem.replace("dnprc_","") for f in P.CACHE_DIR.glob("dnprc_*.json")}
    todo = [r for r in all_refcats if r not in existing]
    print(f"DNPRC: {len(todo)}/{len(all_refcats)} pendientes")
    if not todo: return
    if args.limit:
        todo = todo[:args.limit]
        print(f"Limitando a {len(todo)} (--limit)")

    def fetch(refcat):
        try:
            P.consulta_dnprc(refcat)
            return refcat, None
        except Exception as e:
            return refcat, str(e)

    done = 0; errs = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = [ex.submit(fetch, r) for r in todo]
        for fut in as_completed(futs):
            r, err = fut.result()
            done += 1
            if err: errs += 1
            if done % 50 == 0 or done == len(todo):
                rate = done / (time.time() - t0)
                eta = (len(todo) - done) / max(0.1, rate)
                print(f"  dnprc {done}/{len(todo)} · {errs} errs · "
                      f"{rate:.1f}/s · ETA {eta:.0f}s")


# ============================================================ VERIFY

def cmd_verify(args):
    """Verifica integridad de los PDFs descargados (magic bytes %PDF-)."""
    sheets = L.get_sheet_listing()
    bad = []
    for name in sheets:
        path = L.CACHE_DIR / name
        if not path.exists():
            bad.append((name, "missing"))
            continue
        try:
            head = path.read_bytes()[:5]
            if head != b"%PDF-":
                bad.append((name, f"bad header {head!r}"))
            elif path.stat().st_size < 10000:
                bad.append((name, f"too small ({path.stat().st_size} bytes)"))
        except Exception as e:
            bad.append((name, str(e)))
    print(f"PDFs verificados: {len(sheets) - len(bad)}/{len(sheets)} OK")
    if bad:
        print(f"\n{len(bad)} con problemas:")
        for n, e in bad[:20]: print(f"  {n}: {e}")
        if args.fix:
            print(f"\nRe-descargando {len(bad)}...")
            for n, _ in bad:
                path = L.CACHE_DIR / n
                if path.exists(): path.unlink()
            cmd_plans(args)


# ============================================================ REFRESH

def cmd_refresh(args):
    """Invalida caché y vuelve a descargar todo. Para tras un cambio del Ayto."""
    targets = args.what or ["sheets", "plans"]
    if "sheets" in targets:
        sj = L.CACHE_DIR / "sheets.json"
        if sj.exists():
            sj.unlink()
            print("× borrado sheets.json")
        L.get_sheet_listing()  # re-descarga
        print("✓ listado actualizado")

    if "plans" in targets:
        if args.older_than_days:
            cutoff = time.time() - args.older_than_days*86400
            old = [p for p in L.CACHE_DIR.glob("PLANO_*.pdf")
                   if p.stat().st_mtime < cutoff]
            print(f"Borrando {len(old)} planos > {args.older_than_days} días")
            for p in old: p.unlink()
        else:
            old = list(L.CACHE_DIR.glob("PLANO_*.pdf"))
            print(f"Borrando {len(old)} planos")
            for p in old: p.unlink()
        cmd_plans(args)

    if "parcels" in targets:
        d = P.CACHE_DIR
        for f in list(d.glob("bbox_*.json")) + list(d.glob("poly_*.json")):
            f.unlink()
        print("× borrado WFS de parcelas")
        cmd_parcels(args)

    if "dnprc" in targets:
        for f in P.CACHE_DIR.glob("dnprc_*.json"):
            f.unlink()
        print("× borrado contenido DNPRC")
        cmd_dnprc(args)


# ============================================================ GC

def cmd_gc(args):
    """Garbage collection: borra cosas que no aporten."""
    n = 0
    for f in L.CACHE_DIR.rglob("*"):
        if not f.is_file(): continue
        # Archivos vacíos o casi vacíos
        if f.stat().st_size < 50 and f.suffix == ".json":
            f.unlink(); n += 1
    print(f"Eliminados {n} archivos vacíos/inválidos")


# ============================================================ ALL

def cmd_all(args):
    cmd_status(args); print()
    cmd_plans(args); print()
    cmd_parcels(args); print()
    cmd_dnprc(args); print()
    cmd_verify(args); print()
    print("--- final ---")
    cmd_status(args)


# ============================================================ CLI

def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("status", help="Estado del caché")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("plans", help="Descargar 153 PDFs del PGOU")
    sp.add_argument("--workers", type=int, default=6)
    sp.set_defaults(func=cmd_plans)

    sp = sub.add_parser("parcels", help="Descargar polígonos por chunks WFS")
    sp.add_argument("--workers", type=int, default=4)
    sp.add_argument("--with-polygons", action="store_true",
                    help="También descargar polígono individual de cada parcela")
    sp.set_defaults(func=cmd_parcels)

    sp = sub.add_parser("dnprc", help="Descargar contenido catastral por parcela")
    sp.add_argument("--workers", type=int, default=4)
    sp.add_argument("--limit", type=int, help="Limitar número de descargas")
    sp.set_defaults(func=cmd_dnprc)

    sp = sub.add_parser("verify", help="Verificar integridad de PDFs")
    sp.add_argument("--fix", action="store_true",
                    help="Re-descargar los corruptos/faltantes")
    sp.add_argument("--workers", type=int, default=6)
    sp.set_defaults(func=cmd_verify)

    sp = sub.add_parser("refresh", help="Invalidar caché y volver a descargar")
    sp.add_argument("what", nargs="*",
                    choices=["sheets", "plans", "parcels", "dnprc"],
                    help="Qué refrescar (default: sheets+plans)")
    sp.add_argument("--older-than-days", type=int,
                    help="Solo refrescar archivos más viejos que N días")
    sp.add_argument("--workers", type=int, default=6)
    sp.add_argument("--with-polygons", action="store_true")
    sp.add_argument("--limit", type=int)
    sp.set_defaults(func=cmd_refresh)

    sp = sub.add_parser("gc", help="Limpiar archivos inválidos del caché")
    sp.set_defaults(func=cmd_gc)

    sp = sub.add_parser("all", help="Todo (status + plans + parcels + dnprc + verify)")
    sp.add_argument("--workers", type=int, default=4)
    sp.add_argument("--with-polygons", action="store_true")
    sp.add_argument("--limit", type=int)
    sp.add_argument("--fix", action="store_true")
    sp.set_defaults(func=cmd_all)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
