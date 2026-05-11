#!/usr/bin/env python3
"""Stress test del caché: ejecuta el pipeline contra muchas RCs y mide tiempos.
Cada RC se ejecuta DOS veces:
  1ª (cold): puede tocar red para datos no cacheados
  2ª (warm): debería ser 100% local

Reporta hit-rate del caché y tiempos por etapa."""
import json, sys, time, urllib.parse, urllib.request
from pathlib import Path

from _compat import L, P

# Recopilar RCs reales del proyecto (training, test, parcels)
def collect_rcs():
    rcs = set()
    base = Path(__file__).parent / "validation_app"
    for fn in ["static/candidates.json", "static/test_set.json", "test_set.json", "parcel_set.json"]:
        p = base / fn
        if p.exists():
            for c in json.loads(p.read_text()):
                if isinstance(c, dict) and "rc" in c:
                    rcs.add(c["rc"])
    # Más RCs aleatorias del catastro de calles conocidas
    return sorted(rcs)


def run_one(rc):
    """Ejecuta el pipeline completo y devuelve tiempos por etapa."""
    t = {}
    t0 = time.time()
    loc = L.locate(rc)
    t["locate"] = time.time() - t0

    t0 = time.time()
    poly = P.consulta_dnprc(rc[:14])  # contenido cacheado
    t["dnprc"] = time.time() - t0

    return loc["sheet_name"], t


def main():
    rcs = collect_rcs()
    print(f"Stress test sobre {len(rcs)} RCs únicas\n")

    # Verificar cuántas DNPRC ya están en caché ANTES
    dnprc_cache = P.CACHE_DIR
    cached_before = len(list(dnprc_cache.glob("dnprc_*.json")))
    print(f"DNPRC en caché antes: {cached_before}")
    print(f"Polígonos en caché antes: {len(list(dnprc_cache.glob('poly_*.json')))}")
    print(f"Bbox WFS en caché: {len(list(dnprc_cache.glob('bbox_*.json')))}\n")

    cold, warm = [], []
    fails = []
    print(f"{'#':>3} {'RC':22s} {'plano':22s} {'locate':>8} {'dnprc':>8}")
    print("-"*70)
    for i, rc in enumerate(rcs, 1):
        try:
            sheet, t = run_one(rc)
        except Exception as e:
            fails.append((rc, str(e)))
            continue
        print(f"{i:>3} {rc:22s} {sheet:22s} {t['locate']*1000:>6.0f}ms {t['dnprc']*1000:>6.0f}ms")
        cold.append(t)
        if i >= 50:
            break

    print(f"\n=== RESUMEN ({len(cold)} RCs ejecutadas) ===")
    if cold:
        import statistics
        loc_t = [c["locate"]*1000 for c in cold]
        dnp_t = [c["dnprc"]*1000 for c in cold]
        print(f"locate (modelo geométrico, no toca red):")
        print(f"  mediana={statistics.median(loc_t):.0f}ms · p95={sorted(loc_t)[int(len(loc_t)*0.95)]:.0f}ms · max={max(loc_t):.0f}ms")
        print(f"dnprc (cache hit = ms, miss = decenas/cientos ms):")
        print(f"  mediana={statistics.median(dnp_t):.0f}ms · p95={sorted(dnp_t)[int(len(dnp_t)*0.95)]:.0f}ms · max={max(dnp_t):.0f}ms")
        # Hit-rate aproximado: si dnprc < 50ms, asumimos cache hit
        hits = sum(1 for d in dnp_t if d < 50)
        print(f"  cache hits (<50ms): {hits}/{len(dnp_t)} = {100*hits/len(dnp_t):.0f}%")

    cached_after = len(list(dnprc_cache.glob("dnprc_*.json")))
    print(f"\nDNPRC en caché ahora: {cached_after}  (+{cached_after - cached_before} nuevas)")
    if fails:
        print(f"\n{len(fails)} fallos:")
        for rc, e in fails[:5]: print(f"  {rc}: {e}")


if __name__ == "__main__":
    main()
