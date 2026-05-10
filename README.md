# oviedo_rc

Pipeline _production-ready_ que, dada una referencia catastral (RC) del término
municipal de Oviedo, resuelve **toda la información derivable**:

- Hoja del **PGOU 1:1000** que contiene la parcela (PDF original + PNGs anotados,
  polígono alineado a las líneas del plano por _cross-correlation_).
- Polígono catastral en **EPSG:25830** (UTM ETRS89 30N), con área y label.
- **Inmuebles** dentro de la parcela: dirección, escalera/planta/puerta, uso,
  superficie, año.
- **WMS catastral** (mosaico local + fallback online).

Cada capa funciona **cache-first** con _fallback_ online. Tras un prefetch, todo
el pipeline corre **offline** para la zona urbana de Oviedo.

---

## Instalación

```bash
pip install -e .            # paquete + CLI `oviedo-rc`
pip install -e '.[web]'     # con app Flask de validación
pip install -e '.[dev]'     # con pytest + ruff
```

Requiere **Python ≥ 3.10**. Dependencias core: `requests`, `PyMuPDF`,
`opencv-python`, `numpy`.

---

## Uso (CLI)

```bash
# Bundle completo en bundles/<RC>/
oviedo-rc 9651017TP6095S0001IT

# Solo localizar (sin descargar PDF/WMS), output JSON
oviedo-rc 9651017TP6095S0001IT --locate-only

# Skip WMS y/o snap del polígono
oviedo-rc <RC> --no-wms --no-snap

# Output del bundle como JSON
oviedo-rc <RC> --json
```

Equivalente con `python -m oviedo_rc <RC>`.

## Uso (API Python)

```python
from oviedo_rc import process_rc, locate, validate_rc

# Solo localizar (rápido, no descarga)
loc = locate("9651017TP6095S0001IT")
print(loc["sheet_name"], loc["body_relative"])

# Pipeline completo
bundle = process_rc("9651017TP6095S0001IT")
print(bundle.address)                  # CL ALTAMIRANO 6 OVIEDO (ASTURIAS)
print(bundle.sheet_name)               # PLANO_15_K_IV.pdf
print(bundle.plan_zoom_png)            # bundles/<RC>/<RC>_plan_zoom.png
```

`process_rc` devuelve un `RCBundle` con paths absolutos a los artefactos.

---

## Estructura del bundle

```
bundles/<RC>/
├── <RC>_plan_full.png       Plano completo con polígono o cruz
├── <RC>_plan_zoom.png       Recorte 900×900 alrededor del RC
├── <RC>_polygon.png         Zoom con polígono alineado al plano (snap)
├── <RC>_wms.png             WMS catastral (300 m × 300 m del bbox)
├── <RC>_content.json        Inmuebles dentro de la parcela
└── <RC>_metadata.json       Plano, cell, sub_quadrant, snap dx/dy/score, área...
```

---

## Estructura del repo

```
oviedo_rc_locator/
├── pyproject.toml          paquete instalable + CLI `oviedo-rc`
├── src/oviedo_rc/          paquete Python (la API pública)
│   ├── config.py           paths, modelo MALLA, regex RC
│   ├── geom.py             validate_rc(), locate()
│   ├── pgou.py             listado y descarga de hojas del Ayuntamiento
│   ├── catastro.py         rc_to_utm, WFS bbox, get_parcel_polygon, DNPRC
│   ├── wms.py              WMS catastral (local mosaic + remote)
│   ├── render.py           render PDF + dibujo de marcador/polígono
│   ├── snap.py             cross-correlation polígono ↔ líneas del plano
│   ├── pipeline.py         process_rc() + RCBundle
│   ├── http_utils.py       GET con retry + magic-byte validation
│   ├── errors.py           OviedoError, RCError
│   └── cli.py              entry-point CLI
├── scripts/                tooling de mantenimiento
│   ├── prefetch.py         pre-descarga (plans, parcels, dnprc, all, gc, verify)
│   ├── prefetch_wms.py     mosaico WMS catastral
│   ├── build_coords_cache.py   construye coords_local.json desde polígonos
│   ├── stress_test.py      bench cold vs warm contra el caché
│   └── distributed/        workers SSH para distribuir el prefetch
│       ├── dnprc_worker.py
│       └── dnprc_worker_sni.py
├── bin/                    wrappers shell (cron)
│   ├── update.sh           update | full | verify | refresh | gc | status
│   ├── distributed_dnprc.sh    orquestación de Pis vía Tailscale
│   └── test_offline.sh     smoke test del modo offline
├── web/                    app Flask de validación + corrección manual
│   ├── server.py
│   └── gen_*.py            generadores de candidatos / test sets
├── tests/                  pytest (formato RC + calibraciones)
├── data/calibrations.json  4 anchors manuales del modelo geométrico
└── docs/                   ADRs, notas de calibración
```

