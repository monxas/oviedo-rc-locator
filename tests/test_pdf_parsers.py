"""Golden tests para los parsers de fichas PDF (Oviedo + Gijón).

Estos tests requieren los PDFs cacheados localmente. Si no existen, se
saltan los casos individualmente (no falla la suite).

Marcado como `integration` — exclúyelo con `pytest -m "not integration"`
si no tienes el cache (~/.cache/oviedo_rc/...).

Cubre variedad de tipos de ámbito:
  Oviedo: UG, AU, AUS, AA, PE  → parser oviedo_pdf_parse.parse_ficha_pdf
  Gijón:  APP, AUA, distintos usos → parser gijon_pdf_parse.parse_ficha_pdf

Los goldens se generaron leyendo los JSONs ya parseados en
~/.cache/oviedo_rc/fichas_data/ y ~/.cache/oviedo_rc/gijon/fichas_data/.
Si un campo divergiera tras un cambio del parser, este test lo detectaría.
"""
from pathlib import Path

import pytest


# ------------------------------------------------------------------
# Oviedo
# ------------------------------------------------------------------

OVIEDO_PDF_DIR = Path.home() / ".cache" / "oviedo_rc" / "fichas"

OVIEDO_GOLDEN: dict[str, dict] = {
    # UG — Rodriguez Cabezas 4
    "RODRIGUEZ_CABEZAS_4_UG_RC4_Ficha_n_0120_PGOU.pdf": {
        "clave": "UG-RC4",
        "numero": 120,
        "tipo_ambito": "UNIDAD DE GESTIÓN",
        "nombre": "RODRIGUEZ CABEZAS 4",
        "hoja_pgou": "15-J/",
        "sistema_actuacion": "COMPENSACIÓN",
        "iniciativa": "PRIVADA",
        "instrumento_planeamiento_definitiva": "ESTUDIO DE DETALLE",
        "aprovechamiento_medio_codigo": "RC",
        "aprovechamiento_medio_uah": 1.3001,
        "superficie_total_ambito_m2": 1223.0,
        "altura_maxima_plantas": 4,
        "viviendas_construibles_total": 16,
        "densidad_bruta_viv_ha": 130.83,
    },
    # AU — Altos de Santo Medero
    "ALTOS_DE_SANTO_MEDERO_AU_ASM_Ficha_n_0601_PGOU.pdf": {
        "clave": "AU-ASM",
        "numero": 601,
        "tipo_ambito": "AMBITO URBANIZABLE",
        "nombre": "ALTOS DE SANTO MEDERO",
        "sistema_actuacion": "COMPENSACIÓN",
        "iniciativa": "PRIVADA",
        "aprovechamiento_medio_codigo": "RU",
        "aprovechamiento_medio_uah": 0.26,
        "superficie_total_ambito_m2": 178558.0,
        "altura_maxima_plantas": 3,
        "viviendas_construibles_total": 232,
        "densidad_bruta_viv_ha": 12.99,
    },
    # AUS — Anieves Este
    "ANIEVES_ESTE_AUS_ANE_Ficha_n_0671_PGO.pdf": {
        "clave": "AUS-ANE",
        "numero": 671,
        "tipo_ambito": "AMBITO URBANIZABLE",
        "nombre": "ANIEVES-ESTE",
        "sistema_actuacion": "URBANIZABLE PRIORITARIO",
        "iniciativa": "PRIVADA",
        "aprovechamiento_medio_codigo": "RA",
        "aprovechamiento_medio_uah": 0.4417,
        "superficie_total_ambito_m2": 18048.0,
        "altura_maxima_plantas": 4,
        "viviendas_construibles_total": 71,
        "densidad_bruta_viv_ha": 39.34,
    },
    # AA — Avenida del Mar 3
    "AVENIDA_DEL_MAR_3_AA_AM3_Ficha_n_0312_PGOU.pdf": {
        "clave": "AA-AM3",
        "numero": 312,
        "tipo_ambito": "ACTUACION AISLADA",
        "nombre": "AVENIDA DEL MAR 3",
        "sistema_actuacion": "NO PROCEDE",
        "iniciativa": "PUBLICA",
        "instrumento_planeamiento_definitiva": "PLAN ESPECIAL",
        "superficie_total_ambito_m2": 14897.0,
    },
    # PE — Armando Collar
    "ARMANDO_COLLAR_PE_ACO_Ficha_n_0419_PGOU.pdf": {
        "clave": "PE-ACO",
        "numero": 419,
        "tipo_ambito": "PLAN ESPECIAL",
        "nombre": "ARMANDO COLLAR",
        "sistema_actuacion": "COMPENSACIÓN",
        "iniciativa": "PRIVADA",
        "instrumento_planeamiento_definitiva": "PLAN ESPECIAL",
        "aprovechamiento_medio_codigo": "RC",
        "aprovechamiento_medio_uah": 1.3154,
        "superficie_total_ambito_m2": 19987.0,
        "altura_maxima_plantas": 6,
        "viviendas_construibles_total": 226,
        "densidad_bruta_viv_ha": 113.07,
    },
}


