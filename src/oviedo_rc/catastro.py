"""Cliente del Catastro: rc_to_utm, consulta_dnprc, WFS bbox.
Todas las funciones son CACHE-FIRST con fallback online."""
import json
import re
import urllib.request

from .config import (CACHE_DIR, COORDS_FILE, PARCELS_DIR, HTTP_HEADERS)
from .errors import RCError
from .http_utils import http_get

VIA_TYPE = {
    "CL": "C/", "AV": "Av.", "PZ": "Pza.", "PJ": "Psje.",
    "GL": "Glta.", "RD": "Rda.", "TR": "Trav.",
}

# ---------- Cache rc14 → coords (offline) ----------

_COORDS_CACHE = None


def _load_coords_local():
    global _COORDS_CACHE
    if _COORDS_CACHE is not None:
        return _COORDS_CACHE
    if not COORDS_FILE.exists():
        _COORDS_CACHE = {}
    else:
        try:
            _COORDS_CACHE = json.loads(COORDS_FILE.read_text())
        except Exception:
            _COORDS_CACHE = {}
    return _COORDS_CACHE


def rc_to_utm(rc14):
    """RC (14 chars) → (X, Y, dirección) en UTM ETRS89 30N (EPSG:25830)."""
    local = _load_coords_local()
    if rc14 in local:
        e = local[rc14]
        return float(e["x"]), float(e["y"]), e.get("address", "")
    # Online fallback
    url = (
        f"https://ovc.catastro.meh.es/ovcservweb/OVCSWLocalizacionRC/"
        f"OVCCoordenadas.asmx/Consulta_CPMRC?Provincia=&Municipio=&"
        f"SRS=EPSG:25830&RC={rc14}"
    )
    r = http_get(url, timeout=30)
    text = r.text
    m_x = re.search(r"<xcen>([\d.]+)</xcen>", text)
    m_y = re.search(r"<ycen>([\d.]+)</ycen>", text)
    if not m_x or not m_y:
        err = re.search(r"<des>([^<]+)</des>", text)
        msg = err.group(1).strip() if err else "respuesta sin coordenadas"
        raise RCError(f"Catastro: {msg} (RC={rc14})")
    addr = re.search(r"<ldt>([^<]+)</ldt>", text)
    return float(m_x.group(1)), float(m_y.group(1)), (addr.group(1) if addr else "")


# ---------- WFS de parcelas (bbox) ----------

_BBOX_INDEX = None


def _build_bbox_index():
    """Carga lazy de TODAS las parcelas conocidas en bbox_*.json → rc → record."""
    global _BBOX_INDEX
    if _BBOX_INDEX is not None:
        return _BBOX_INDEX
    _BBOX_INDEX = {}
    for jf in PARCELS_DIR.glob("bbox_*.json"):
        try:
            for p in json.loads(jf.read_text()):
                rc = p.get("refcat")
                if rc and rc not in _BBOX_INDEX:
                    _BBOX_INDEX[rc] = p
        except Exception:
            continue
    return _BBOX_INDEX


def _bbox_from_local(xmin, ymin, xmax, ymax):
    """Devuelve parcelas cuyo polígono intersecta el bbox, si la zona está
    completamente cubierta por chunks ya descargados. None si cobertura parcial."""
    chunk_bboxes = []
    for jf in PARCELS_DIR.glob("bbox_*.json"):
        try:
            x1, y1, x2, y2 = map(int, jf.stem.split("_")[1:5])
            chunk_bboxes.append((x1, y1, x2, y2))
        except Exception:
            continue
    if not chunk_bboxes:
        return None
    # Aproximación: la unión bounding rectangle cubre el bbox solicitado
    cx1 = min(b[0] for b in chunk_bboxes); cy1 = min(b[1] for b in chunk_bboxes)
    cx2 = max(b[2] for b in chunk_bboxes); cy2 = max(b[3] for b in chunk_bboxes)
    if not (cx1 <= xmin and cx2 >= xmax and cy1 <= ymin and cy2 >= ymax):
        return None
    idx = _build_bbox_index()
    out = []
    for p in idx.values():
        poly = p.get("poly_utm", [])
        if not poly:
            continue
        pxs = [pt[0] for pt in poly]; pys = [pt[1] for pt in poly]
        if (max(pxs) < xmin or min(pxs) > xmax
                or max(pys) < ymin or min(pys) > ymax):
            continue
        out.append(p)
    return out


