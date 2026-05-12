"""Parser para fichas PDF del PGOU de Oviedo.

A diferencia de Gijón, los PDFs de Oviedo son texto plano (sin cipher).
PyMuPDF.get_text() devuelve líneas directamente legibles.

Estructura típica:
- Página 1: plano con cotas (descartada por ser mayoritariamente números).
- Página 2: "CONDICIONES GENERALES Y PARAMETROS" — campos clave.
- Página 3: criterios y objetivos (texto libre, no extraído).

El layout es "label en una línea, valor en la siguiente". Algunos labels
comparten línea con el valor (ej. "NUMERO 120", "INICIATIVA PRIVADA",
"VIVIENDAS CONSTRUIBLES TOTAL: 16 Viv"), así que tratamos cada caso.

Las tablas de calificación de suelos vienen en bloques tipo:

    CODIGO
    CALIFICACION
    m2 de suelo
    m2 construible
    EL
    ESPACIOS LIBRES PUBLICOS

    284

    0
    EX
    EQUIPAMIENTO PÚBLICO SIN DEFINIR

    1.946

    0
    ...
    TOTAL SUELO PUBLICO

    14.300 m2

    0 m2

Parseamos las filas en grupos de 4 líneas con texto no vacío
(codigo, calificacion, m2_suelo, m2_construible).
"""
from __future__ import annotations

import re
from pathlib import Path

import fitz  # PyMuPDF


_NUMBER_RE = re.compile(r"-?\d{1,3}(?:\.\d{3})*(?:,\d+)?|-?\d+(?:,\d+)?")

# Labels conocidos: si la "siguiente línea" tras un label resulta ser
# otro label (campo vacío en el PDF), devolvemos None.
_KNOWN_LABELS = {
    "CLAVE", "NUMERO", "TIPO DE AMBITO", "NOMBRE", "HOJA", "ESCALA",
    "DATOS URBANISTICOS", "ORIGEN AMBITO", "FECHA",
    "SISTEMA DE ACTUACION", "INICIATIVA", "INICIATIVA PRIVADA",
    "INICIATIVA PÚBLICA",
    "INFORMACION DE GESTION", "INSTRUMENTO DE PLANEAMIENTO",
    "INICIAL", "DEFINITIVA",
    "INFORMACION FASE DE EJECUCION", "EXPROPIACION", "CESIONES",
    "URBANIZACION", "EDIFICACION",
    "ORDENACION PORMENORIZADA",
    "CALIFICACION DE SUELOS PUBLICOS", "CALIFICACION DE SUELOS PRIVADOS",
    "CODIGO", "CALIFICACION", "M2 DE SUELO", "M2 CONSTRUIBLE",
    "TOTAL SUELO PUBLICO", "TOTAL SUELO PRIVADO",
    "APROVECHAMIENTO URBANISTICO", "SUPERFICIE TOTAL DEL AMBITO",
    "INDICE EDIF. BRUTA", "USO GLOBAL PREDOMINANTE",
    "ALTURA MAXIMA", "SISTEMAS GENERALES", "VIARIO",
    "ESPACIOS LIBRES", "EQUIPAMIENTOS",
    "OTROS PARAMETROS NO VINCULANTES",
}


def _looks_like_label(s: str) -> bool:
    n = re.sub(r"\s+", " ", s.strip().upper())
    if n in _KNOWN_LABELS:
        return True
    # Headers que empiezan con un label conocido pero tienen sufijo (ej. "ALTURA MAXIMA")
    return False


def _to_float(s: str | None) -> float | None:
    if s is None:
        return None
    s = s.strip().rstrip("%").strip()
    if not s:
        return None
    s = s.replace(" ", "")
    # Quita sufijos comunes
    for suf in ("m2c/m2", "m2/viv", "Viv/Ha", "m2", "Viv", "u.a.h.", "PLANTAS", "%"):
        if s.endswith(suf):
            s = s[: -len(suf)].strip()
    if not s:
        return None
    s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def _to_int(s: str | None) -> int | None:
    f = _to_float(s)
    if f is None:
        return None
    return int(round(f))


