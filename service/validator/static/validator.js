// ----- token handling -----
// Si viene en ?token=... lo guardamos en localStorage y limpiamos la URL.
// La URL queda sólo con ?rc=... para compartir.
function readQuery() {
  const q = new URLSearchParams(location.search);
  const qstr = q.get('queue') || '';
  const queue = qstr.split(',').map(s => s.trim().toUpperCase()).filter(Boolean);
  return { token: q.get('token'), rc: (q.get('rc') || '').toUpperCase() || null, queue };
}
let __queue = [];
let __queue_total = 0;
(() => {
  const { token, rc } = readQuery();
  if (token) {
    localStorage.setItem('iarq_validator_token', token);
    const url = new URL(location.href);
    url.searchParams.delete('token');
    history.replaceState({}, '', url.toString());
  }
  if (!localStorage.getItem('iarq_validator_token')) {
    document.getElementById('token-modal').classList.add('show');
  }
})();
function saveToken() {
  const t = document.getElementById('token-input').value.trim();
  if (!t) return;
  localStorage.setItem('iarq_validator_token', t);
  document.getElementById('token-modal').classList.remove('show');
  loadInitial();
}
function getToken() { return localStorage.getItem('iarq_validator_token') || ''; }

let current = null;
let viewMpx = 0.75;
// dragVec in CROP-NATIVE pixels (polygon user-correction).
let dragVec = { dx: 0, dy: 0 };
// dragVec separado para el panel ficha (en ficha-native px).
let dragVecFicha = { dx: 0, dy: 0 };
// Per-pane viewport state. scale = mPerPxNative / viewMpx.
const VP = {
  crop:  { panX: 0, panY: 0, scale: 1, inner: null, wrap: null, img: null, mPerPxNative: 0, nativeSize: 0 },
  wms:   { panX: 0, panY: 0, scale: 1, inner: null, wrap: null, img: null, mPerPxNative: 0, nativeSize: 0 },
  ficha: { panX: 0, panY: 0, scale: 1, inner: null, wrap: null, img: null, mPerPxNative: 0, nativeSize: 0, nativeSizeH: 0 },
};

function initVP() {
  VP.crop.wrap  = document.querySelector('.crop-pane .canvas-wrap');
  VP.crop.inner = document.getElementById('crop-inner');
  VP.crop.img   = document.getElementById('crop');
  VP.wms.wrap   = document.querySelector('.wms-pane .canvas-wrap');
  VP.wms.inner  = document.getElementById('wms-inner');
  VP.wms.img    = document.getElementById('wms');
  VP.ficha.wrap  = document.querySelector('.ficha-pane .canvas-wrap');
  VP.ficha.inner = document.getElementById('ficha-inner');
  VP.ficha.img   = document.getElementById('ficha');
}

function api(path, opts={}) {
  opts.headers = opts.headers || {};
  const t = getToken();
  if (t) opts.headers['Authorization'] = 'Bearer ' + t;
  return fetch(path, opts);
}
function imgUrl(url) {
  // las imágenes <img> no llevan headers, así que añadimos token como query
  const t = getToken();
  return url + (t ? (url.includes('?') ? '&' : '?') + 'token=' + encodeURIComponent(t) : '');
}

// ----- Transform-based viewport -----
let rafPending = false;
function scheduleTransform() {
  if (rafPending) return;
  rafPending = true;
  requestAnimationFrame(() => {
    rafPending = false;
    applyTransform();
  });
}
function applyTransform() {
  for (const pane of [VP.crop, VP.wms, VP.ficha]) {
    if (!pane.inner || !pane.mPerPxNative) continue;
    pane.scale = pane.mPerPxNative / viewMpx;
    pane.inner.style.transform = `translate(${pane.panX}px, ${pane.panY}px) scale(${pane.scale})`;
  }
  const green = document.getElementById('poly-green');
  if (green) green.setAttribute('transform', `translate(${dragVec.dx} ${dragVec.dy})`);
  const greenF = document.getElementById('poly-green-ficha');
  if (greenF) greenF.setAttribute('transform', `translate(${dragVecFicha.dx} ${dragVecFicha.dy})`);
  const zl = document.getElementById('zoom-label');
  if (zl) zl.textContent = viewMpx.toFixed(2) + ' m/px';
  const dEl = document.getElementById('drag_dxdy');
  if (dEl) {
    dEl.textContent = dragVec.dx + ', ' + dragVec.dy;
    dEl.className = (dragVec.dx || dragVec.dy) ? 'dragged-indicator' : '';
  }
}