def wfs_parcels_bbox(xmin, ymin, xmax, ymax):
    """Lista de parcelas (refcat, label, area_m2, poly_utm) en bbox.
    Cache-first: 1) cache exacto, 2) índice agregado de chunks, 3) WFS online."""
    cache = PARCELS_DIR / f"bbox_{int(xmin)}_{int(ymin)}_{int(xmax)}_{int(ymax)}.json"
    if cache.exists():
        return json.loads(cache.read_text())
    local = _bbox_from_local(xmin, ymin, xmax, ymax)
    if local is not None:
        return local
    # WFS online
    url = (f"https://ovc.catastro.meh.es/INSPIRE/wfsCP.aspx?"
           f"service=wfs&version=2.0.0&request=GetFeature"
           f"&typenames=cp:CadastralParcel&srsname=EPSG:25830"
           f"&bbox={xmin},{ymin},{xmax},{ymax},EPSG:25830")
    req = urllib.request.Request(url, headers=HTTP_HEADERS)
    xml = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", errors="ignore")
    parcels = []
    for m in re.finditer(
        r'<cp:CadastralParcel[^>]*gml:id="([^"]+)".*?</cp:CadastralParcel>',
        xml, re.DOTALL,
    ):
        block = m.group(0)
        rm = re.search(r'<cp:nationalCadastralReference>([^<]+)</cp:nationalCadastralReference>', block)
        lm = re.search(r'<cp:label>([^<]+)</cp:label>', block)
        am = re.search(r'<cp:areaValue[^>]*>([0-9.]+)</cp:areaValue>', block)
        pm = re.search(r'<gml:posList[^>]*>([^<]+)</gml:posList>', block)
        if not (rm and pm):
            continue
        coords = list(map(float, pm.group(1).split()))
        parcels.append({
            "refcat": rm.group(1),
            "label": lm.group(1) if lm else "",
            "area_m2": float(am.group(1)) if am else None,
            "poly_utm": list(zip(coords[::2], coords[1::2])),
        })
    cache.write_text(json.dumps(parcels, ensure_ascii=False))
    return parcels


# ---------- WFS de UNA parcela ----------

def get_parcel_polygon(refcat14):
    """Polígono UTM de la parcela. Cache-first."""
    cache = PARCELS_DIR / f"poly_{refcat14}.json"
    if cache.exists():
        return json.loads(cache.read_text())
    idx = _build_bbox_index()
    if refcat14 in idx:
        p = idx[refcat14]
        return {
            "refcat14": refcat14,
            "label": p.get("label"),
            "area_m2": p.get("area_m2"),
            "polygon_utm": p.get("poly_utm"),
        }
    # WFS online
    url = (f"https://ovc.catastro.meh.es/INSPIRE/wfsCP.aspx?"
           f"service=wfs&version=2.0.0&request=GetFeature"
           f"&STOREDQUERIE_ID=GetParcel&srsname=EPSG:25830&refcat={refcat14}")
    req = urllib.request.Request(url, headers=HTTP_HEADERS)
    xml = urllib.request.urlopen(req, timeout=15).read().decode("utf-8", errors="ignore")
    posm = re.search(r'<gml:posList[^>]*>([^<]+)</gml:posList>', xml)
    label = re.search(r'<cp:label>([^<]+)</cp:label>', xml)
    area = re.search(r'<cp:areaValue[^>]*>([0-9.]+)</cp:areaValue>', xml)
    if not posm:
        return None
    coords = list(map(float, posm.group(1).split()))
    result = {
        "refcat14": refcat14,
        "label": label.group(1) if label else None,
        "area_m2": float(area.group(1)) if area else None,
        "polygon_utm": list(zip(coords[::2], coords[1::2])),
    }
    cache.write_text(json.dumps(result, ensure_ascii=False))
    return result


# ---------- Contenido catastral (DNPRC) ----------

def _safe_num(s):
    if s is None:
        return 0
    try:
        return int(round(float(str(s).replace(",", "."))))
    except (ValueError, TypeError):
        return 0


def consulta_dnprc(refcat14):
    """Devuelve {refcat14, units} con info de cada inmueble."""
    cache = PARCELS_DIR / f"dnprc_{refcat14}.json"
    if cache.exists():
        return json.loads(cache.read_text())
    url = (f"https://ovc.catastro.meh.es/OVCServWeb/OVCWcfCallejero/"
           f"COVCCallejero.svc/json/Consulta_DNPRC?RefCat={refcat14}")
    try:
        req = urllib.request.Request(url, headers=HTTP_HEADERS)
        d = json.loads(urllib.request.urlopen(req, timeout=15).read())
    except Exception as e:
        return {"error": str(e), "refcat14": refcat14, "units": []}

    res = d.get("consulta_dnprcResult", {})
    out = []

    def parse_record(r):
        rc = r.get("rc", r.get("idbi", {}).get("rc", {}))
        full_rc = (rc.get("pc1", "") + rc.get("pc2", "") + rc.get("car", "")
                   + rc.get("cc1", "") + rc.get("cc2", ""))
        dir_d = r.get("dt", {}).get("locs", {}).get("lous", {}).get("lourb", {}).get("dir", {})
        loint = r.get("dt", {}).get("locs", {}).get("lous", {}).get("lourb", {}).get("loint", {})
        debi = r.get("debi", {})
        return {
            "rc": full_rc,
            "address": (
                f"{VIA_TYPE.get(dir_d.get('tv', ''), dir_d.get('tv', ''))} "
                f"{dir_d.get('nv', '').strip()} {dir_d.get('pnp', '').strip()}"
            ).strip(),
            "floor": loint.get("pt", ""),
            "door": loint.get("pu", ""),
            "stair": loint.get("es", ""),
            "use": debi.get("luso", ""),
            "area_m2": _safe_num(debi.get("sfc")),
            "year": int(debi.get("ant", "0")) if str(debi.get("ant", "")).isdigit() else None,
        }

    recs = res.get("lrcdnp", {}).get("rcdnp", [])
    if recs:
        if isinstance(recs, dict):
            recs = [recs]
        out.extend(parse_record(r) for r in recs)
    bico = res.get("bico", {})
    if bico and not out:
        bi = bico.get("bi", {})
        if bi:
            out.append(parse_record({**bi, "rc": bi.get("idbi", {}).get("rc", {})}))

    result = {"refcat14": refcat14, "units": out}
    cache.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    return result