def _is_coord_dump_page(text: str) -> bool:
    """Página 1 normalmente es un plano con cientos de cotas decimales."""
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    if not lines:
        return True
    # Líneas que parecen sólo número (cota o etiqueta de altura tipo "-I+VI")
    nums = 0
    for ln in lines:
        if re.fullmatch(r"-?\d+(?:\.\d+)?", ln):
            nums += 1
        elif re.fullmatch(r"-?[IVXP]+\+?[IVXP]*", ln):  # alturas tipo "II", "-I+VI"
            nums += 1
    return nums / len(lines) > 0.4


def _find_value_after_label(lines: list[str], label: str,
                            *, max_skip: int = 1,
                            reject_labels: bool = True) -> str | None:
    """Devuelve la línea siguiente no vacía tras la línea que coincide con `label`.

    Tolera variaciones en mayúsculas/minúsculas y espacios. Si el label
    aparece junto al valor en la misma línea (`LABEL: valor`), lo extrae también.

    Si `reject_labels`, descarta valores que son a su vez labels conocidos
    (el campo está vacío en el PDF).
    """
    label_norm = re.sub(r"\s+", " ", label.strip().upper())
    for i, ln in enumerate(lines):
        ln_clean = re.sub(r"\s+", " ", ln.strip().upper())
        # Caso A: label completo en la línea, valor a continuación
        if ln_clean == label_norm:
            for j in range(i + 1, min(i + 1 + max_skip + 1, len(lines))):
                v = lines[j].strip()
                if v:
                    if reject_labels and _looks_like_label(v):
                        return None
                    return v
            return None
        # Caso B: "LABEL valor" en la misma línea
        if ln_clean.startswith(label_norm + " ") or ln_clean.startswith(label_norm + ":"):
            rest = ln.strip()[len(label):].lstrip(": ").strip()
            if rest:
                if reject_labels and _looks_like_label(rest):
                    return None
                return rest
    return None


def _find_value_inline_or_next(lines: list[str], label_pat: re.Pattern) -> str | None:
    """Igual que _find_value_after_label pero tomando una regex como label.

    El primer grupo de la regex, si existe, será el valor inline. Si no hay
    captura o está vacía, mira la línea siguiente.
    """
    for i, ln in enumerate(lines):
        m = label_pat.match(ln.strip())
        if not m:
            continue
        if m.groups() and m.group(1).strip():
            return m.group(1).strip()
        # valor en la línea siguiente
        for j in range(i + 1, min(i + 3, len(lines))):
            v = lines[j].strip()
            if v:
                return v
        return None
    return None


def _parse_calificacion_table(lines: list[str], start_label: str,
                              end_label: str) -> tuple[list[dict], float | None, float | None]:
    """Parsea la tabla entre `start_label` (ej "CALIFICACION DE SUELOS PUBLICOS")
    y `end_label` (ej "TOTAL SUELO PUBLICO").

    Devuelve (filas, total_m2_suelo, total_m2_construible).
    Cada fila: {codigo, calificacion, m2_suelo, m2_construible}.
    """
    s_norm = re.sub(r"\s+", " ", start_label.strip().upper())
    e_norm = re.sub(r"\s+", " ", end_label.strip().upper())
    try:
        i_start = next(i for i, ln in enumerate(lines)
                        if re.sub(r"\s+", " ", ln.strip().upper()) == s_norm)
        i_end = next(i for i, ln in enumerate(lines[i_start + 1:], i_start + 1)
                      if re.sub(r"\s+", " ", ln.strip().upper()) == e_norm)
    except StopIteration:
        return [], None, None

    # Tras start_label vienen los 4 headers "CODIGO/CALIFICACION/m2 de suelo/m2 construible"
    # y luego las filas. Saltamos los headers explícitamente.
    body = []
    for ln in lines[i_start + 1: i_end]:
        s = ln.strip()
        if not s:
            continue
        if re.sub(r"\s+", " ", s.upper()) in ("CODIGO", "CALIFICACION", "M2 DE SUELO", "M2 CONSTRUIBLE"):
            continue
        body.append(s)

    # Agrupa de 4 en 4
    rows: list[dict] = []
    i = 0
    while i < len(body):
        # Necesitamos 4 elementos: codigo, calificacion, m2_suelo, m2_construible.
        # codigo es un token corto (1-4 chars, posibles dígitos y guión, espacios).
        # calificacion es texto en mayúsculas.
        # los dos últimos son números (posiblemente con coma).
        if i + 3 < len(body):
            codigo = body[i].strip()
            calif = body[i + 1].strip()
            m2s = body[i + 2].strip()
            m2c = body[i + 3].strip()
            # Confirma que las dos últimas son numéricas
            if _NUMBER_RE.fullmatch(m2s.replace(" m2", "").strip()) and \
               _NUMBER_RE.fullmatch(m2c.replace(" m2", "").strip()):
                rows.append({
                    "codigo": codigo,
                    "calificacion": calif,
                    "m2_suelo": _to_float(m2s),
                    "m2_construible": _to_float(m2c),
                })
                i += 4
                continue
        i += 1

    # Captura el total leyendo las 2 líneas no vacías después de end_label
    total_s = None
    total_c = None
    after_end = [ln.strip() for ln in lines[i_end + 1: i_end + 8] if ln.strip()]
    # Skip los 4 headers si reaparecen
    after_end = [a for a in after_end if re.sub(r"\s+", " ", a.upper()) not in
                  ("CODIGO", "CALIFICACION", "M2 DE SUELO", "M2 CONSTRUIBLE")]
    if len(after_end) >= 2:
        total_s = _to_float(after_end[0])
        total_c = _to_float(after_end[1])

    return rows, total_s, total_c


