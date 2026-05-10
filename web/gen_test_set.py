#!/usr/bin/env python3
"""Genera N RCs NUEVAS (no usadas en el training set) y un mosaico para
evaluar la precisión del modelo refinado."""
import sys, json, random, urllib.parse, urllib.request, time
from pathlib import Path
import numpy as np
import cv2
import fitz

from _compat import L

OUT_DIR = Path(__file__).parent / "static" / "test_set"
OUT_DIR.mkdir(parents=True, exist_ok=True)

# Cargar RCs ya usadas en training
TRAIN = json.load(open(Path(__file__).parent / "static" / "candidates.json"))
USED_RCS = {c["rc"] for c in TRAIN}

# Calles distintas a las del training (zonas periféricas)
STREETS_TEST = [
    ("CL", "MELQUIADES ALVAREZ", range(2, 50, 2)),
    ("CL", "MARTINEZ MARINA", range(1, 30, 2)),
    ("CL", "JOVELLANOS", range(1, 30)),
    ("CL", "RUA", range(1, 30)),
    ("CL", "MENDIZABAL", range(1, 30, 2)),
    ("CL", "TORENO", range(1, 30, 2)),
    ("CL", "QUINTANA", range(1, 30)),
    ("CL", "JESUS", range(1, 30)),
    ("CL", "MILICIAS NACIONALES", range(1, 30, 2)),
    ("CL", "CIMADEVILLA", range(1, 30)),
    ("CL", "RAMON Y CAJAL", range(1, 50, 3)),
    ("CL", "EUSEBIO GONZALEZ ABASCAL", range(1, 30)),
    ("CL", "RIO NORA", range(1, 30, 2)),
    ("CL", "CARMEN", range(1, 30)),
    ("AV", "TORRELAVEGA", range(1, 30, 2)),
]

HEADERS = {"User-Agent": "Mozilla/5.0 (test-validator)"}


def query_rc(sigla, calle, numero):
    import urllib.parse
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
            pc1 = rc.get("pc1", ""); pc2 = rc.get("pc2", "")
            car = rc.get("car", ""); cc1 = rc.get("cc1", ""); cc2 = rc.get("cc2", "")
            if pc1 and pc2 and car and cc1 and cc2:
                return pc1 + pc2 + car + cc1 + cc2
        return None
    except Exception:
        return None


