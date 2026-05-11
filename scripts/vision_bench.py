"""
Benchmark: GPT-5.5 (via OpenClaw OAuth) vs Florence-2 (local Mac mini).

Tarea: dado un crop PGOU con polígono catastral dibujado en la posición SNAP,
predecir el offset (dx, dy) necesario para que el polígono encaje en el edificio
real del plano. Ground truth = drag manual del usuario en validator_labels.json.

GPT pipeline:  imagen + prompt JSON → respuesta {dx, dy, aligned}
Florence pipeline: imagen + grounding prompt → bbox edificio cercano → offset

Output: tabla por RC con (gpt_dx, gpt_dy, gpt_ms, flor_dx, flor_dy, flor_ms, truth_dx, truth_dy, errores)
"""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path.home() / "oviedo-rc-locator"
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "service"))

import cv2
import numpy as np
from PIL import Image, ImageDraw

# Reusa el pipeline del validator
from validator_ui import _generate_for_rc, DISPLAY_CROP


TEST_RCS = [
    # (RC, ground_truth_dxdy_display_px)  ← dxdy son píxeles de display (DISPLAY_CROP=1800)
    ("3750004TP6035S0001AA", [107, 14]),
    ("3268412TP7036N0001AA", [-93, 12]),
    ("9135404TP6093S0001AA", [74, -36]),
    ("1461116TP7016S0001AA", [-46, 24]),
    ("9954005TP6095S0001AA", [-45, 24]),
]

OUTDIR = Path("/tmp/vision_bench")
OUTDIR.mkdir(exist_ok=True)


def render_for_gpt(data: dict, out_path: Path):
    """PGOU crop con polígono RED dibujado en snap position. PNG."""
    img = Image.open(__import__("io").BytesIO(data["crop_png"])).convert("RGB")
    draw = ImageDraw.Draw(img, "RGBA")
    pts = [tuple(p) for p in data["poly_snap"]]
    # rojo translúcido + borde rojo grueso
    draw.polygon(pts, fill=(220, 40, 40, 50), outline=(220, 40, 40))
    # borde extra grueso por encima
    pts_closed = pts + [pts[0]]
    for i in range(len(pts_closed) - 1):
        draw.line([pts_closed[i], pts_closed[i+1]], fill=(220, 40, 40), width=8)
    img.save(out_path, "PNG", optimize=True)


def call_gpt(image_path: Path) -> dict:
    """Llama a GPT-5.5 vía OpenClaw CLI con prompt JSON."""
    prompt = (
        "Estás viendo un plano urbanístico (PGOU) con un polígono rojo dibujado "
        "encima. El polígono debería estar perfectamente alineado con el contorno "
        "de un edificio del plano. "
        "Tu tarea: determinar si el polígono está bien alineado, y si no, calcular "
        "el offset (dx, dy) en píxeles para moverlo al edificio correcto más cercano. "
        "La imagen es 1800x1800. dx positivo = derecha. dy positivo = abajo. "
        "Devuelve EXCLUSIVAMENTE JSON sin texto extra:\n"
        '{"aligned": bool, "dx": int, "dy": int, "confidence": 0-1, "reasoning": "..."}\n'
        "Si crees que ya está bien, dx=0, dy=0, aligned=true."
    )
    t0 = time.time()
    r = subprocess.run([
        str(Path.home() / ".npm-global/bin/openclaw"),
        "capability", "image", "describe",
        "--file", str(image_path),
        "--prompt", prompt,
        "--model", "openai-codex/gpt-5.5",
        "--json",
        "--timeout-ms", "60000",
    ], capture_output=True, text=True, timeout=120)
    elapsed_ms = int((time.time() - t0) * 1000)
    if r.returncode != 0:
        return {"error": r.stderr[:300], "ms": elapsed_ms}
    try:
        data = json.loads(r.stdout)
        # extraer el campo .outputs[0].text
        text = data["outputs"][0]["text"]
        # buscar JSON en el texto
        import re
        m = re.search(r"\{[^{}]*\"aligned\".*?\}", text, re.DOTALL)
        if not m:
            return {"error": f"no JSON in text: {text[:200]}", "ms": elapsed_ms, "raw": text}
        parsed = json.loads(m.group(0))
        return {**parsed, "ms": elapsed_ms, "raw": text[:300]}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}", "ms": elapsed_ms, "raw": r.stdout[:300]}


def main():
    results = []
    for rc, truth_dxdy in TEST_RCS:
        print(f"\n=== {rc} (truth dxdy={truth_dxdy}) ===", flush=True)
        try:
            data = _generate_for_rc(rc)
        except Exception as e:
            print(f"  ERR generate: {e}")
            continue
        # imagen para GPT (con polígono rojo dibujado)
        img_path = OUTDIR / f"{rc}.png"
        render_for_gpt(data, img_path)
        print(f"  img={img_path}  size={img_path.stat().st_size//1024}KB", flush=True)
        # GPT call
        gpt = call_gpt(img_path)
        if "error" in gpt:
            print(f"  GPT ERR: {gpt['error'][:200]}")
        else:
            print(f"  GPT  dx={gpt.get('dx')}  dy={gpt.get('dy')}  aligned={gpt.get('aligned')}  conf={gpt.get('confidence')}  ms={gpt['ms']}")
            print(f"       reasoning: {gpt.get('reasoning', '')[:120]}")
        results.append({
            "rc": rc,
            "truth_dxdy": truth_dxdy,
            "snap_dxdy": data["snap_dxdy"],
            "cal_dxdy": data["cal_dxdy"],
            "gpt": gpt,
        })
    # guardar
    out = OUTDIR / "results.json"
    out.write_text(json.dumps(results, indent=2, ensure_ascii=False))
    print(f"\nGuardado: {out}")


if __name__ == "__main__":
    main()