@pytest.mark.integration
@pytest.mark.parametrize("filename, expected", OVIEDO_GOLDEN.items())
def test_oviedo_ficha_pdf(filename, expected):
    from oviedo_rc.oviedo_pdf_parse import parse_ficha_pdf

    pdf = OVIEDO_PDF_DIR / filename
    if not pdf.exists():
        pytest.skip(f"PDF no disponible: {filename}")
    parsed = parse_ficha_pdf(pdf)
    for k, v in expected.items():
        got = parsed.get(k)
        assert got == v, f"{filename}.{k}: expected {v!r}, got {got!r}"


# ------------------------------------------------------------------
# Gijón
# ------------------------------------------------------------------

GIJON_PDF_DIR = Path.home() / ".cache" / "oviedo_rc" / "gijon" / "fichas_pdf"

GIJON_GOLDEN: dict[str, dict] = {
    # APP residencial colectiva pequeño
    "APP-10.pdf": {
        "ambito_id": "APP-10",
        "uso_predominante": "RESIDENCIAL VIVIENDA COLECTIVA",
        "categoria_suelo": "SUNC URBANO NO CONSOLIDADO",
        "clase_suelo": "SUELO URBANO",
        "sistema_actuacion": "COMPENSACIÓN",
        "iniciativa": "PRIVADA",
        "altura_max_plantas": 3,
        "superficie_ambito_m2": 5342.31,
        "edificabilidad_m2m2": 0.88,
        "viviendas_estimadas": 50,
        "ordenanza": "3",
    },
    # APP productivo terciario
    "APP-100.pdf": {
        "ambito_id": "APP-100",
        "uso_predominante": "PRODUCTIVO TERCIARIO",
        "clase_suelo": "SUELO URBANO",
        "sistema_actuacion": "COMPENSACIÓN",
        "superficie_ambito_m2": 38364.72,
        "edificabilidad_m2m2": 0.3,
    },
    # AUA residencial mediano
    "AUA-085.pdf": {
        "ambito_id": "AUA-085",
        "uso_predominante": "RESIDENCIAL VIVIENDA COLECTIVA",
        "categoria_suelo": "SUNC URBANO NO CONSOLIDADO",
        "clase_suelo": "SUELO URBANO",
        "sistema_actuacion": "COMPENSACIÓN",
        "iniciativa": "PRIVADA",
        "altura_max_plantas": 3,
        "superficie_ambito_m2": 1047.0,
        "edificabilidad_m2m2": 1.22,
        "viviendas_estimadas": 13,
        "ordenanza": "3",
    },
    # APP-CAR-01 — Cabueñes
    "APP-CAR-01.pdf": {
        "ambito_id": "APP-CAR-01",
        "uso_predominante": "RESIDENCIAL VIVIENDA COLECTIVA",
        "clase_suelo": "SUELO URBANO",
        "sistema_actuacion": "COMPENSACIÓN",
        "altura_max_plantas": 4,
        "superficie_ambito_m2": 3485.0,
        "edificabilidad_m2m2": 0.3,
        "viviendas_estimadas": 10,
        "ordenanza": "4",
    },
    # APP-CE-10 — vivienda unifamiliar, suelo urbanizable
    "APP-CE-10.pdf": {
        "ambito_id": "APP-CE-10",
        "uso_predominante": "RESIDENCIAL VIVIENDA UNIFAMILIAR",
        "categoria_suelo": "URBANIZABLE SECTORIZADO",
        "clase_suelo": "SUELO URBANIZABLE",
        "sistema_actuacion": "COMPENSACIÓN",
        "iniciativa": "PRIVADA",
        "superficie_ambito_m2": 119316.0,
        "edificabilidad_m2m2": 0.15,
        "viviendas_estimadas": 77,
        "ordenanza": "5.3",
    },
}


@pytest.mark.integration
@pytest.mark.parametrize("filename, expected", GIJON_GOLDEN.items())
def test_gijon_ficha_pdf(filename, expected):
    from oviedo_rc.gijon_pdf_parse import parse_ficha_pdf

    pdf = GIJON_PDF_DIR / filename
    if not pdf.exists():
        pytest.skip(f"PDF no disponible: {filename}")
    parsed = parse_ficha_pdf(pdf)
    for k, v in expected.items():
        got = parsed.get(k)
        assert got == v, f"{filename}.{k}: expected {v!r}, got {got!r}"
