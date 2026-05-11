# Oviedo RC Locator API · uso

Base URL: `https://locator.iarquitectos.com`

Auth: header `Authorization: Bearer <token>` (o `?token=` o cookie `iarq_locator`).

## Endpoints

```
GET /health                 → estado sin auth
GET /locate/{rc}            → ejecuta pipeline y devuelve JSON + URLs PNG
GET /img/<sha>.png          → PNG cacheado (TTL 24h por cron)
GET /docs                   → este texto
GET /api/swagger            → OpenAPI Swagger UI
```

## GET /locate/{rc}

`rc` = referencia catastral de 20 caracteres alfanuméricos.

```bash
TOKEN=...
curl -s https://locator.iarquitectos.com/locate/9651017TP6095S0001IT \
  -H "Authorization: Bearer $TOKEN" -H "User-Agent: curl/8" \
  | python3 -m json.tool
```

### Respuesta

```json
{
  "rc": "9651017TP6095S0001IT",
  "address": "CL ALTAMIRANO 6 OVIEDO (ASTURIAS)",
  "sheet": "PLANO_15_K_IV.pdf",
  "sheet_url": "https://www.oviedo.es/documents/35127/...",
  "cell": "15-K",
  "sub_quadrant": "IV",
  "utm": [269455.69, 4804918.13],
  "polygon_area_m2": 425.0,
  "n_units": 1,
  "plan_full_url":  "https://locator.iarquitectos.com/img/<sha>.png",
  "plan_zoom_url":  "https://locator.iarquitectos.com/img/<sha>.png",
  "polygon_url":    "https://locator.iarquitectos.com/img/<sha>.png",
  "wms_url":        "https://locator.iarquitectos.com/img/<sha>.png",
  "snap_dx": 12,
  "snap_dy": -2,
  "snap_score": 0.354,
  "snap_confident": true,
  "edge_override": null,
  "reliability": "high",
  "expected_residual_m": 1.66,
  "n_labels": 5,
  "warnings": [],
  "took_ms": 1620
}
```

### Imágenes

- `plan_full_url`  · plano PGOU completo con cruz
- `plan_zoom_url`  · recorte 900×900 alrededor del RC
- `polygon_url`    · zoom con polígono catastral alineado por snap
- `wms_url`        · WMS catastral 300×300 m

Requieren el mismo Bearer token.

## Campos clave

| campo | descripción |
|---|---|
| `snap_score` | 0-1, cross-correlation polígono ↔ líneas plano. >0.30 fiable. |
| `snap_confident` | `true` si `snap_score ≥ 0.30`. |
| `reliability` | `high`/`medium`/`low` según labels manuales en `(cell, sub_quadrant)`. |
| `expected_residual_m` | error esperado del polígono en metros (mediana). |
| `n_labels` | labels manuales usados para calibrar este bucket. |
| `warnings` | array de avisos (ej. fuera de bbox urbano). |

## Errores

| Código | Causa |
|---|---|
| 401 | token inválido |
| 404 | RC no resoluble (fuera de bbox, no en Catastro, hoja inexistente) |
| 422 | RC mal formado (no 20 chars alfanuméricos) |
| 500 | error de red en Catastro / Ayuntamiento |

## Cobertura

- Término municipal de Oviedo (zona urbana).
- 151 hojas del PGOU 1:1000 (2013 consolidado).
- Caché local de polígonos catastrales (~54k) y WMS (~38 MB).
- Calibración por (cell, sub_quadrant) con labels manuales.

## Latencia

- Primer hit cold (descarga PDF / WMS): 10-15 s
- Subsequent warm: 1-2 s

## Limpieza

Las imágenes `/img/<sha>.png` se cachean en `/tmp/locator_cache/img/` y se purgan
diariamente a las 03:00 (cron). Los `bundles/` permanecen.
