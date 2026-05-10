# Validador RC → PGOU Oviedo

App web para validar (y refinar) las predicciones del modelo `locate_rc.py`.

## Flujo

1. **Generar candidatos**: `python3 gen_candidates.py [N]` (default 100).
   - Pide N RCs al catastro de Oviedo distribuidas por calles representativas
   - Para cada una ejecuta `locate()` → predice plano + posición pixel
   - Genera dos imágenes por candidato:
     - `imgs/<idx>_plan.jpg` — crop ±125 m del plano PGOU con cruz roja en la predicción
     - `imgs/<idx>_cat.jpg` — catastro 200 m alrededor con cruz roja en la RC exacta
   - Guarda metadata en `static/candidates.json`

2. **Validar en web**: `python3 server.py` → abre http://127.0.0.1:5057
   - Para cada candidato comparas la cruz del plano con la del catastro
   - Pulsas:
     - **OK** (atajo `j`) — la cruz está sobre el portal correcto
     - **Mal** (atajo `k`) — está mal. Si quieres aportar corrección, **click sobre el plano** donde está realmente y luego pulsa Mal
     - **Skip** (`s`) — no se distingue
     - **Reset** (`r`) — deshace el voto
   - Resultados se guardan en `results/validations.json`

3. **Exportar**: `/api/export` devuelve JSON con todos los votos + correcciones en metros (Δx, Δy) listas para inyectar como nuevas calibraciones.

## Atajos teclado
- `j` — OK
- `k` — Mal  
- `s` — Skip
- `r` — Reset (la card visible en la mitad superior de la ventana)

## Filtros
Botones arriba para mostrar solo: Todos / Pendientes / OK / Mal / Skip.

## Re-entrenar el modelo

Tras N validaciones (especialmente las "Mal con click"), el JSON exportado contiene:

```json
{
  "rc": "...",
  "utm": [X, Y],
  "sheet_name": "PLANO_X_Y_Z.pdf",
  "body_rx_predicted": 0.443,
  "body_ry_predicted": 0.532,
  "verdict": "bad",
  "correction_dx_m": +12.3,
  "correction_dy_m": -5.1
}
```

Las correcciones en metros se aplican como:
```
body_rx_correcto = body_rx_predicted + correction_dx_m / BODY_W_M
body_ry_correcto = body_ry_predicted + correction_dy_m / BODY_H_M
```

Esos pares (RC, body_rx, body_ry) se añaden a `CALIBRATIONS` en `locate_rc.py` y se vuelve a ajustar el modelo geométrico por LSQ.
