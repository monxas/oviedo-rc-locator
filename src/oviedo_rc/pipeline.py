"""Pipeline end-to-end: RC → bundle (planos anotados, polígono, contenido).

`process_rc(rc, concejo=None)` → `RCBundle` con todos los artefactos en
disco. Si `concejo` es None, se infiere por bbox UTM (fallback OVIEDO).
"""
import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

from .concejo import OVIEDO, Concejo, get_concejo_for_utm
from . import calibration, geom, catastro, pgou, render, snap, wms


@dataclass
class RCBundle:
    """Artefactos generados para un RC. Todas las rutas son absolutas."""
    rc: str
    address: str
    utm: tuple
    sheet_name: str
    sheet_path: str
    bundle_dir: str
    plan_full_png: str
    plan_zoom_png: str
    polygon_png: Optional[str]
    wms_png: Optional[str]
    content_json: str
    metadata_json: str

    def to_dict(self):
        return asdict(self)


def _anchor_utm(col, row_idx, compass, concejo: Concejo):
    m = concejo.malla
    sub_x_off = 0 if "W" in compass else m.cell_w / 2
    sub_y_off = 0 if "N" in compass else m.cell_h / 2
    body_x_min = m.x0 + col * m.cell_w + sub_x_off - m.marg_x
    body_y_max = m.ymax - row_idx * m.cell_h - sub_y_off + m.marg_y
    return body_x_min, body_y_max


def _render_on_sheet(sheet_name, col, row_idx, sub_quadrant, sub_compass,
                      poly_utm, snap_polygon, concejo: Concejo):
    """Renderiza el polígono sobre `sheet_name` aplicando cal+snap.
    Devuelve dict con: img, body_rect, poly_px, snap_info, snap_score."""
    import cv2
    pdf_path = pgou.fetch_sheet_pdf(sheet_name, concejo)
    img, _, _ = render.render_pdf_page(pdf_path)
    body_rect = render.detect_body_rect(img)
    anchor = _anchor_utm(col, row_idx, sub_compass, concejo)
    poly_px = render.utm_polygon_to_pixel(poly_utm, body_rect, anchor, sub_compass)
    cell_key = f"{col}-{'ABCDEFGHIJKLMNOPQRSTUVWXY'[row_idx]}"
    cal_dx, cal_dy = calibration.offset_for(cell_key, sub_quadrant, concejo)
    poly_px = [(x + cal_dx, y + cal_dy) for x, y in poly_px]
    snap_info = None
    snap_score = 0.0
    if snap_polygon:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        dx, dy, score = snap.snap(gray, poly_px)
        snap_score = score
        if score > 0:
            poly_px = [(x + dx, y + dy) for x, y in poly_px]
            snap_info = {"dx": int(dx), "dy": int(dy), "score": float(score)}
    return {
        "sheet_name": sheet_name,
        "cell": cell_key,
        "sub_quadrant": sub_quadrant,
        "img": img,
        "body_rect": body_rect,
        "poly_px": poly_px,
        "cal_dxdy": (cal_dx, cal_dy),
        "snap_info": snap_info,
        "snap_score": snap_score,
    }


