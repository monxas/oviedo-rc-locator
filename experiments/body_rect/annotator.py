"""Plan B (no usado en el bench original): tool de anotación manual.

Sólo necesario si `analyze_via_labels.py` no es concluyente. Lee
annot_selection.json (paso 1), renderiza a 300 DPI, muestra heurística
en azul y permite OK (1-click) o drag corners + Save.

Uso:
    .venv/bin/python annotator.py [--port 8765]

Output: body_rect_gt.json en este directorio.
"""
import argparse
import json
import os
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel
import uvicorn

from _setup import setup_paths

setup_paths()
import cv2  # noqa: E402,F401
from oviedo_rc import render  # noqa: E402
from oviedo_rc.config import CACHE_DIR as _OVIEDO_CACHE  # noqa: E402

HERE = Path(__file__).resolve().parent
CACHE_DIR = Path(_OVIEDO_CACHE)
SELECTION_FILE = HERE / "annot_selection.json"
GT_FILE = HERE / "body_rect_gt.json"
IMG_CACHE = HERE / ".annot_imgs"
IMG_CACHE.mkdir(exist_ok=True)


def load_selection():
    return json.loads(SELECTION_FILE.read_text())


def load_gt():
    if GT_FILE.exists():
        return json.loads(GT_FILE.read_text())
    return {}


def save_gt(gt):
    tmp = GT_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(gt, indent=2))
    os.replace(tmp, GT_FILE)


def render_and_cache(pdf_name):
    """Renderiza PDF a 300 DPI, cachea PNG y devuelve (path_png, W, H, heuristic_rect)."""
    pdf_path = CACHE_DIR / pdf_name
    if not pdf_path.exists():
        raise HTTPException(404, f"PDF no existe: {pdf_path}")
    png_path = IMG_CACHE / f"{pdf_name}.png"
    meta_path = IMG_CACHE / f"{pdf_name}.meta.json"
    if png_path.exists() and meta_path.exists():
        meta = json.loads(meta_path.read_text())
        return png_path, meta["W"], meta["H"], meta["heuristic_rect"]
    img, _, _ = render.render_pdf_page(str(pdf_path))
    H, W = img.shape[:2]
    rect = render.detect_body_rect(img)
    cv2.imwrite(str(png_path), img, [cv2.IMWRITE_PNG_COMPRESSION, 3])
    meta = {"W": W, "H": H, "heuristic_rect": list(rect)}
    meta_path.write_text(json.dumps(meta))
    return png_path, W, H, list(rect)


app = FastAPI()


@app.get("/", response_class=HTMLResponse)
def index():
    sel = load_selection()
    gt = load_gt()
    rows = []
    for i, pdf in enumerate(sel):
        state = gt.get(pdf)
        if state:
            action = state.get("user_action", "?")
            badge = f'<span style="color:green">✓ {action}</span>'
        else:
            badge = '<span style="color:#999">pendiente</span>'
        rows.append(
            f'<tr><td>{i + 1}</td><td><a href="/sheet/{i}">{pdf}</a></td><td>{badge}</td></tr>'
        )
    done = sum(1 for pdf in sel if pdf in gt)
    return f"""
<!doctype html><meta charset=utf-8>
<title>body_rect annotator</title>
<style>
body {{ font-family: system-ui; max-width: 800px; margin: 2em auto; }}
table {{ border-collapse: collapse; width: 100%; }}
td, th {{ padding: 6px 12px; border-bottom: 1px solid #ddd; }}
.bar {{ background: #eee; height: 8px; border-radius: 4px; overflow: hidden; }}
.bar > div {{ background: #4caf50; height: 100%; width: {done * 100 // len(sel)}%; }}
</style>
<h1>body_rect annotator</h1>
<p>Progreso: <b>{done} / {len(sel)}</b></p>
<div class="bar"><div></div></div>
<table>
<tr><th>#</th><th>PDF</th><th>Estado</th></tr>
{"".join(rows)}
</table>
<p style="margin-top:2em;color:#666">GT en <code>{GT_FILE}</code></p>
"""