def render_plan_zoom(loc, target_w=900):
    sheet_pdf = L.CACHE_DIR / loc["sheet_name"]
    L.fetch(loc["sheet_url"], sheet_pdf, expected_type="application/pdf")
    doc = fitz.open(sheet_pdf)
    pix = doc[0].get_pixmap(dpi=200)
    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.h, pix.w, pix.n)
    plan = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR if pix.n == 3 else cv2.COLOR_RGBA2BGR)
    H, W = plan.shape[:2]
    gray = cv2.cvtColor(plan, cv2.COLOR_BGR2GRAY)
    _, binv = cv2.threshold(gray, 80, 255, cv2.THRESH_BINARY_INV)
    binv = cv2.morphologyEx(binv, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    contours, _ = cv2.findContours(binv, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    cands = [(w*h, x, y, w, h) for c in contours for x, y, w, h in [cv2.boundingRect(c)]
             if 0.30*W*H < w*h < 0.90*W*H]
    if not cands: return None
    _, fx, fy, fw, fh = sorted(cands, reverse=True)[0]
    rx, ry = loc["body_relative"]["rx"], loc["body_relative"]["ry"]
    rc_x = int(fx + rx*fw); rc_y = int(fy + ry*fh)
    out = plan.copy()
    cv2.circle(out, (rc_x, rc_y), 25, (0, 0, 255), 5)
    cv2.circle(out, (rc_x, rc_y), 6, (0, 0, 255), -1)
    for dx, dy in [(-110, 0), (110, 0), (0, -110), (0, 110)]:
        cv2.line(out, (rc_x + dx//4, rc_y + dy//4), (rc_x + dx, rc_y + dy), (0, 0, 255), 3)
    m_per_px = 553.55 / fw
    half_px = int(125 / m_per_px)
    y0 = max(0, rc_y - half_px); y1 = min(H, rc_y + half_px)
    x0 = max(0, rc_x - half_px); x1 = min(W, rc_x + half_px)
    crop = out[y0:y1, x0:x1]
    h, w = crop.shape[:2]
    if w > target_w:
        crop = cv2.resize(crop, (target_w, int(h*target_w/w)), interpolation=cv2.INTER_AREA)
    return crop


def fetch_catastro(X, Y, size_m=200, w=600):
    url = ("https://ovc.catastro.meh.es/Cartografia/WMS/ServidorWMS.aspx?"
           "SERVICE=WMS&REQUEST=GetMap&VERSION=1.1.1&LAYERS=Catastro&"
           f"SRS=EPSG:25830&BBOX={X-size_m/2},{Y-size_m/2},{X+size_m/2},{Y+size_m/2}"
           f"&WIDTH={w}&HEIGHT={w}&FORMAT=image/png&STYLES=")
    req = urllib.request.Request(url, headers=HEADERS)
    img = cv2.imdecode(np.frombuffer(urllib.request.urlopen(req).read(), np.uint8),
                       cv2.IMREAD_COLOR)
    cv2.circle(img, (w//2, w//2), 22, (0, 0, 255), 4)
    cv2.drawMarker(img, (w//2, w//2), (0, 0, 255), cv2.MARKER_CROSS, 50, 3)
    return img


def main(target_n=10):
    pool = []
    for sigla, calle, nums in STREETS_TEST:
        for n in nums:
            pool.append((sigla, calle, n))
    random.seed(123)
    random.shuffle(pool)

    candidates = []
    seen = set()
    for sigla, calle, num in pool:
        if len(candidates) >= target_n: break
        rc = query_rc(sigla, calle, num)
        if not rc or rc in USED_RCS or rc in seen: continue
        seen.add(rc)
        try:
            loc = L.locate(rc)
        except L.RCError:
            continue
        try:
            plan_crop = render_plan_zoom(loc)
            if plan_crop is None: continue
            cat_img = fetch_catastro(loc["utm"][0], loc["utm"][1])
        except Exception:
            continue
        idx = len(candidates)
        cv2.imwrite(str(OUT_DIR / f"{idx:02d}_plan.jpg"), plan_crop, [cv2.IMWRITE_JPEG_QUALITY, 85])
        cv2.imwrite(str(OUT_DIR / f"{idx:02d}_cat.jpg"),  cat_img,   [cv2.IMWRITE_JPEG_QUALITY, 85])
        candidates.append({
            "idx": idx, "rc": rc, "address": loc["address"],
            "sheet_name": loc["sheet_name"], "cell": loc["cell"],
            "sub_quadrant": loc["sub_quadrant"],
            "warnings": loc["warnings"],
        })
        print(f"  {idx:2d}: {loc['address'][:50]:50s} → {loc['sheet_name']}")
        time.sleep(0.05)

    # Mosaico: 10 filas, plan + catastro lado a lado
    from PIL import Image, ImageDraw, ImageFont
    rows = []
    for c in candidates:
        plan = Image.open(OUT_DIR / f"{c['idx']:02d}_plan.jpg")
        cat = Image.open(OUT_DIR / f"{c['idx']:02d}_cat.jpg")
        # Misma altura
        h = 400
        plan = plan.resize((int(plan.width * h / plan.height), h), Image.LANCZOS)
        cat = cat.resize((int(cat.width  * h / cat.height ), h), Image.LANCZOS)
        rows.append((c, plan, cat))
    title_h = 50
    canvas = Image.new("RGB", (max(p.width + ca.width + 30 for _, p, ca in rows) + 60,
                                sum(p.height for _, p, _ in rows) + title_h*len(rows) + 20),
                        "white")
    d = ImageDraw.Draw(canvas)
    try:
        f = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial Bold.ttf", 22)
    except:
        f = ImageFont.load_default()
    y = 10
    for c, plan, cat in rows:
        d.text((20, y + 5),
               f"#{c['idx']}  RC {c['rc']}  ·  {c['address']}  →  {c['sheet_name']}",
               fill="black", font=f)
        y += title_h
        canvas.paste(plan, (20, y))
        canvas.paste(cat, (20 + plan.width + 30, y))
        y += plan.height
    canvas.save(Path(__file__).parent / "test_mosaic.png", optimize=True)

    # JSON metadata
    json.dump(candidates, open(Path(__file__).parent / "test_set.json", "w"),
              indent=2, ensure_ascii=False)
    print(f"\n{len(candidates)} RCs test guardadas. Mosaico: test_mosaic.png")


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    main(n)
