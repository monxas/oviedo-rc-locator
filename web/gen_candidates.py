#!/usr/bin/env python3
"""Genera N RCs candidatas distribuidas en Oviedo, ejecuta el modelo, y produce
imágenes (plano zoom + catastro) para validación humana en la app web."""
import sys, os, json, random, urllib.request, re, time
from pathlib import Path
import numpy as np
import cv2
import fitz

from _compat import L

OUT_DIR = Path(__file__).parent / "static" / "imgs"
OUT_DIR.mkdir(parents=True, exist_ok=True)
META = Path(__file__).parent / "static" / "candidates.json"

# Calles representativas de Oviedo (cubrir distintas zonas)
STREETS = [
    ("CL", "URIA", range(1, 70, 3)),
    ("CL", "PELAYO", range(1, 30, 2)),
    ("CL", "ARGUELLES", range(1, 40, 2)),
    ("CL", "FONCALADA", range(1, 30, 2)),
    ("CL", "FRUELA", range(1, 25)),
    ("CL", "CONDE DE TORENO", range(1, 30, 2)),
    ("CL", "MAGDALENA", range(1, 30, 2)),
    ("CL", "MARQUES DE PIDAL", range(1, 30, 2)),
    ("CL", "MARQUES DE SANTA CRUZ", range(1, 30, 2)),
    ("CL", "SAN FRANCISCO", range(1, 30, 2)),
    ("CL", "CERVANTES", range(1, 30, 2)),
    ("CL", "MANUEL PEDREGAL", range(1, 20)),
    ("AV", "GALICIA", range(1, 100, 3)),
    ("CL", "INDEPENDENCIA", range(1, 30, 2)),
    ("CL", "MIGUEL ANGEL BLANCO", range(1, 25)),
    ("CL", "SAMUEL SANCHEZ", range(1, 25)),
    ("CL", "FUERTES ACEVEDO", range(1, 90, 3)),
    ("CL", "GENERAL ELORZA", range(1, 90, 3)),
    ("CL", "TENDERINA ALTA", range(1, 60, 3)),
    ("CL", "TENDERINA BAJA", range(1, 60, 3)),
    ("CL", "CAMPOAMOR", range(1, 30, 2)),
    ("CL", "GIL DE JAZ", range(1, 30, 2)),
    ("CL", "CAVEDA", range(1, 30, 2)),
    ("CL", "POSADA HERRERA", range(1, 30, 2)),
    ("CL", "SAN BERNABE", range(1, 30, 2)),
    ("CL", "SANTA TERESA", range(1, 30, 2)),
    ("PZ", "AMERICA", range(1, 15)),
    ("PZ", "ESCANDALERA", range(1, 10)),
    ("PZ", "CONSTITUCION", range(1, 10)),
    ("CL", "ALONSO QUINTANILLA", range(1, 20)),
    ("CL", "SANTA SUSANA", range(1, 20)),
    ("CL", "AZCARRAGA", range(1, 20)),
    ("CL", "GASCONA", range(1, 30, 2)),
    ("CL", "SAN ANTONIO", range(1, 20)),
    ("AV", "PUMARIN", range(1, 30, 2)),
]

HEADERS = {"User-Agent": "Mozilla/5.0 (validator)"}


def query_rc(sigla, calle, numero):
    """Pide al catastro la RC de una dirección."""
    import urllib.parse
    url = (f"https://ovc.catastro.meh.es/OVCServWeb/OVCWcfCallejero/COVCCallejero.svc/json/"
           f"Consulta_DNPLOC?Provincia=ASTURIAS&Municipio=OVIEDO&"
           f"Sigla={urllib.parse.quote(sigla)}&Calle={urllib.parse.quote(calle)}&Numero={numero}")
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        d = json.loads(urllib.request.urlopen(req, timeout=15).read())
        # Estructura: consulta_dnplocResult.lrcdnp.rcdnp[0].rc → pc1+pc2+car+cc1+cc2
        recs = d.get("consulta_dnplocResult", {}).get("lrcdnp", {}).get("rcdnp", [])
        if not recs: return None
        if isinstance(recs, dict): recs = [recs]
        # Tomar primera con cargo válido
        for r in recs:
            rc = r.get("rc", {})
            pc1 = rc.get("pc1", ""); pc2 = rc.get("pc2", "")
            car = rc.get("car", ""); cc1 = rc.get("cc1", ""); cc2 = rc.get("cc2", "")
            if pc1 and pc2 and car and cc1 and cc2:
                return pc1 + pc2 + car + cc1 + cc2
        return None
    except Exception as e:
        return None