def process_rc(rc, output_dir=None, *, snap_polygon=True, draw_wms=True,
               edge_override=True, concejo: Concejo | None = None):
    """Pipeline completo. Devuelve RCBundle.

    snap_polygon: aplica snap por cross-correlation (default True).
    draw_wms: renderiza WMS catastral.
    edge_override: si el RC está cerca del borde de cell (<50m) y el snap
        del plano principal es débil, renderiza también en el plano vecino y
        elige el de mejor snap_score. Default True.
    concejo: si None, se infiere por UTM (fallback OVIEDO).
    """
    import cv2

    rc14 = geom.validate_rc(rc)
    # Pre-resuelve concejo a partir de UTM si no viene dado, para evitar
    # que geom.locate y los downstream auto-detecten cada uno.
    if concejo is None:
        X0, Y0, _ = catastro.rc_to_utm(rc14)
        concejo = get_concejo_for_utm(X0, Y0) or OVIEDO

    info = geom.locate(rc, concejo=concejo)

    out = Path(output_dir) if output_dir else Path("bundles") / info["rc"]
    out.mkdir(parents=True, exist_ok=True)

    # Polígono catastral
    poly = catastro.get_parcel_polygon(rc14)
    polygon_png = None
    snap_info = None
    edge_override_info = None
    edge_candidates_tried = []

    if poly and poly.get("polygon_utm"):
        col_letter = info["cell"].split("-")
        col = int(col_letter[0])
        row_idx = "ABCDEFGHIJKLMNOPQRSTUVWXY".index(col_letter[1])

        # Render principal
        primary = _render_on_sheet(
            info["sheet_name"], col, row_idx,
            info["sub_quadrant"], info["sub_compass"],
            poly["polygon_utm"], snap_polygon, concejo,
        )

        chosen = primary
        edge_candidates_tried = []
        # Edge override
        if edge_override and snap_polygon and primary["snap_score"] < 0.25:
            X, Y = info["utm"]
            neighbors = calibration.edge_neighbors(X, Y, concejo=concejo)
            for n in neighbors:
                try:
                    cand = _render_on_sheet(
                        n["sheet_name"], n["col"], n["row"],
                        n["sub_quadrant"], n["sub_compass"],
                        poly["polygon_utm"], snap_polygon, concejo,
                    )
                    edge_candidates_tried.append({
                        "sheet": cand["sheet_name"],
                        "score": round(cand["snap_score"], 3),
                    })
                    if cand["snap_score"] > chosen["snap_score"] + 0.05:
                        edge_override_info = {
                            "from_sheet": chosen["sheet_name"],
                            "from_score": round(chosen["snap_score"], 3),
                            "to_sheet": cand["sheet_name"],
                            "to_score": round(cand["snap_score"], 3),
                        }
                        chosen = cand
                except Exception as e:
                    edge_candidates_tried.append({
                        "sheet": n["sheet_name"], "error": str(e)[:80],
                    })

        img = chosen["img"]
        body_rect = chosen["body_rect"]
        poly_px = chosen["poly_px"]
        snap_info = chosen["snap_info"]
        if edge_override_info:
            info = dict(info)
            info["sheet_name"] = chosen["sheet_name"]
            info["cell"] = chosen["cell"]
            info["sub_quadrant"] = chosen["sub_quadrant"]

        pxs = [p[0] for p in poly_px]; pys = [p[1] for p in poly_px]
        cx_px = (min(pxs) + max(pxs)) // 2
        cy_px = (min(pys) + max(pys)) // 2

        annotated = render.draw_polygon(img, poly_px, color=(0, 0, 255), thickness=4)
        zoom = render.crop_around(annotated, cx_px, cy_px, size=900)
        polygon_png = str(out / f"{info['rc']}_polygon.png")
        cv2.imwrite(polygon_png, zoom)
    else:
        # Sin polígono: solo marcador
        pdf_path = pgou.fetch_sheet_pdf(info["sheet_name"], concejo)
        img, _, _ = render.render_pdf_page(pdf_path)
        body_rect = render.detect_body_rect(img)
        rx = info["body_relative"]["rx"]
        ry = info["body_relative"]["ry"]
        cx_px, cy_px = render.body_rel_to_pixel(rx, ry, body_rect)
        annotated = render.draw_marker(img, cx_px, cy_px)

    plan_full = str(out / f"{info['rc']}_plan_full.png")
    cv2.imwrite(plan_full, annotated)
    plan_zoom = str(out / f"{info['rc']}_plan_zoom.png")
    cv2.imwrite(plan_zoom, render.crop_around(annotated, cx_px, cy_px, 900))

    # 4) WMS catastral
    wms_png = None
    if draw_wms:
        X, Y = info["utm"]
        pad = 60
        wms_bytes = wms.get(X - pad, Y - pad, X + pad, Y + pad, w=900)
        wms_png = str(out / f"{info['rc']}_wms.png")
        Path(wms_png).write_bytes(wms_bytes)

    # 5) Contenido catastral
    content = catastro.consulta_dnprc(rc14)
    content_json = out / f"{info['rc']}_content.json"
    content_json.write_text(json.dumps(content, ensure_ascii=False, indent=2))

    # 6) Calidad esperada de la calibración para este (cell, sub)
    cal_quality = calibration.quality_for(info["cell"], info["sub_quadrant"], concejo)

    # 7) Warnings adicionales según calidad
    quality_warnings = []
    if cal_quality["reliability"] == "low":
        quality_warnings.append(
            f"Calibración débil ({cal_quality['source']}, n={cal_quality['n_labels']}): "
            f"residual esperable ±{cal_quality['expected_residual_m']} m"
        )
    elif cal_quality["reliability"] == "unknown":
        quality_warnings.append(
            f"Sin calibración local — usando bias global: residual esperable >{cal_quality['expected_residual_m']} m"
        )
    elif cal_quality["expected_residual_m"] > 2:
        quality_warnings.append(
            f"Bucket con varianza alta (σ ±{cal_quality['expected_residual_m']} m): "
            f"posible offset residual"
        )

    # 8) Metadata
    meta = {
        "rc": info["rc"],
        "address": info["address"],
        "utm": list(info["utm"]),
        "cell": info["cell"],
        "sub_quadrant": info["sub_quadrant"],
        "sub_compass": info["sub_compass"],
        "sheet_name": info["sheet_name"],
        "sheet_url": info["sheet_url"],
        "warnings": list(info["warnings"]) + quality_warnings,
        "body_relative": info["body_relative"],
        "snap": snap_info,
        "edge_override": edge_override_info,
        "edge_candidates_tried": edge_candidates_tried,
        "calibration_quality": cal_quality,
        "polygon_area_m2": (poly or {}).get("area_m2"),
        "polygon_label": (poly or {}).get("label"),
        "n_units": len(content.get("units", [])),
        "concejo": concejo.slug,
    }
    metadata_json = out / f"{info['rc']}_metadata.json"
    metadata_json.write_text(json.dumps(meta, ensure_ascii=False, indent=2))

    return RCBundle(
        rc=info["rc"],
        address=info["address"],
        utm=tuple(info["utm"]),
        sheet_name=info["sheet_name"],
        sheet_path=str(pgou.fetch_sheet_pdf(info["sheet_name"], concejo)),
        bundle_dir=str(out),
        plan_full_png=plan_full,
        plan_zoom_png=plan_zoom,
        polygon_png=polygon_png,
        wms_png=wms_png,
        content_json=str(content_json),
        metadata_json=str(metadata_json),
    )