function paneScale(pane) {
  return pane.mPerPxNative ? (pane.mPerPxNative / viewMpx) : 1;
}

function centerPaneOnPoint(pane, ptNativeX, ptNativeY) {
  if (!pane.wrap) return;
  const wRect = pane.wrap.getBoundingClientRect();
  const s = paneScale(pane);
  pane.panX = wRect.width / 2 - ptNativeX * s;
  pane.panY = wRect.height / 2 - ptNativeY * s;
}

function polyCentroidNative() {
  if (!current) return { x: 0, y: 0 };
  const pts = current.poly_snap;
  let cx = 0, cy = 0;
  for (const [x, y] of pts) { cx += x; cy += y; }
  return { x: cx / pts.length, y: cy / pts.length };
}

function polyBBoxNative() {
  if (!current) return null;
  let minX=Infinity,maxX=-Infinity,minY=Infinity,maxY=-Infinity;
  for (const [x, y] of current.poly_snap) {
    if (x<minX) minX=x; if (x>maxX) maxX=x;
    if (y<minY) minY=y; if (y>maxY) maxY=y;
  }
  return { minX, maxX, minY, maxY };
}

function renderOverlay() {
  if (!current) return;
  const svg = document.getElementById('overlay');
  const W = current.crop_size_px;
  // SVG overlay sits inside crop-inner; size = native crop. It scales with crop-inner's transform.
  svg.setAttribute('viewBox', '0 0 ' + W + ' ' + W);
  svg.style.width = W + 'px';
  svg.style.height = W + 'px';

  const poly = current.poly_snap.map(p => p.join(',')).join(' ');
  // Build once; we won't rebuild per-frame (drag updates transform attribute only).
  // stroke-width stays in native px so on-screen thickness scales with zoom; that's
  // acceptable (line gets thinner when zoomed in) and avoids extra work per frame.
  svg.innerHTML = `
    <polygon points="${poly}" fill="rgba(220,40,40,0.18)" stroke="#dc2828" stroke-width="3" />
    <polygon class="draggable" id="poly-green" points="${poly}" fill="rgba(40,200,80,0.22)" stroke="#22c55e" stroke-width="3"
             transform="translate(${dragVec.dx} ${dragVec.dy})" />
  `;

  // WMS overlay: dibuja el polígono catastral sobre la referencia para
  // identificar el edificio buscado entre parcelas similares.
  const svgWms = document.getElementById('overlay-wms');
  if (svgWms && current.poly_wms && current.poly_wms.length) {
    const Ww = current.wms_size_px;
    svgWms.setAttribute('viewBox', '0 0 ' + Ww + ' ' + Ww);
    svgWms.style.width = Ww + 'px';
    svgWms.style.height = Ww + 'px';
    const polyWmsStr = current.poly_wms.map(p => p.join(',')).join(' ');
    svgWms.innerHTML = `<polygon points="${polyWmsStr}" fill="rgba(220,40,40,0.22)" stroke="#dc2828" stroke-width="2.5" />`;
  } else if (svgWms) {
    svgWms.innerHTML = '';
  }

  // Ficha overlay (Phase 2c): rojo estático + verde drag-able sobre el plano de la ficha.
  const svgF = document.getElementById('overlay-ficha');
  if (svgF && current.poly_ficha && current.poly_ficha.length) {
    const Wf = current.ficha_size_px;
    const Hf = current.ficha_size_px_h || Wf;
    svgF.setAttribute('viewBox', '0 0 ' + Wf + ' ' + Hf);
    svgF.style.width = Wf + 'px';
    svgF.style.height = Hf + 'px';
    const polyFStr = current.poly_ficha.map(p => p.join(',')).join(' ');
    svgF.innerHTML = `
      <polygon points="${polyFStr}" fill="rgba(220,40,40,0.18)" stroke="#dc2828" stroke-width="3" />
      <polygon class="draggable" id="poly-green-ficha" points="${polyFStr}" fill="rgba(40,200,80,0.22)" stroke="#22c55e" stroke-width="3"
               transform="translate(${dragVecFicha.dx} ${dragVecFicha.dy})" />
    `;
  } else if (svgF) {
    svgF.innerHTML = '';
  }
}

