#!/usr/bin/env python3
"""Construye un cache local rc14 → (X, Y, address) extrayendo:
  - Coordenadas: del centroide de cada polígono catastral en bbox WFS cache
  - Dirección: del primer inmueble en dnprc cache (o del referencePoint del WFS)

Tras ejecutarlo, locate_rc.rc_to_utm puede consultarlo y evitar la red para
cualquier RC ya cacheada.
"""
import json
from pathlib import Path
from _compat import L

CACHE_PARCELS = L.CACHE_DIR / "parcels"
COORDS_CACHE = L.CACHE_DIR / "coords_local.json"


def main():
    coords = {}
    n_from_bbox = 0
    n_from_dnprc = 0

    # 1. Extraer coords de los polígonos WFS (centroide)
    for jf in CACHE_PARCELS.glob("bbox_*.json"):
        try:
            for p in json.loads(jf.read_text()):
                refcat = p["refcat"]
                if refcat in coords: continue
                poly = p["poly_utm"]
                if not poly: continue
                xs = [pt[0] for pt in poly]
                ys = [pt[1] for pt in poly]
                cx = sum(xs)/len(xs); cy = sum(ys)/len(ys)
                coords[refcat] = {"x": cx, "y": cy, "address": ""}
                n_from_bbox += 1
        except Exception as e:
            print(f"Skip {jf.name}: {e}")

    # 2. Direcciones desde los DNPRC cache
    for jf in CACHE_PARCELS.glob("dnprc_*.json"):
        try:
            d = json.loads(jf.read_text())
            refcat = d.get("refcat14", jf.stem.replace("dnprc_",""))
            if refcat not in coords: continue
            units = d.get("units", [])
            if units:
                addr = units[0].get("address", "")
                # Construir formato similar a Catastro: "CL X Y OVIEDO (ASTURIAS)"
                if addr:
                    # convertir "C/" → "CL", etc.
                    rev = {"C/":"CL","Av.":"AV","Pza.":"PZ","Psje.":"PJ","Glta.":"GL","Rda.":"RD","Trav.":"TR"}
                    parts = addr.split()
                    if parts and parts[0] in rev:
                        parts[0] = rev[parts[0]]
                    coords[refcat]["address"] = " ".join(parts) + " OVIEDO (ASTURIAS)"
                    n_from_dnprc += 1
        except Exception:
            continue

    COORDS_CACHE.write_text(json.dumps(coords, ensure_ascii=False))
    size_mb = COORDS_CACHE.stat().st_size / 1e6
    print(f"Coords cache: {len(coords)} entries · "
          f"{n_from_bbox} desde bbox · {n_from_dnprc} con dirección · {size_mb:.1f} MB")
    print(f"Saved: {COORDS_CACHE}")


if __name__ == "__main__":
    main()
