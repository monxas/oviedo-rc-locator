#!/usr/bin/env python3
"""Genera el quality set: lista hardcodeada de RCs aleatorias y aplica el
pipeline (parcel_layer.render con snap) a cada una."""
import sys, json
from pathlib import Path
import numpy as np
import cv2
import urllib.request

from _compat import L, P

OUT = Path(__file__).parent / "static" / "quality_set"
OUT.mkdir(parents=True, exist_ok=True)

RCS = open("/tmp/quality_rcs.txt").read().strip().split("\n")


def fetch_catastro(X, Y, w=600, size_m=200):
    """WMS catastral con preferencia local."""
    try:
        from wms_local import wms_get_local
        img = wms_get_local(X-size_m/2, Y-size_m/2, X+size_m/2, Y+size_m/2, w=w)
        if img is not None:
            cv2.circle(img, (img.shape[1]//2, img.shape[0]//2), 22, (0,0,255), 4)
            cv2.drawMarker(img, (img.shape[1]//2, img.shape[0]//2), (0,0,255),
                           cv2.MARKER_CROSS, 50, 3)
            return img
    except Exception:
        pass
    url = ("https://ovc.catastro.meh.es/Cartografia/WMS/ServidorWMS.aspx?"
           "SERVICE=WMS&REQUEST=GetMap&VERSION=1.1.1&LAYERS=Catastro&"
           f"SRS=EPSG:25830&BBOX={X-size_m/2},{Y-size_m/2},{X+size_m/2},{Y+size_m/2}"
           f"&WIDTH={w}&HEIGHT={w}&FORMAT=image/png&STYLES=")
    req = urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0"})
    img = cv2.imdecode(np.frombuffer(urllib.request.urlopen(req).read(), np.uint8),
                       cv2.IMREAD_COLOR)
    cv2.circle(img, (w//2, w//2), 22, (0,0,255), 4)
    cv2.drawMarker(img, (w//2, w//2), (0,0,255), cv2.MARKER_CROSS, 50, 3)
    return img


def main():
    out_meta = []
    for idx, rc in enumerate(RCS):
        print(f"#{idx} {rc}")
        try:
            loc = L.locate(rc)
        except Exception as e:
            print(f"  ERR locate: {e}"); continue
        try:
            P.render(rc, str(OUT / f"{idx:02d}_plan_full.png"),
                     bbox_m=80, fetch_content=True, snap="global")
        except Exception as e:
            print(f"  ERR render: {e}"); continue
        full = cv2.imread(str(OUT / f"{idx:02d}_plan_full.png"))
        h, w = full.shape[:2]
        if w > 1100:
            full = cv2.resize(full, (1100, int(h*1100/w)), interpolation=cv2.INTER_AREA)
        cv2.imwrite(str(OUT / f"{idx:02d}_plan.jpg"), full,
                    [cv2.IMWRITE_JPEG_QUALITY, 85])
        (OUT / f"{idx:02d}_plan_full.png").unlink(missing_ok=True)
        try:
            cat = fetch_catastro(loc["utm"][0], loc["utm"][1])
            cv2.imwrite(str(OUT / f"{idx:02d}_cat.jpg"), cat,
                        [cv2.IMWRITE_JPEG_QUALITY, 85])
        except Exception as e:
            print(f"  catastro err: {e}")
        out_meta.append({
            "idx": idx, "rc": rc, "address": loc["address"],
            "sheet_name": loc["sheet_name"], "cell": loc["cell"],
            "sub_quadrant": loc["sub_quadrant"],
            "warnings": loc["warnings"],
        })
        print(f"  → {loc['sheet_name']}")
    json.dump(out_meta, open(Path(__file__).parent / "quality_set.json", "w"),
              ensure_ascii=False, indent=2)
    print(f"\n{len(out_meta)} guardadas")


if __name__ == "__main__":
    main()