// ----- Initial zoom & centering on RC load -----
function computeInitialZoom() {
  if (!current) return 0.75;
  const bb = polyBBoxNative();
  if (!bb) return 0.75;
  const spanXm = (bb.maxX - bb.minX) * current.crop_m_per_px;
  const spanYm = (bb.maxY - bb.minY) * current.crop_m_per_px;
  const wRect = VP.crop.wrap.getBoundingClientRect();
  const wPx = wRect.width || 1, hPx = wRect.height || 1;
  // viewMpx so polygon fits with ~30% margin (1.3 multiplier).
  const needX = (spanXm * 1.3) / wPx;
  const needY = (spanYm * 1.3) / hPx;
  const need = Math.max(needX, needY, 0.1);
  return Math.max(need, 0.5);  // never zoom in too much initially
}

function polyCentroidFichaNative() {
  if (!current || !current.poly_ficha || !current.poly_ficha.length) return null;
  let cx = 0, cy = 0;
  for (const [x, y] of current.poly_ficha) { cx += x; cy += y; }
  return { x: cx / current.poly_ficha.length, y: cy / current.poly_ficha.length };
}

function recenterFicha() {
  if (!VP.ficha.nativeSize || !VP.ficha.wrap) return;
  const fc = polyCentroidFichaNative();
  if (!fc) return;
  centerPaneOnPoint(VP.ficha, fc.x + dragVecFicha.dx, fc.y + dragVecFicha.dy);
}

function recenterAfterLoad() {
  if (!current) return;
  viewMpx = computeInitialZoom();
  const zr = document.getElementById('zoom');
  if (zr) zr.value = viewMpx;
  const c = polyCentroidNative();
  centerPaneOnPoint(VP.crop, c.x + dragVec.dx, c.y + dragVec.dy);
  if (VP.wms.nativeSize) {
    centerPaneOnPoint(VP.wms, VP.wms.nativeSize / 2, VP.wms.nativeSize / 2);
  }
  recenterFicha();
  scheduleTransform();
}

function setZoom(v) {
  viewMpx = v;
  const zr = document.getElementById('zoom');
  if (zr) zr.value = v;
  scheduleTransform();
}

document.getElementById('zoom').addEventListener('input', e => {
  viewMpx = parseFloat(e.target.value);
  const c = polyCentroidNative();
  centerPaneOnPoint(VP.crop, c.x + dragVec.dx, c.y + dragVec.dy);
  if (VP.wms.nativeSize) {
    centerPaneOnPoint(VP.wms, VP.wms.nativeSize / 2, VP.wms.nativeSize / 2);
  }
  recenterFicha();
  scheduleTransform();
});

window.addEventListener('resize', () => {
  // keep polygon centred when viewport size changes
  if (!current) return;
  const c = polyCentroidNative();
  centerPaneOnPoint(VP.crop, c.x + dragVec.dx, c.y + dragVec.dy);
  if (VP.wms.nativeSize) {
    centerPaneOnPoint(VP.wms, VP.wms.nativeSize / 2, VP.wms.nativeSize / 2);
  }
  recenterFicha();
  scheduleTransform();
});