El paquete usa el directorio `~/.cache/oviedo_rc/` (override con
`$OVIEDO_CACHE`):

```
~/.cache/oviedo_rc/
├── sheets.json                         151 hojas del PGOU
├── coords_local.json                   ~54k rc14 → (X, Y, address)
├── PLANO_*.pdf                         151 PDFs (~240 MB)
├── parcels/
│   ├── bbox_<x1>_<y1>_<x2>_<y2>.json   706 chunks WFS (~50k parcelas)
│   ├── poly_<refcat>.json              polígonos sueltos
│   └── dnprc_<refcat>.json             ~54k contenidos catastrales
└── wms/wms_<bbox>_<mpp>.png            mosaico WMS (135 tiles, ~38 MB)
```

Caché total tras prefetch completo: **~560 MB**.

---

## Pre-fetch para uso offline

```bash
# Estado del caché
python scripts/prefetch.py status

# Bloques individuales
python scripts/prefetch.py plans          # 151 PDFs (~240 MB)
python scripts/prefetch.py parcels        # polígonos WFS bbox urbano
python scripts/prefetch.py dnprc          # contenidos por parcela (lento)
python scripts/prefetch.py all            # todo lo anterior

# WMS catastral (mosaico local)
python scripts/prefetch_wms.py fetch
python scripts/prefetch_wms.py status

# Tras descargar bbox/dnprc, reconstruir el cache rc14→UTM
python scripts/build_coords_cache.py

# Verificación + reparación
python scripts/prefetch.py verify --fix
python scripts/prefetch.py gc

# Cron mensual (mantenimiento)
bin/update.sh update
```

### Distribución entre Raspberry Pis (Tailscale)

Si la IP local es baneada por Catastro, el prefetch DNPRC se distribuye sobre
Pis accesibles por Tailscale SSH:

```bash
bin/distributed_dnprc.sh status
bin/distributed_dnprc.sh start 4000     # 4000 RCs por Pi
bin/distributed_dnprc.sh fetch          # rsync de outputs locales
```

---

## Modelo geométrico

Cells **rectangulares** ~1036 m × 695 m con cinta marginal. Sub-cuadrantes
500 m × 350 m. Convención (verificada con 4 RCs):

```
NW = I    NE = II
SW = III  SE = IV
```

Ajustado por LSQ con 71 RCs reales:

- mediana 4.5 m, p90 7.75 m de residual sobre `body_relative`
- 100 % de hojas correctas en 107 RCs validadas
- snap de polígono cubre el residual restante (~< 5 m)

Constantes en `src/oviedo_rc/config.py` (`MALLA_*`).

---

## Tests

```bash
pytest tests/ -q
```

- `test_format.py` — validador de RC (offline puro)
- `test_calibrations.py` — anchors de `data/calibrations.json` (necesita
  red o caché)

---

## Errores

| Caso | Excepción | Exit code CLI |
|---|---|---|
| RC con formato inválido | `RCError` | 2 |
| RC fuera del bbox de Oviedo | `RCError` | 2 |
| RC inexistente en Catastro | `RCError` | 2 |
| Hoja no en listado del PGOU | `RCError` | 2 |
| Fallo de red persistente (3 retries) | `RuntimeError` | 1 |

`RCError` hereda de `ValueError` y de `OviedoError`. La API pública es:

```python
from oviedo_rc import process_rc, locate, validate_rc
from oviedo_rc import OviedoError, RCError, RCBundle
```
