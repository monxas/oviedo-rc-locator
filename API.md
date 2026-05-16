# Oviedo RC Locator API

Base URL: `https://locator.iarquitectos.com`

Auth: header `Authorization: Bearer <token>` (o `?token=` o cookie `iarq_locator`).

## Endpoints

```
GET /health                  estado sin auth
GET /locate/{rc}             pipeline SU (Suelo Urbano) clásico
GET /snu/{rc}                hoja SNU + render para RCs rurales sin SU
GET /planeamiento/{rc}       ámbito (UG/AU/PE), uso, edificabilidad, ficha sugerida
GET /info/{rc}               UNIFICADO: locate + planeamiento + patrimonio + snu fallback
GET /fichas                  listado de las 234 Fichas de Ámbitos
GET /fichas/search?q=...     búsqueda por código/número/nombre
GET /fichas/{filename}       descarga del PDF de ficha
GET /img/<sha>.png           PNG cacheado (TTL 24h)
GET /docs                    este texto
GET /api/swagger             OpenAPI Swagger UI
```

`{rc}` acepta 14 o 20 caracteres alfanuméricos (catastro completa hasta 14 internamente).

## GET /info/{rc} — recomendado

Una sola request devuelve toda la info disponible. Combina pipeline SU + WFS planeamiento + patrimonio + SNU fallback (paralelizado).

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  https://locator.iarquitectos.com/info/9651017TP6095S0001IT | jq
```

### Respuesta

```json
{
  "rc": "9651017TP6095S0001IT",
  "address": "CL ALTAMIRANO 6 OVIEDO (ASTURIAS)",
  "utm": [269455.69, 4804918.13],
  "locate": {
    "sheet": "PLANO_15_K_IV.pdf",
    "cell": "15-K",
    "sub_quadrant": "IV",
    "polygon_area_m2": 425.0,
    "plan_zoom_url": "https://locator.iarquitectos.com/img/<sha>.png",
    "polygon_url":   "https://locator.iarquitectos.com/img/<sha>.png",
    "wms_url":       "https://locator.iarquitectos.com/img/<sha>.png",
    "snap_score": 0.42,
    "reliability": "high"
  },
  "ambito": "RODRIGUEZ CABEZAS 4",
  "uso_predominante": "R",
  "edificabilidad": 1.45,
  "densidad_viv_ha": 25,
  "sistema_actuacion": "SC",
  "fichas_match": [
    {"filename": "RODRIGUEZ_CABEZAS_4_UG_RC4_Ficha_n_0120_PGOU.pdf",
     "score": 14, "tokens_matched": 4, "num_matched": 2}
  ],
  "patrimonio": [
    {"tipo": "n23_ELEM_CATALOGADOS_AREAS",
     "nombre": "AREA PROTECCION CAMINO SANTIAGO",
     "nivel_proteccion": "--", "tipo_patrimonio": "CUL"}
  ],
  "snu_sheet": null,
  "snu_url": null,
  "notes": [],
  "took_ms": 2000
}
```

`locate` es `null` cuando la RC está en Suelo No Urbanizable; en su lugar se rellenan `snu_sheet` y `snu_url`.

`fichas_match` está vacío si el ámbito viene sólo de ordenanza n12 (no hay ficha asociada).

Latencia: ~2 s urbana (cache caliente), ~5–8 s primera vez (descarga PDF SU + render). Rural ~1.5 s.

## GET /locate/{rc}

Pipeline SU clásico (sin planeamiento). Mismo payload que el campo `locate` de `/info`. Útil cuando sólo necesitas el plano + WMS y quieres minimizar latencia.

## GET /snu/{rc}

Para RCs en Suelo No Urbanizable (78 % del catálogo de Oviedo).

```json
{
  "rc": "33054A03200195",
  "address": "",
  "utm": [260754.87, 4808797.87],
  "snu_sheet": "PLANO_B_3.pdf",
  "snu_url":   "https://locator.iarquitectos.com/img/<sha>.png",
  "took_ms": 500
}
```

La hoja se resuelve por bbox grid del municipio (cal v2 desde WFS real `n02_CLASIFICACION_SUELO`, `data/snu_grid.json`). Cuando la celda exacta no existe, se devuelve el vecino Chebyshev más cercano.

## GET /planeamiento/{rc}

Sólo la parte de planeamiento (sin pipeline SU). Más rápido (~1 s):

```json
{
  "rc": "9651017TP6095S0001IT",
  "address": "...",
  "utm": [269455, 4804918],
  "ambito": "RODRIGUEZ CABEZAS 4",
  "ug": { "_campos_WFS_n15": "..." },
  "layers": {
    "n15_UNIDADES_GESTION": [],
    "n12_USOS_PORMENORIZADOS": []
  },
  "fichas_match": [],
  "took_ms": 800
}
```

Fuente: GeoServer del Principado de Asturias `visorrpgur.asturias.es:8090/geoserver/E79_ENTIDADES_URBANISTICAS/`, filtrado por `id_municipio=33044` (Oviedo INE).

Capas consultadas en paralelo: n15 UG, n25 modif, n22 instrumentos, n12 usos pormenorizados, n06 núcleos rurales, n07 sist. generales, n23 elem. catalogados (BBOX ±50 m), n27 BICs (BBOX ±50 m).

## GET /fichas

```bash
curl ...iarquitectos.com/fichas              # 234 items
curl ...iarquitectos.com/fichas?tipo=UG      # 68 UG
curl ...iarquitectos.com/fichas/search?q=LA_ERIA   # 6 hits
curl -o ficha.pdf ...iarquitectos.com/fichas/RODRIGUEZ_CABEZAS_4_UG_RC4_Ficha_n_0120_PGOU.pdf
```

Tipos: UG (68), UG1 (16), UG2 (43), AU (28), AUS (18), AA (13), PE (33), PP (6), sin-tipo (9). Total 234.

## Campos clave

| campo | descripción |
|---|---|
| `locate.snap_score` | 0–1, cross-correlation polígono ↔ líneas plano. >0.30 fiable. |
| `locate.reliability` | `high`/`medium`/`low` según labels manuales en `(cell, sub_quadrant)`. |
| `ambito` | Nombre UG/AU/PE/núcleo; cuando no hay, ordenanza n12. |
| `uso_predominante` | Código catastro: R residencial, T terciario, I industrial, etc. |
| `edificabilidad` | m²/m² (UG). |
| `sistema_actuacion` | SC compensación · SE expropiación · etc. |
| `fichas_match` | Lista ordenada por score (tokens nombre + número exacto + tokens etiqueta). Top hit suele ser correcto. |
| `patrimonio` | Afecciones a ±50 m: áreas catalogadas, BICs, dominio público. |

## Errores

| Código | Causa |
|---|---|
| 401 | token inválido |
| 404 | RC no resoluble (fuera de bbox, no en Catastro) |
| 422 | RC mal formado (no 14/20 chars alfanuméricos) |
| 500 | error de red en Catastro / Ayuntamiento / WFS |

## Cobertura

- Término municipal de Oviedo (id_municipio INE 33044).
- 151 hojas PGOU 1:1000 + 61 hojas SNU 1:5000 + Mapa Guía + 234 Fichas de Ámbitos.
- 53.804 RCs con coords (catastral local).
- Calibración v7 desplegada (2026-05-12): 1261 labels totales = 139 manuales + 1122 LightGlue auto (filtro `inlier_ratio≥0.65`, `samples≥5`). Mejora −18 % en mediana de error vs cal v6 sobre 23 labels de validación.

## Limpieza

Las imágenes `/img/<sha>.png` se cachean en `/tmp/locator_cache/img/` y se purgan
diariamente a las 03:00 (cron). Los `bundles/` permanecen.
