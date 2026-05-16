# body_rect bench (2026-05-16)

Experimento aislado para evaluar alternativas al detector `render.detect_body_rect`
de Oviedo RC locator. **Resultado: NO desplegar.**

## Veredicto

La heurística actual + calibración v7 (`data/calibration_offsets.json`, 114 buckets)
es **óptima** medida contra los 1261 labels `accept` de `data/validator_labels.json`.

```
método           raw_err_med  residual_med  residual_p90  residual_mean
heuristic              92.85          0.00*        34.21         13.47
heuristic_v2           93.23          0.71         35.00         14.16
hough                 184.97         32.70        127.88         54.95
template              167.46         15.78         44.49         32.77
```

`*` mediana 0 es parcialmente tautológica (truth definido desde raw_heuristic),
pero p90/mean **NO** son tautológicos y reflejan varianza intra-bucket real.

### Por qué no desplegar

1. La cal v7 por bucket absorbe completamente el ~150 px de offset en Y entre el
   marco exterior (heurística) y el inner body (template). Δ body_rect heuristic
   vs template = consistentemente (+50, +150) px → cal lo neutraliza.
2. El template añade ruido intra-bucket (los corners detectados varían levemente
   entre hojas), empeorando p90 (34 → 44 px) y mean (13.5 → 33 px).
3. `hough` y `heuristic_v2` no mejoran nada.

### Semántica del anchor (referencia)

`src/oviedo_rc/geom.py:53-58`:
```python
body_x_min = m.x0 + col * m.cell_w + sub_x_off - m.marg_x
body_y_max = m.ymax - row * m.cell_h - sub_y_off + m.marg_y
```
El modelo asume que `(bx, by)` del body_rect = píxel donde físicamente está
`(X_min_W, Y_max_N)` UTM = esquina NW del **área dibujable** del mapa (no del
cajetín decorativo). La heurística captura el cajetín, off por ~150 px en Y.
La cal v7 lo absorbe.

## Cómo correr el bench (plan A — sin anotación manual)

Asume CWD = repo root y `.venv` activo con `oviedo_rc` instalado.

```bash
# 1. scan: detecta body_rect heurística en todas las hojas cacheadas
python experiments/body_rect/scan_fallback.py

# 2. (opcional, sólo si quieres reproducir la selección del plan B)
python experiments/body_rect/sample_candidates.py

# 3. construye templates (escribe en experiments/body_rect/templates/)
python experiments/body_rect/build_templates.py

# 4. EL BENCH: análisis label-based, ~13 min para 1261 labels
python experiments/body_rect/analyze_via_labels.py
```

Output: `analyze_via_labels.json` con summary por método + cal_per_bucket deltas.

## Plan B (sólo si plan A no es concluyente)

```bash
python experiments/body_rect/annotator.py --port 8765
# anota 15 hojas en http://<vm>:8765/
python experiments/body_rect/bench_annotator.py
```

15 hojas dan 80× menos muestras que el plan A y miden algo semánticamente
distinto ("el ojo del user dibujando el frame"), no "qué body_rect hace que las
labels reales proyecten a la UTM correcta". Reservar para casos donde el label
set sea pequeño o esté contaminado.

## Cuándo re-evaluar

Síntomas que justifican reabrir:

1. **Mediana de drag en el validator UI sube** sostenidamente (>2 semanas) por
   encima de un umbral relevante (sugerencia: >10 px display, equivalente a
   >20 px nativos). Esto significaría que la cal v7 envejeció — pero **primero**
   recalibrar con `scripts/recalibrate.py` antes de cuestionar body_rect.
2. **Nuevo formato de hoja del Ayuntamiento** (rediseño del cajetín del PGOU)
   que rompa la heurística geométrica → fallback al 5% activo en hojas reales.
   El scan inicial (2026-05-16) encontró 0 fallbacks en 212 hojas; si reaparecen
   en >5% del catálogo, body_detect.detect_heuristic_v2 con Otsu podría rescatar.
3. **Cal mantiene residual_p90 > 50 px sostenidamente** incluso tras recalibrar
   → indica que la cal ya no puede absorber la variabilidad inter-sheet y un
   detector con menor varianza geométrica podría ayudar. Re-correr este bench
   con el label set actualizado.

## Estructura

```
experiments/body_rect/
├── README.md               ← este archivo
├── _setup.py               ← path setup compartido
├── body_detect.py          ← 4 métodos: heuristic, heuristic_v2, hough, template
├── scan_fallback.py        ← paso 0: detecta heurística en todas las hojas
├── sample_candidates.py    ← paso 1 (plan B): selecciona 15 hojas
├── build_templates.py      ← paso 2: extrae templates de corners
├── annotator.py            ← plan B: GT manual vía web
├── bench_annotator.py      ← plan B: bench contra GT manual
├── analyze_via_labels.py   ← plan A: bench contra 1261 labels reales
└── templates/              ← corners SU/SNU para detect_template (8 PNGs)
```

## Outputs (no commiteados, regenerables)

- `scan_fallback.json` — resultado del paso 0
- `annot_selection.json` — selección del paso 1
- `template_refs.json` — qué hojas se usaron para los templates
- `analyze_via_labels.json` — output del bench (plan A)
- `bench_annotator.md` / `.json` — output del bench (plan B)
- `body_rect_gt.json`, `.annot_imgs/` — outputs de la sesión de anotación (plan B)

## Tag de rollback

Si por error se despliega un cambio basado en este experimento y hay que volver
atrás: tag `pre-lightglue-2026-05-11` es el snapshot estable de referencia.