def parse_ficha_pdf(pdf_path: Path) -> dict:
    """Devuelve estructura con campos clave de la ficha de ámbito de Oviedo.

    Si no se encuentran datos, la mayoría de campos serán `None`. Las listas
    `suelos_publicos`/`suelos_privados` quedan vacías.
    """
    pdf_path = Path(pdf_path)
    out: dict = {
        "filename": pdf_path.name,
        "clave": None, "numero": None, "tipo_ambito": None, "nombre": None,
        "hoja_pgou": None,
        "origen_ambito": None, "sistema_actuacion": None, "iniciativa": None,
        "instrumento_planeamiento_inicial": None,
        "instrumento_planeamiento_definitiva": None,
        "expropiacion": None, "cesiones": None, "urbanizacion": None,
        "edificacion": None,
        "suelos_publicos": [], "suelos_privados": [],
        "total_suelo_publico_m2": None, "total_suelo_publico_construible_m2": None,
        "total_suelo_privado_m2": None, "total_suelo_privado_construible_m2": None,
        "aprovechamiento_medio_codigo": None, "aprovechamiento_medio_uah": None,
        "superficie_total_ambito_m2": None, "indice_edif_bruta": None,
        "uso_global_predominante": None, "altura_maxima_plantas": None,
        "viviendas_proteccion_pct": None, "n_aprox_viv_proteccion": None,
        "sg_viario_m2": None, "sg_espacios_libres_m2": None,
        "sg_equipamientos_m2": None,
        "viviendas_construibles_total": None,
        "densidad_bruta_viv_ha": None, "superficie_media_viv_m2": None,
    }

    doc = fitz.open(pdf_path)
    try:
        # Recoge texto de las páginas no-coord (típicamente página 2)
        all_lines: list[str] = []
        for page in doc:
            txt = page.get_text()
            if _is_coord_dump_page(txt):
                continue
            for ln in txt.split("\n"):
                # Línea vacía o sólo whitespace → mantenemos para separar bloques
                all_lines.append(ln.rstrip())
    finally:
        doc.close()

    if not all_lines:
        return out

    # CLAVE: aparece como "CLAVE\n<valor>" o bien con valor inline
    out["clave"] = _find_value_after_label(all_lines, "CLAVE")

    # NUMERO: usualmente inline como "NUMERO 120" o "NUMERO 86"
    num_str = _find_value_inline_or_next(
        all_lines, re.compile(r"^NUMERO\s+(\d+)\s*$", re.IGNORECASE))
    if not num_str:
        num_str = _find_value_after_label(all_lines, "NUMERO")
    out["numero"] = _to_int(num_str)

    # TIPO DE AMBITO
    out["tipo_ambito"] = _find_value_after_label(all_lines, "TIPO DE AMBITO")

    # NOMBRE
    out["nombre"] = _find_value_after_label(all_lines, "NOMBRE")

    # HOJA: viene como "HOJA\n15-J/" o similar
    out["hoja_pgou"] = _find_value_after_label(all_lines, "HOJA")

    # ORIGEN AMBITO
    out["origen_ambito"] = _find_value_after_label(all_lines, "ORIGEN AMBITO")

    # SISTEMA DE ACTUACION
    out["sistema_actuacion"] = _find_value_after_label(all_lines, "SISTEMA DE ACTUACION")

    # INICIATIVA: a veces "INICIATIVA PRIVADA" en una sola línea
    init_inline = _find_value_inline_or_next(
        all_lines, re.compile(r"^INICIATIVA\s+(.+?)\s*$", re.IGNORECASE))
    out["iniciativa"] = init_inline

    # EXPROPIACION / CESIONES / URBANIZACION / EDIFICACION
    out["expropiacion"] = _find_value_after_label(all_lines, "EXPROPIACION")
    out["cesiones"] = _find_value_after_label(all_lines, "CESIONES")
    out["urbanizacion"] = _find_value_after_label(all_lines, "URBANIZACION")
    out["edificacion"] = _find_value_after_label(all_lines, "EDIFICACION")

    # INSTRUMENTO DE PLANEAMIENTO — hay "INICIAL" y "DEFINITIVA" como labels
    # propios; los valores aparecen tras estos labels.
    # Pero a veces uno está vacío (sólo aparece DEFINITIVA en la siguiente línea).
    # Buscamos el bloque entre "INSTRUMENTO DE PLANEAMIENTO" e "INFORMACION FASE DE EJECUCION".
    try:
        i_ip = next(i for i, ln in enumerate(all_lines)
                     if re.sub(r"\s+", " ", ln.strip().upper()) == "INSTRUMENTO DE PLANEAMIENTO")
        i_end = next(i for i, ln in enumerate(all_lines[i_ip + 1:], i_ip + 1)
                      if "INFORMACION FASE DE EJECUCION" in ln.upper())
        block = [ln.strip() for ln in all_lines[i_ip + 1: i_end] if ln.strip()]
        # block contiene típicamente ["INICIAL", "DEFINITIVA", "ESTUDIO DE DETALLE"]
        # o ["INICIAL", "DEFINITIVA", "PROYECTO DE URBANIZACIÓN", "PLAN ESPECIAL"]
        # o ["INICIAL", "DEFINITIVA", "DELIMITACION DE SECTORES"]
        # Filtra labels ("INICIAL", "DEFINITIVA") y fechas dd/mm/yyyy
        date_re = re.compile(r"^\d{1,2}/\d{1,2}/\d{2,4}$")
        vals = [b for b in block
                 if b.upper() not in ("INICIAL", "DEFINITIVA")
                 and not date_re.match(b)]
        if len(vals) == 1:
            out["instrumento_planeamiento_definitiva"] = vals[0]
        elif len(vals) >= 2:
            out["instrumento_planeamiento_inicial"] = vals[0]
            out["instrumento_planeamiento_definitiva"] = vals[1]
    except StopIteration:
        pass

    # CALIFICACION DE SUELOS PUBLICOS
    rows, ts, tc = _parse_calificacion_table(
        all_lines, "CALIFICACION DE SUELOS PUBLICOS", "TOTAL SUELO PUBLICO")
    out["suelos_publicos"] = rows
    out["total_suelo_publico_m2"] = ts
    out["total_suelo_publico_construible_m2"] = tc

    rows, ts, tc = _parse_calificacion_table(
        all_lines, "CALIFICACION DE SUELOS PRIVADOS", "TOTAL SUELO PRIVADO")
    out["suelos_privados"] = rows
    out["total_suelo_privado_m2"] = ts
    out["total_suelo_privado_construible_m2"] = tc

    # APROVECHAMIENTO MEDIO: "APROVECHAMIENTO MEDIO: RC, 1,3001 u.a.h."
    for ln in all_lines:
        m = re.match(
            r"^APROVECHAMIENTO\s+MEDIO\s*:\s*([A-Z0-9 ]*?)\s*,\s*([\d\.,]+)\s*u\.a\.h\.?",
            ln.strip(), re.IGNORECASE)
        if m:
            code = m.group(1).strip()
            out["aprovechamiento_medio_codigo"] = code if code else None
            out["aprovechamiento_medio_uah"] = _to_float(m.group(2))
            break

    # SUPERFICIE TOTAL DEL AMBITO
    out["superficie_total_ambito_m2"] = _to_float(
        _find_value_after_label(all_lines, "SUPERFICIE TOTAL DEL AMBITO"))

    # INDICE EDIF. BRUTA
    out["indice_edif_bruta"] = _to_float(
        _find_value_after_label(all_lines, "INDICE EDIF. BRUTA"))

    # USO GLOBAL PREDOMINANTE
    out["uso_global_predominante"] = _find_value_after_label(
        all_lines, "USO GLOBAL PREDOMINANTE")

    # ALTURA MAXIMA: "4 PLANTAS" o "8 PLANTAS"
    altura = _find_value_after_label(all_lines, "ALTURA MAXIMA")
    out["altura_maxima_plantas"] = _to_int(altura)

    # % PARA VIVIENDAS DE PROTECCION: hay variantes inline y multilínea
    pct = _find_value_inline_or_next(
        all_lines, re.compile(r"^%\s*PARA\s+VIVIENDAS\s+DE\s+PROTECCION\s*:?\s*(.*)$",
                              re.IGNORECASE))
    out["viviendas_proteccion_pct"] = _to_float(pct)

    # Nº APROX. VIV. PROT.: 0
    nv = _find_value_inline_or_next(
        all_lines, re.compile(r"^N[ºoO\.]*\s*APROX\.?\s*VIV\.?\s*PROT\.?\s*:?\s*(.*)$",
                              re.IGNORECASE))
    out["n_aprox_viv_proteccion"] = _to_int(nv)

    # SISTEMAS GENERALES: formato "VIARIO\n0%=0 m2" o "VIARIO\n19,185%=34.256 m2".
    # Hay variantes con label inline: "EQUIPAMIENTOS0%=0 m2".
    def _parse_sg(label_word: str) -> float | None:
        # Busca línea que tras LABEL contenga un valor "X%=Y m2"
        pat = re.compile(rf"^{label_word}\s*([\d\.,]+)\s*%\s*=\s*([\d\.,]+)\s*m2",
                          re.IGNORECASE)
        for i, ln in enumerate(all_lines):
            m = pat.match(ln.strip())
            if m:
                return _to_float(m.group(2))
            # Label en su línea, valor en la siguiente
            if ln.strip().upper() == label_word.upper():
                for j in range(i + 1, min(i + 3, len(all_lines))):
                    nxt = all_lines[j].strip()
                    m2 = re.match(r"^([\d\.,]+)\s*%\s*=\s*([\d\.,]+)\s*m2",
                                   nxt, re.IGNORECASE)
                    if m2:
                        return _to_float(m2.group(2))
        return None

    out["sg_viario_m2"] = _parse_sg("VIARIO")
    out["sg_espacios_libres_m2"] = _parse_sg("ESPACIOS LIBRES")
    out["sg_equipamientos_m2"] = _parse_sg("EQUIPAMIENTOS")

    # VIVIENDAS CONSTRUIBLES TOTAL: 16 Viv (a veces inline, a veces \n)
    v_total = _find_value_inline_or_next(
        all_lines, re.compile(r"^VIVIENDAS\s+CONSTRUIBLES\s+TOTAL\s*:?\s*(.*)$",
                              re.IGNORECASE))
    out["viviendas_construibles_total"] = _to_int(v_total)

    # DENSIDAD BRUTA: 130,83 Viv/Ha
    densidad = _find_value_inline_or_next(
        all_lines, re.compile(r"^DENSIDAD\s+BRUTA\s*:?\s*(.*)$", re.IGNORECASE))
    out["densidad_bruta_viv_ha"] = _to_float(densidad)

    # SUPERFICIE MEDIA: 99 m2/viv (puede haber varias, una por código privado)
    # Sólo capturamos la primera.
    sm = _find_value_inline_or_next(
        all_lines, re.compile(r"^SUPERFICIE\s+MEDIA\s*:?\s*(.*)$", re.IGNORECASE))
    out["superficie_media_viv_m2"] = _to_float(sm)

    return out