// ----- Unified pointer state machine -----
// kind ∈ {'drag-poly', 'pan-pane'}
const pointers = new Map();
let pinch = null;   // { startDist, startZoom, midPane, worldX, worldY, midX, midY }
let lastTap = { t: 0, x: 0, y: 0 };

function paneFromPoint(clientX, clientY) {
  for (const key of ['crop', 'wms', 'ficha']) {
    const pane = VP[key];
    if (!pane.wrap) continue;
    const r = pane.wrap.getBoundingClientRect();
    if (clientX >= r.left && clientX <= r.right && clientY >= r.top && clientY <= r.bottom) {
      return pane;
    }
  }
  return null;
}

function startPinch() {
  const pts = [...pointers.values()];
  if (pts.length < 2) return;
  const midX = (pts[0].x + pts[1].x) / 2;
  const midY = (pts[0].y + pts[1].y) / 2;
  const dist = Math.hypot(pts[0].x - pts[1].x, pts[0].y - pts[1].y);
  const pane = paneFromPoint(midX, midY) || VP.crop;
  const r = pane.wrap.getBoundingClientRect();
  const s = paneScale(pane);
  // world coords (native px) of the midpoint in that pane
  const worldX = (midX - r.left - pane.panX) / s;
  const worldY = (midY - r.top  - pane.panY) / s;
  pinch = {
    startDist: dist || 1,
    startZoom: viewMpx,
    midPane: pane,
    worldX, worldY,
    midX, midY,
    // snapshot panX/Y de ambos panes para sync solidario
    startCrop: { panX: VP.crop.panX, panY: VP.crop.panY },
    startWms:  { panX: VP.wms.panX,  panY: VP.wms.panY  },
  };
  for (const p of pointers.values()) {
    if (p.kind === 'drag-poly' || p.kind === 'pan-pane') p.kind = 'pinch';
  }
}

function updatePinch() {
  const pts = [...pointers.values()];
  if (pts.length < 2 || !pinch) return;
  const dx = pts[0].x - pts[1].x, dy = pts[0].y - pts[1].y;
  const dist = Math.hypot(dx, dy) || 1;
  const midX = (pts[0].x + pts[1].x) / 2;
  const midY = (pts[0].y + pts[1].y) / 2;
  const ratio = dist / pinch.startDist;
  // bigger dist → zoom IN → lower viewMpx
  viewMpx = Math.max(0.05, Math.min(2.0, pinch.startZoom / ratio));
  const zr = document.getElementById('zoom');
  if (zr) zr.value = viewMpx;
  // Pane activo: ancla world point bajo midpoint.
  const active = pinch.midPane;
  const r = active.wrap.getBoundingClientRect();
  const sActive = active.mPerPxNative / viewMpx;
  const newActivePanX = (midX - r.left) - pinch.worldX * sActive;
  const newActivePanY = (midY - r.top)  - pinch.worldY * sActive;
  // Delta del pane activo respecto al snapshot inicial del pinch (en pixels display).
  const startActive = (active === VP.crop) ? pinch.startCrop : pinch.startWms;
  const dPanX = newActivePanX - startActive.panX;
  const dPanY = newActivePanY - startActive.panY;
  active.panX = newActivePanX;
  active.panY = newActivePanY;
  // Pane "el otro": replicar exactamente el mismo delta de display px.
  // Como ambos comparten viewMpx, el on-screen movement coincide → sincronización solidaria.
  const other = (active === VP.crop) ? VP.wms : VP.crop;
  const startOther = (other === VP.crop) ? pinch.startCrop : pinch.startWms;
  other.panX = startOther.panX + dPanX;
  other.panY = startOther.panY + dPanY;
  scheduleTransform();
}

