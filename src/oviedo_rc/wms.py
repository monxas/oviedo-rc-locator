"""WMS catastral: cache-first con mosaico local y fallback a GetMap remoto."""
import re
import urllib.request
from pathlib import Path

import numpy as np

from .config import WMS_DIR, HTTP_HEADERS

_TILE_RE = re.compile(r"wms_(\d+)_(\d+)_(\d+)_(\d+)_([\d.]+)\.png$")
_INDEX = None


def _index():
    global _INDEX
    if _INDEX is not None:
        return _INDEX
    out = []
    if WMS_DIR.exists():
        for p in WMS_DIR.glob("wms_*.png"):
            m = _TILE_RE.search(p.name)
            if not m:
                continue
            x1, y1, x2, y2 = map(int, m.groups()[:4])
            mpp = float(m.group(5))
            out.append((x1, y1, x2, y2, mpp, p))
    _INDEX = out
    return out


def get_local(xmin, ymin, xmax, ymax, w=900):
    """Recorta el bbox UTM del mosaico cacheado. None si no está cubierto."""
    import cv2
    idx = _index()
    if not idx:
        return None
    by_mpp = {}
    for t in idx:
        by_mpp.setdefault(t[4], []).append(t)
    for mpp in sorted(by_mpp.keys()):
        tiles = by_mpp[mpp]
        relevant = [t for t in tiles
                    if not (t[2] <= xmin or t[0] >= xmax
                            or t[3] <= ymin or t[1] >= ymax)]
        if not relevant:
            continue
        rx_min = min(t[0] for t in relevant); rx_max = max(t[2] for t in relevant)
        ry_min = min(t[1] for t in relevant); ry_max = max(t[3] for t in relevant)
        if rx_min > xmin or rx_max < xmax or ry_min > ymin or ry_max < ymax:
            continue
        big_w = int((rx_max - rx_min) / mpp)
        big_h = int((ry_max - ry_min) / mpp)
        big = np.full((big_h, big_w, 3), 255, dtype=np.uint8)
        for x1, y1, x2, y2, _, p in relevant:
            tile = cv2.imread(str(p))
            if tile is None:
                continue
            ox = int((x1 - rx_min) / mpp)
            oy = int((ry_max - y2) / mpp)
            tw = int((x2 - x1) / mpp); th = int((y2 - y1) / mpp)
            if tile.shape[1] != tw or tile.shape[0] != th:
                tile = cv2.resize(tile, (tw, th), interpolation=cv2.INTER_AREA)
            big[oy:oy + th, ox:ox + tw] = tile
        cx0 = int((xmin - rx_min) / mpp); cx1 = int((xmax - rx_min) / mpp)
        cy0 = int((ry_max - ymax) / mpp); cy1 = int((ry_max - ymin) / mpp)
        crop = big[cy0:cy1, cx0:cx1]
        if w and crop.shape[1] != w:
            target_h = int(crop.shape[0] * w / crop.shape[1])
            crop = cv2.resize(
                crop, (w, target_h),
                interpolation=cv2.INTER_AREA if w < crop.shape[1] else cv2.INTER_CUBIC,
            )
        return crop
    return None


def get(xmin, ymin, xmax, ymax, w=900, *, layer="Catastro"):
    """Devuelve PNG bytes del WMS catastral. Cache-first (mosaico local),
    fallback a WMS remoto."""
    import cv2
    img = get_local(xmin, ymin, xmax, ymax, w=w)
    if img is not None:
        ok, buf = cv2.imencode(".png", img)
        if ok:
            return buf.tobytes()
    # WMS remoto
    url = ("https://ovc.catastro.meh.es/Cartografia/WMS/ServidorWMS.aspx?"
           "SERVICE=WMS&REQUEST=GetMap&VERSION=1.1.1&"
           f"LAYERS={layer}&SRS=EPSG:25830&"
           f"BBOX={xmin},{ymin},{xmax},{ymax}&WIDTH={w}&HEIGHT={w}"
           "&FORMAT=image/png&STYLES=")
    req = urllib.request.Request(url, headers=HTTP_HEADERS)
    return urllib.request.urlopen(req, timeout=60).read()


def coverage():
    """Resumen de tiles cacheados."""
    idx = _index()
    if not idx:
        return {"tiles": 0}
    by_mpp = {}
    for t in idx:
        by_mpp.setdefault(t[4], []).append(t)
    out = {"tiles": len(idx), "by_mpp": {}}
    for mpp, tiles in by_mpp.items():
        x1 = min(t[0] for t in tiles); x2 = max(t[2] for t in tiles)
        y1 = min(t[1] for t in tiles); y2 = max(t[3] for t in tiles)
        out["by_mpp"][mpp] = {
            "n_tiles": len(tiles),
            "bbox": [x1, y1, x2, y2],
            "size_mb": sum(t[5].stat().st_size for t in tiles) / 1e6,
        }
    return out