def render_plan_zoom(loc, target_w=900):
    """Genera crop del plano alrededor de la marca + cruz roja."""
    sheet_pdf = L.CACHE_DIR / loc["sheet_name"]
    L.fetch(loc["sheet_url"], sheet_pdf, expected_type="application/pdf")
    doc = fitz.open(sheet_pdf)
    pix = doc[0].get_pixmap(dpi=200)
    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.h, pix.w, pix.n)
    plan = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR if pix.n == 3 else cv2.COLOR_RGBA2BGR)
    H, W = plan.shape[:2]

    # Detectar marco
    gray = cv2.cvtColor(plan, cv2.COLOR_BGR2GRAY)
    _, binv = cv2.threshold(gray, 80, 255, cv2.THRESH_BINARY_INV)
    binv = cv2.morphologyEx(binv, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    contours, _ = cv2.findContours(binv, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    cands = [(w*h, x, y, w, h) for c in contours for x, y, w, h in [cv2.boundingRect(c)]
             if 0.30*W*H < w*h < 0.90*W*H]
    if not cands:
        return None, None, None
    _, fx, fy, fw, fh = sorted(cands, reverse=True)[0]

    rx, ry = loc["body_relative"]["rx"], loc["body_relative"]["ry"]
    rc_x = int(fx + rx*fw); rc_y = int(fy + ry*fh)

    # Marca
    out = plan.copy()
    cv2.circle(out, (rc_x, rc_y), 25, (0, 0, 255), 5)
    cv2.circle(out, (rc_x, rc_y), 6, (0, 0, 255), -1)
    for dx, dy in [(-110, 0), (110, 0), (0, -110), (0, 110)]:
        cv2.line(out, (rc_x + dx//4, rc_y + dy//4), (rc_x + dx, rc_y + dy), (0, 0, 255), 3)

    # Crop 200m alrededor (a 200dpi: 1px ≈ 0.13m, 200m ≈ 1540px)
    m_per_px = 553.55 / fw
    half_px = int(125 / m_per_px)
    y0 = max(0, rc_y - half_px); y1 = min(H, rc_y + half_px)
    x0 = max(0, rc_x - half_px); x1 = min(W, rc_x + half_px)
    crop = out[y0:y1, x0:x1]
    # Escalar a target_w
    h, w = crop.shape[:2]
    if w > target_w:
        new_h = int(h * target_w / w)
        crop = cv2.resize(crop, (target_w, new_h), interpolation=cv2.INTER_AREA)
    # Mark center pixel offset (RC en el crop)
    # RC en el crop: (rc_x - x0, rc_y - y0) escalado
    rc_in_crop_x = (rc_x - x0) * (crop.shape[1] / (x1 - x0))
    rc_in_crop_y = (rc_y - y0) * (crop.shape[0] / (y1 - y0))
    return crop, (rc_in_crop_x, rc_in_crop_y), (m_per_px * (x1 - x0) / crop.shape[1])


def fetch_catastro(X, Y, size_m=200, w=600):
    url = ("https://ovc.catastro.meh.es/Cartografia/WMS/ServidorWMS.aspx?"
           "SERVICE=WMS&REQUEST=GetMap&VERSION=1.1.1&LAYERS=Catastro&"
           f"SRS=EPSG:25830&BBOX={X-size_m/2},{Y-size_m/2},{X+size_m/2},{Y+size_m/2}"
           f"&WIDTH={w}&HEIGHT={w}&FORMAT=image/png&STYLES=")
    req = urllib.request.Request(url, headers=HEADERS)
    img = cv2.imdecode(np.frombuffer(urllib.request.urlopen(req).read(), np.uint8), cv2.IMREAD_COLOR)
    cv2.circle(img, (w//2, w//2), 22, (0, 0, 255), 4)
    cv2.drawMarker(img, (w//2, w//2), (0, 0, 255), cv2.MARKER_CROSS, 50, 3)
    return img


def main(target_n=100):
    candidates = []
    seen_rcs = set()
    # Mezclar combinaciones para distribuir geográficamente
    pool = []
    for sigla, calle, nums in STREETS:
        for n in nums:
            pool.append((sigla, calle, n))
    random.seed(42)
    random.shuffle(pool)

    print(f"Pool de {len(pool)} direcciones; objetivo {target_n} candidatos.")
    i = 0
    for sigla, calle, num in pool:
        if len(candidates) >= target_n: break
        i += 1
        rc = query_rc(sigla, calle, num)
        if not rc or rc in seen_rcs: continue
        seen_rcs.add(rc)
        try:
            loc = L.locate(rc)
        except L.RCError as e:
            print(f"  {i:3d}. {sigla} {calle} {num} — RC {rc}  SKIP: {e}")
            continue

        # Generar imágenes
        try:
            plan_crop, rc_in_crop, m_per_px_crop = render_plan_zoom(loc)
            if plan_crop is None:
                print(f"  {i:3d}. SKIP frame")
                continue
            cat_img = fetch_catastro(loc["utm"][0], loc["utm"][1])
        except Exception as e:
            print(f"  {i:3d}. ERR rendering: {e}")
            continue

        idx = len(candidates)
        plan_path = OUT_DIR / f"{idx:03d}_plan.jpg"
        cat_path = OUT_DIR / f"{idx:03d}_cat.jpg"
        cv2.imwrite(str(plan_path), plan_crop, [cv2.IMWRITE_JPEG_QUALITY, 80])
        cv2.imwrite(str(cat_path), cat_img, [cv2.IMWRITE_JPEG_QUALITY, 85])

        candidates.append({
            "idx": idx,
            "rc": rc,
            "address": loc["address"],
            "utm": list(loc["utm"]),
            "cell": loc["cell"],
            "sub_quadrant": loc["sub_quadrant"],
            "sheet_name": loc["sheet_name"],
            "body_rx": loc["body_relative"]["rx"],
            "body_ry": loc["body_relative"]["ry"],
            "warnings": loc["warnings"],
            "plan_img": f"imgs/{plan_path.name}",
            "cat_img": f"imgs/{cat_path.name}",
            "plan_rc_xy": list(rc_in_crop),  # pixel del RC dentro del crop
            "plan_m_per_px": m_per_px_crop,  # m/px del crop renderizado
            "plan_size": [plan_crop.shape[1], plan_crop.shape[0]],
        })
        print(f"  {i:3d}. {idx:3d}: {loc['address'][:50]:50s} → {loc['sheet_name']}")
        time.sleep(0.05)

    META.write_text(json.dumps(candidates, indent=2, ensure_ascii=False))
    print(f"\n{len(candidates)} candidatos guardados en {META}")


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 100
    main(n)