function panBoth(dx, dy) {
  VP.crop.panX += dx; VP.crop.panY += dy;
  VP.wms.panX  += dx; VP.wms.panY  += dy;
}

function isOnGreen(target) {
  if (!target) return null;
  if (target.id === 'poly-green' || (target.closest && target.closest('#poly-green'))) return 'crop';
  if (target.id === 'poly-green-ficha' || (target.closest && target.closest('#poly-green-ficha'))) return 'ficha';
  return null;
}

function paneKeyFromWrap(wrap) {
  if (!wrap) return null;
  if (wrap === VP.crop.wrap || wrap === VP.wms.wrap) return 'sync';   // crop+wms solidarios
  if (wrap === VP.ficha.wrap) return 'ficha';
  return null;
}

function onPointerDown(e) {
  // Only handle inside a canvas-wrap; ignore buttons/inputs.
  if (e.target.closest && e.target.closest('button, input, #token-modal, #mobile-actions, .zoom-bar')) return;
  const wrap = e.target.closest && e.target.closest('.canvas-wrap');
  if (!wrap) return;
  try { wrap.setPointerCapture(e.pointerId); } catch {}
  const onGreen = isOnGreen(e.target);
  const paneKey = paneKeyFromWrap(wrap);
  const entry = {
    id: e.pointerId,
    x: e.clientX, y: e.clientY,
    startX: e.clientX, startY: e.clientY,
    startPan: null,
    kind: 'pan-pane',
    wrap,
    paneKey,
  };
  if (onGreen && pointers.size === 0) {
    entry.kind = 'drag-poly';
    entry.dragTarget = onGreen;   // 'crop' o 'ficha'
    entry.startDragVec = (onGreen === 'ficha')
      ? { dx: dragVecFicha.dx, dy: dragVecFicha.dy }
      : { dx: dragVec.dx,      dy: dragVec.dy };
  } else {
    entry.kind = 'pan-pane';
    entry.startPanCrop  = { x: VP.crop.panX,  y: VP.crop.panY  };
    entry.startPanWms   = { x: VP.wms.panX,   y: VP.wms.panY   };
    entry.startPanFicha = { x: VP.ficha.panX, y: VP.ficha.panY };
  }
  pointers.set(e.pointerId, entry);

  if (pointers.size === 2) {
    startPinch();
  } else if (pointers.size === 1 && entry.kind === 'pan-pane') {
    // double-tap detection (only for single pointers, not on polygon)
    const now = Date.now();
    const dt = now - lastTap.t;
    const dx = e.clientX - lastTap.x;
    const dy = e.clientY - lastTap.y;
    if (dt < 300 && Math.hypot(dx, dy) < 40) {
      // double-tap → fit polygon
      doubleTapFit();
      lastTap = { t: 0, x: 0, y: 0 };
    } else {
      lastTap = { t: now, x: e.clientX, y: e.clientY };
    }
  }
  e.preventDefault();
}

function onPointerMove(e) {
  const entry = pointers.get(e.pointerId);
  if (!entry) return;
  entry.x = e.clientX;
  entry.y = e.clientY;

  if (pointers.size >= 2 && pinch) {
    updatePinch();
    e.preventDefault();
    return;
  }

  if (entry.kind === 'drag-poly') {
    const target = entry.dragTarget === 'ficha' ? VP.ficha : VP.crop;
    const s = paneScale(target) || 1;
    const nx = Math.round((e.clientX - entry.startX) / s + entry.startDragVec.dx);
    const ny = Math.round((e.clientY - entry.startY) / s + entry.startDragVec.dy);
    if (entry.dragTarget === 'ficha') { dragVecFicha.dx = nx; dragVecFicha.dy = ny; }
    else                              { dragVec.dx      = nx; dragVec.dy      = ny; }
    scheduleTransform();
    e.preventDefault();
  } else if (entry.kind === 'pan-pane') {
    const dx = e.clientX - entry.startX;
    const dy = e.clientY - entry.startY;
    if (entry.paneKey === 'ficha') {
      VP.ficha.panX = entry.startPanFicha.x + dx;
      VP.ficha.panY = entry.startPanFicha.y + dy;
    } else {
      VP.crop.panX = entry.startPanCrop.x + dx;
      VP.crop.panY = entry.startPanCrop.y + dy;
      VP.wms.panX  = entry.startPanWms.x  + dx;
      VP.wms.panY  = entry.startPanWms.y  + dy;
    }
    scheduleTransform();
    e.preventDefault();
  }
}

