#!/usr/bin/env python3
"""Genera N RCs nuevas (no usadas en train ni test) y aplica la capa de
parcelas catastrales (polígonos + contenido) para validación visual."""
import sys, json, random, urllib.parse, urllib.request, time
from pathlib import Path
import numpy as np
import cv2

from _compat import L, P

OUT_DIR = Path(__file__).parent / "static" / "parcel_set"
OUT_DIR.mkdir(parents=True, exist_ok=True)

# Excluir las RCs ya usadas
USED = set()
for fn in ["candidates.json", "test_set.json"]:
    p = Path(__file__).parent / "static" / fn
    if not p.exists(): p = Path(__file__).parent / fn
    if p.exists():
        for c in json.loads(p.read_text()):
            USED.add(c["rc"])

# Calles aún sin probar mucho — para diversificar geográficamente
STREETS = [
    ("CL", "JUAN BAUTISTA AZNAR", range(1, 30, 2)),
    ("CL", "PRADO PICON", range(1, 30, 2)),
    ("CL", "DOCTOR CASAL", range(1, 30, 2)),
    ("CL", "ALONSO LOGROÑO", range(1, 30, 2)),
    ("CL", "GENERAL ZUBILLAGA", range(1, 50, 3)),
    ("CL", "VAZQUEZ DE MELLA", range(1, 30, 2)),
    ("CL", "MARQUES DE TEVERGA", range(1, 30, 2)),
    ("CL", "VICTOR CHAVARRI", range(1, 30, 2)),
    ("CL", "DOCTOR FLEMING", range(1, 30, 2)),
    ("CL", "ROSAL", range(1, 30, 2)),
    ("AV", "BUENAVISTA", range(1, 50, 3)),
    ("CL", "TEODORO CUESTA", range(1, 30, 2)),
    ("CL", "ARQUITECTO REGUERA", range(1, 20)),
    ("CL", "DARIO DE REGOYOS", range(1, 30, 2)),
    ("CL", "PEPIN RIVERO", range(1, 20)),
]

HEADERS = {"User-Agent": "Mozilla/5.0 (parcel-validator)"}


def query_rc(sigla, calle, numero):
    url = (f"https://ovc.catastro.meh.es/OVCServWeb/OVCWcfCallejero/COVCCallejero.svc/json/"
           f"Consulta_DNPLOC?Provincia=ASTURIAS&Municipio=OVIEDO&"
           f"Sigla={urllib.parse.quote(sigla)}&Calle={urllib.parse.quote(calle)}&Numero={numero}")
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        d = json.loads(urllib.request.urlopen(req, timeout=15).read())
        recs = d.get("consulta_dnplocResult", {}).get("lrcdnp", {}).get("rcdnp", [])
        if not recs: return None
        if isinstance(recs, dict): recs = [recs]
        for r in recs:
            rc = r.get("rc", {})
            full = rc.get("pc1","") + rc.get("pc2","") + rc.get("car","") + rc.get("cc1","") + rc.get("cc2","")
            if len(full) == 20: return full
        return None
    except Exception:
        return None


def fetch_catastro(X, Y, w=600, size_m=200):
    url = ("https://ovc.catastro.meh.es/Cartografia/WMS/ServidorWMS.aspx?"
           "SERVICE=WMS&REQUEST=GetMap&VERSION=1.1.1&LAYERS=Catastro&"
           f"SRS=EPSG:25830&BBOX={X-size_m/2},{Y-size_m/2},{X+size_m/2},{Y+size_m/2}"
           f"&WIDTH={w}&HEIGHT={w}&FORMAT=image/png&STYLES=")
    req = urllib.request.Request(url, headers=HEADERS)
    img = cv2.imdecode(np.frombuffer(urllib.request.urlopen(req).read(), np.uint8), cv2.IMREAD_COLOR)
    cv2.circle(img, (w//2, w//2), 22, (0, 0, 255), 4)
    cv2.drawMarker(img, (w//2, w//2), (0, 0, 255), cv2.MARKER_CROSS, 50, 3)
    return img


def main(target_n=7):
    pool = [(s, c, n) for s, c, nums in STREETS for n in nums]
    random.seed(456)
    random.shuffle(pool)
    candidates = []
    seen = set()

    for sigla, calle, num in pool:
        if len(candidates) >= target_n: break
        rc = query_rc(sigla, calle, num)
        if not rc or rc in USED or rc in seen: continue
        seen.add(rc)

        idx = len(candidates)
        plan_path = OUT_DIR / f"{idx:02d}_plan.jpg"
        cat_path = OUT_DIR / f"{idx:02d}_cat.jpg"
        try:
            print(f"#{idx} {sigla} {calle} {num}  RC={rc}")
            # Render con capa de parcelas
            contents = P.render(rc, str(OUT_DIR / f"{idx:02d}_plan_full.png"),
                                 bbox_m=80, fetch_content=True, max_workers=8)
            # Recargar el PNG y guardarlo como JPG comprimido
            full = cv2.imread(str(OUT_DIR / f"{idx:02d}_plan_full.png"))
            # Resize para que sea más manejable
            h, w = full.shape[:2]
            target_w = 1100
            if w > target_w:
                full = cv2.resize(full, (target_w, int(h*target_w/w)), interpolation=cv2.INTER_AREA)
            cv2.imwrite(str(plan_path), full, [cv2.IMWRITE_JPEG_QUALITY, 85])
            (OUT_DIR / f"{idx:02d}_plan_full.png").unlink()
            # Catastro de referencia
            loc = L.locate(rc)
            cat = fetch_catastro(loc["utm"][0], loc["utm"][1])
            cv2.imwrite(str(cat_path), cat, [cv2.IMWRITE_JPEG_QUALITY, 85])
            candidates.append({
                "idx": idx, "rc": rc, "address": loc["address"],
                "sheet_name": loc["sheet_name"], "cell": loc["cell"],
                "sub_quadrant": loc["sub_quadrant"],
                "warnings": loc["warnings"],
                "n_parcels_in_bbox": len(contents),
                "n_units_in_bbox": sum(len(v.get("units",[])) for v in contents.values()),
            })
        except Exception as e:
            print(f"   ERR: {e}")
            continue
        time.sleep(0.05)

    json.dump(candidates, open(Path(__file__).parent / "parcel_set.json", "w"),
              indent=2, ensure_ascii=False)
    print(f"\n{len(candidates)} RCs guardadas")


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 7
    main(n)