@app.get("/sheet/{idx}", response_class=HTMLResponse)
def sheet(idx: int):
    sel = load_selection()
    if idx < 0 or idx >= len(sel):
        raise HTTPException(404)
    pdf = sel[idx]
    _, W, H, rect = render_and_cache(pdf)
    next_idx = idx + 1 if idx + 1 < len(sel) else None
    prev_idx = idx - 1 if idx > 0 else None
    nav = []
    if prev_idx is not None:
        nav.append(f'<a href="/sheet/{prev_idx}">← prev</a>')
    nav.append('<a href="/">index</a>')
    if next_idx is not None:
        nav.append(f'<a href="/sheet/{next_idx}">next →</a>')
    return f"""
<!doctype html><meta charset=utf-8>
<title>{pdf}</title>
<style>
body {{ font-family: system-ui; margin: 0; padding: 8px; background: #222; color: #eee; }}
.toolbar {{ position: sticky; top: 0; background: #333; padding: 8px; z-index: 10; display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }}
.toolbar button {{ padding: 6px 16px; font-size: 14px; cursor: pointer; }}
button.ok {{ background: #4caf50; color: white; border: 0; }}
button.save {{ background: #2196f3; color: white; border: 0; }}
button.skip {{ background: #777; color: white; border: 0; }}
#wrap {{ position: relative; display: inline-block; margin-top: 8px; }}
#img {{ display: block; max-width: none; }}
#overlay {{ position: absolute; left: 0; top: 0; pointer-events: none; }}
.handle {{ position: absolute; width: 18px; height: 18px; border: 2px solid yellow; background: rgba(255,255,0,0.3); border-radius: 50%; cursor: grab; transform: translate(-50%, -50%); pointer-events: auto; }}
.handle:active {{ cursor: grabbing; background: rgba(255,255,0,0.8); }}
#status {{ font-size: 12px; color: #aaa; }}
</style>
<div class=toolbar>
  <div>{" | ".join(nav)}</div>
  <div><b>{idx + 1}/{len(sel)}</b> {pdf}</div>
  <button class=ok onclick="acceptHeuristic()">OK (acepta heurística)</button>
  <button class=save onclick="saveDrag()">Save (con drag)</button>
  <button class=skip onclick="skipSheet()">Skip</button>
  <span id=status></span>
</div>
<div id=wrap>
  <img id=img src="/img/{idx}">
  <svg id=overlay></svg>
  <div class=handle id=h_tl></div>
  <div class=handle id=h_tr></div>
  <div class=handle id=h_br></div>
  <div class=handle id=h_bl></div>
</div>
<script>
const W = {W}, H = {H};
const heuristic = {rect};  // [x, y, w, h] en píxeles del PNG original
let rect = [...heuristic];  // current rect [x, y, w, h]
let scale = 1;
let dragged = false;

const img = document.getElementById('img');
const overlay = document.getElementById('overlay');
const handles = {{tl: document.getElementById('h_tl'), tr: document.getElementById('h_tr'), br: document.getElementById('h_br'), bl: document.getElementById('h_bl')}};
const wrap = document.getElementById('wrap');

function fitToWindow() {{
  const maxW = window.innerWidth - 32;
  const maxH = window.innerHeight - 100;
  const s = Math.min(maxW / W, maxH / H, 1.0);
  scale = s;
  img.style.width = (W * s) + 'px';
  img.style.height = (H * s) + 'px';
  overlay.setAttribute('width', W * s);
  overlay.setAttribute('height', H * s);
  redraw();
}}

function redraw() {{
  const [x, y, w, h] = rect;
  const sx = x * scale, sy = y * scale, sw = w * scale, sh = h * scale;
  overlay.innerHTML = `
    <rect x="${{sx}}" y="${{sy}}" width="${{sw}}" height="${{sh}}" fill="none" stroke="${{dragged ? 'yellow' : '#2196f3'}}" stroke-width="3" />
  `;
  handles.tl.style.left = sx + 'px';        handles.tl.style.top = sy + 'px';
  handles.tr.style.left = (sx + sw) + 'px'; handles.tr.style.top = sy + 'px';
  handles.br.style.left = (sx + sw) + 'px'; handles.br.style.top = (sy + sh) + 'px';
  handles.bl.style.left = sx + 'px';        handles.bl.style.top = (sy + sh) + 'px';
}}

function startDrag(corner, e) {{
  e.preventDefault();
  const onMove = ev => {{
    const r = wrap.getBoundingClientRect();
    const px = (ev.clientX - r.left) / scale;
    const py = (ev.clientY - r.top) / scale;
    let [x, y, w, h] = rect;
    let x2 = x + w, y2 = y + h;
    if (corner === 'tl') {{ x = px; y = py; }}
    else if (corner === 'tr') {{ x2 = px; y = py; }}
    else if (corner === 'br') {{ x2 = px; y2 = py; }}
    else if (corner === 'bl') {{ x = px; y2 = py; }}
    x = Math.max(0, Math.min(x, W));
    y = Math.max(0, Math.min(y, H));
    x2 = Math.max(0, Math.min(x2, W));
    y2 = Math.max(0, Math.min(y2, H));
    rect = [Math.min(x, x2), Math.min(y, y2), Math.abs(x2 - x), Math.abs(y2 - y)];
    dragged = true;
    redraw();
  }};
  const onUp = () => {{
    window.removeEventListener('mousemove', onMove);
    window.removeEventListener('mouseup', onUp);
  }};
  window.addEventListener('mousemove', onMove);
  window.addEventListener('mouseup', onUp);
}}

handles.tl.addEventListener('mousedown', e => startDrag('tl', e));
handles.tr.addEventListener('mousedown', e => startDrag('tr', e));
handles.br.addEventListener('mousedown', e => startDrag('br', e));
handles.bl.addEventListener('mousedown', e => startDrag('bl', e));

img.onload = fitToWindow;
window.addEventListener('resize', fitToWindow);
if (img.complete) fitToWindow();

async function post(action, gtRect) {{
  document.getElementById('status').textContent = 'guardando...';
  const r = await fetch('/save', {{
    method: 'POST', headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{pdf: '{pdf}', user_action: action, gt_rect: gtRect, heuristic_rect: heuristic}})
  }});
  const j = await r.json();
  document.getElementById('status').textContent = 'OK';
  if ({json.dumps(next_idx)} !== null) {{
    setTimeout(() => location.href = '/sheet/' + {json.dumps(next_idx)}, 300);
  }} else {{
    setTimeout(() => location.href = '/', 500);
  }}
}}

function acceptHeuristic() {{ post('accept_heuristic', heuristic); }}
function saveDrag() {{
  if (!dragged) {{ alert('Arrastra alguna esquina primero o usa OK'); return; }}
  post('drag', rect.map(v => Math.round(v)));
}}
function skipSheet() {{ post('skip', null); }}

document.addEventListener('keydown', e => {{
  if (e.key === 'o' || e.key === 'O') acceptHeuristic();
  else if (e.key === 's' || e.key === 'S') {{ if (dragged) saveDrag(); }}
  else if (e.key === 'n' || e.key === 'N' || e.key === 'ArrowRight') {{
    if ({json.dumps(next_idx)} !== null) location.href = '/sheet/' + {json.dumps(next_idx)};
  }}
  else if (e.key === 'p' || e.key === 'P' || e.key === 'ArrowLeft') {{
    if ({json.dumps(prev_idx)} !== null) location.href = '/sheet/' + {json.dumps(prev_idx)};
  }}
}});
</script>
"""


@app.get("/img/{idx}")
def img(idx: int):
    sel = load_selection()
    if idx < 0 or idx >= len(sel):
        raise HTTPException(404)
    png_path, _, _, _ = render_and_cache(sel[idx])
    return Response(content=png_path.read_bytes(), media_type="image/png")


class SaveReq(BaseModel):
    pdf: str
    user_action: str
    gt_rect: list | None = None
    heuristic_rect: list


@app.post("/save")
def save(req: SaveReq):
    gt = load_gt()
    gt[req.pdf] = {
        "user_action": req.user_action,
        "gt_rect": req.gt_rect,
        "heuristic_rect": req.heuristic_rect,
        "ts": time.time(),
    }
    save_gt(gt)
    return JSONResponse({"ok": True, "count": len(gt)})


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="0.0.0.0")
    args = ap.parse_args()
    print(f"GT file: {GT_FILE}")
    print(f"Selection: {SELECTION_FILE}")
    print(f"Listening on http://{args.host}:{args.port}/")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