function onPointerEnd(e) {
  pointers.delete(e.pointerId);
  if (pointers.size < 2) pinch = null;
  if (pointers.size === 1) {
    // Refresh the surviving pointer's baselines so subsequent move math is correct.
    const [only] = [...pointers.values()];
    only.startX = only.x;
    only.startY = only.y;
    if (only.kind === 'pinch') {
      // demote: choose pan-pane (safer than drag-poly mid-gesture)
      only.kind = 'pan-pane';
    }
    only.startPanCrop  = { x: VP.crop.panX,  y: VP.crop.panY  };
    only.startPanWms   = { x: VP.wms.panX,   y: VP.wms.panY   };
    only.startPanFicha = { x: VP.ficha.panX, y: VP.ficha.panY };
    only.startDragVec  = (only.dragTarget === 'ficha')
      ? { dx: dragVecFicha.dx, dy: dragVecFicha.dy }
      : { dx: dragVec.dx, dy: dragVec.dy };
  }
}

function doubleTapFit() {
  if (!current) return;
  recenterAfterLoad();
}

function attachPointerHandlers() {
  // Attach to both wraps so events are scoped to canvas areas; capture ensures
  // we still get events if the finger drifts out.
  for (const key of ['crop', 'wms', 'ficha']) {
    const wrap = VP[key].wrap;
    if (!wrap) continue;
    wrap.addEventListener('pointerdown', onPointerDown);
    wrap.addEventListener('pointermove', onPointerMove);
    wrap.addEventListener('pointerup', onPointerEnd);
    wrap.addEventListener('pointercancel', onPointerEnd);
  }
}

async function loadRC(rc) {
  dragVec.dx = 0; dragVec.dy = 0;
  dragVecFicha.dx = 0; dragVecFicha.dy = 0;
  const dragEl = document.getElementById('drag_dxdy');
  if (dragEl) { dragEl.textContent = '0, 0'; dragEl.className = ''; }
  document.getElementById('rc').textContent = 'cargando…';
  const r = await api('/api/next' + (rc ? '?rc=' + encodeURIComponent(rc) : ''));
  if (r.status === 401) {
    localStorage.removeItem('iarq_validator_token');
    document.getElementById('token-modal').classList.add('show');
    return;
  }
  if (!r.ok) { alert('error ' + r.status); return; }
  const data = await r.json();
  current = data;
  // pane native sizes & m/px for transform math
  VP.crop.mPerPxNative = data.crop_m_per_px;
  VP.crop.nativeSize   = data.crop_size_px;
  VP.wms.mPerPxNative  = data.wms_m_per_px;
  VP.wms.nativeSize    = data.wms_size_px;
  // actualizar URL con ?rc= (sin recargar)
  const url = new URL(location.href);
  url.searchParams.set('rc', data.rc);
  history.replaceState({}, '', url.toString());

  document.getElementById('rc').textContent = data.rc;
  document.getElementById('addr').textContent = data.address || '—';
  document.getElementById('cell').textContent = data.cell + '-' + data.sub_quadrant;
  // info panel (sólo visible en desktop)
  const setText = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
  setText('snap_score', data.snap_score.toFixed(3));
  setText('snap_dxdy', data.snap_dxdy.join(', '));
  setText('cal_dxdy', data.cal_dxdy.join(', '));
  const q = data.calibration_quality || {};
  setText('reliability', q.reliability || '—');
  setText('n_labels', q.n_labels || '—');
  setText('err_m', q.expected_residual_m ? q.expected_residual_m.toFixed(2) + ' m' : '—');


  const banner = document.getElementById('snap-banner');
  if (data.snap_confident) {
    banner.className = 'banner ok'; banner.textContent = 'snap ' + data.snap_score.toFixed(2);
  } else {
    banner.className = 'banner warn'; banner.textContent = '⚠ INCIERTO ' + data.snap_score.toFixed(2);
  }
  // Reset transform: imgs keep their native size; we only translate/scale via CSS.
  const cropImg = document.getElementById('crop');
  const wmsImg = document.getElementById('wms');
  // ensure imgs are not constrained by old style.width/height
  cropImg.style.width = ''; cropImg.style.height = '';
  wmsImg.style.width  = ''; wmsImg.style.height  = '';
  renderOverlay();

  let loaded = 0;
  const done = () => {
    loaded += 1;
    if (loaded < 2) return;
    requestAnimationFrame(() => requestAnimationFrame(() => {
      recenterAfterLoad();
      if (typeof setBusy === 'function') setBusy(false);
      if (typeof prefetchNext === 'function') prefetchNext();
    }));
  };
  cropImg.onload = done;
  wmsImg.onload = done;
  cropImg.src = imgUrl(data.crop_url);
  wmsImg.src = imgUrl(data.wms_url);

  // Ficha plano (4º panel) — opcional
  const mainEl = document.querySelector('main');
  const fichaImg = document.getElementById('ficha');
  const fichaEt = document.getElementById('ficha-etiqueta');
  if (data.ficha_url) {
    if (mainEl) mainEl.classList.add('has-ficha');
    if (fichaEt) fichaEt.textContent = data.ficha_etiqueta || '—';
    VP.ficha.mPerPxNative = data.ficha_m_per_px || 0.127;
    VP.ficha.nativeSize   = data.ficha_size_px || 0;
    VP.ficha.nativeSizeH  = data.ficha_size_px_h || data.ficha_size_px || 0;
    if (fichaImg) {
      fichaImg.style.width = ''; fichaImg.style.height = '';
      fichaImg.onload = () => {
        // centrar el polígono catastral en el viewport del panel ficha
        if (current && current.poly_ficha && current.poly_ficha.length) {
          let cx = 0, cy = 0;
          for (const [x, y] of current.poly_ficha) { cx += x; cy += y; }
          cx /= current.poly_ficha.length; cy /= current.poly_ficha.length;
          centerPaneOnPoint(VP.ficha, cx, cy);
          scheduleTransform();
        }
      };
      fichaImg.src = imgUrl(data.ficha_url);
    }
  } else {
    if (mainEl) mainEl.classList.remove('has-ficha');
    if (fichaImg) fichaImg.removeAttribute('src');
    if (fichaEt) fichaEt.textContent = '—';
    VP.ficha.nativeSize = 0;
  }
  loadStats();
  prefetchNext();
}

// Prefetch del próximo RC de la cola: trigger del backend + warm-up imágenes.
// Sin await — corre en background para no bloquear la UI actual.
function prefetchNext() {
  if (!__queue.length) return;
  const next = __queue[0];  // peek sin shift
  if (window.__prefetched === next) return;
  window.__prefetched = next;
  api('/api/next?rc=' + encodeURIComponent(next))
    .then(r => r.ok ? r.json() : null)
    .then(d => {
      if (!d) return;
      // El navegador cachea las imágenes con esta carga "fantasma"
      const i1 = new Image(); i1.src = imgUrl(d.crop_url);
      const i2 = new Image(); i2.src = imgUrl(d.wms_url);
    })
    .catch(() => { window.__prefetched = null; });
}

function loadInitial() {
  const { rc, queue } = readQuery();
  if (queue && queue.length) {
    __queue = queue.slice();
    __queue_total = __queue.length;
    const first = __queue.shift();
    loadRC(first);
    return;
  }
  loadRC(rc);
}
function queueIndicator() {
  if (!__queue_total) return '';
  const done = __queue_total - __queue.length;
  return ` · cola ${done}/${__queue_total}`;
}

async function loadStats() {
  const r = await api('/api/stats');
  if (!r.ok) return;
  const d = await r.json();
  const next = d.recal_threshold - (d.accept_counter || 0);
  document.getElementById('stats').textContent =
    d.total + ' · ' + d.pending + ' pend. · recal en ' + next + queueIndicator();
  const f = document.getElementById('footer-stats');
  if (f) f.textContent = JSON.stringify(d.by_action || {});
}

let _submitting = false;
function setBusy(on) {
  _submitting = on;
  const overlay = document.getElementById('busy-overlay');
  if (overlay) overlay.style.display = on ? 'flex' : 'none';
  ['btn-accept-d','btn-accept-m','btn-reject-d','btn-reject-m','btn-skip-d','btn-skip-m'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.disabled = on;
  });
}

async function submit(action) {
  if (_submitting) return;             // evita doble-click
  if (!current) return;
  setBusy(true);
  const payload = {
    rc: current.rc,
    action: action,
    dxdy: action === 'accept' ? [dragVec.dx, dragVec.dy] : [0, 0],
    snap_score: current.snap_score,
    snap_dxdy: current.snap_dxdy,
    cal_dxdy: current.cal_dxdy,
    ficha_dxdy: action === 'accept' ? [dragVecFicha.dx, dragVecFicha.dy] : [0, 0],
    ficha_etiqueta: current.ficha_etiqueta || null,
    ficha_filename: current.ficha_filename || null,
    ficha_cal_dxdy: [0, 0],   // por ahora siempre 0; cuando ficha_plano aplique offset, leerlo aquí
  };
  let r;
  try {
    r = await api('/api/label', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
  } catch (e) {
    setBusy(false);
    alert('error de red: ' + e.message);
    return;
  }
  if (!r.ok) { setBusy(false); alert('error guardando (' + r.status + ')'); return; }
  const resp = await r.json();
  if (resp.recalibrated) {
    // Recal corre out-of-band (sentinel data/.recal_pending). Las offsets nuevas
    // se aplican vía mtime-reload sin reiniciar el servicio, así que sólo
    // mostramos un toast breve y seguimos.
    document.getElementById('rc').textContent = '♻ recal en curso (fondo)';
  }
  // siguiente RC. setBusy(false) lo gestiona loadRC al terminar.
  if (__queue.length) {
    loadRC(__queue.shift());
  } else {
    if (__queue_total > 0) {
      const el = document.getElementById('rc');
      if (el) el.textContent = '✓ cola completada (' + __queue_total + ')';
      __queue_total = 0;
    }
    loadRC(null);
  }
}

['btn-accept-d', 'btn-accept-m'].forEach(id => { const el = document.getElementById(id); if (el) el.onclick = () => submit('accept'); });
['btn-reject-d', 'btn-reject-m'].forEach(id => { const el = document.getElementById(id); if (el) el.onclick = () => submit('reject_unfixable'); });
['btn-skip-d', 'btn-skip-m'].forEach(id => { const el = document.getElementById(id); if (el) el.onclick = () => submit('skip'); });

window.addEventListener('keydown', e => {
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
  if (e.key === 'a' || e.key === 'A' || e.key === 'Enter') submit('accept');
  else if (e.key === 'x' || e.key === 'X') submit('reject_unfixable');
  else if (e.key === 's' || e.key === 'S') submit('skip');
});

initVP();
attachPointerHandlers();
if (getToken()) loadInitial();
