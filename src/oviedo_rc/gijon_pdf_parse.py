"""Parser para fichas PDF del PGOU de Gijón.

Los PDFs usan fuentes custom sin ToUnicode CMap. Mayúsculas vienen
con Caesar +29 ASCII (ej "5(6,'(1&,$/" -> "RESIDENCIAL"); dígitos
vienen como letras griegas (Ϭ-ϵ -> 0-9). Las minúsculas usan otra
fuente con substitution arbitraria que no decodificamos, pero los
labels de campos son strings ciphered consistentes entre PDFs así
que los usamos como anclas para localizar los valores.

Estrategia:
1. Extraer spans con coords (x, y) via PyMuPDF "dict".
2. Decodificar cada span.
3. Para textos canónicos (RESIDENCIAL ..., COMPENSACIÓN, ...) basta presencia.
4. Para valores numéricos: usar ancla cifrada como ¿label?, buscar número
   en la MISMA fila Y aproximada con x mayor (a la derecha) — si la celda
   está vacía (no hay número en esa fila), el campo queda None.
"""
from __future__ import annotations

import re
from pathlib import Path

import fitz  # PyMuPDF


# --- cipher decoder --------------------------------------------------------

_GREEK = {
    "Ϭ": "0", "ϭ": "1", "Ϯ": "2", "ϯ": "3", "ϰ": "4",
    "ϱ": "5", "ϲ": "6", "ϳ": "7", "ϴ": "8", "ϵ": "9",
    "͕": ",", "͘": ".", "й": "%", "͗": ":", "ͬ": "/",
    "Ͳ": "-", "Ï": "Ó", "Ò": "Ú",
}


def decode_line(s: str) -> str:
    """Decodifica Caesar+29 mayúsculas, dígitos griegos y \\x03 -> espacio.
    Ignora el form-feed (\\x0c) que aparece a veces en SUNC."""
    out: list[str] = []
    for c in s:
        o = ord(c)
        if 32 <= o <= 90:
            out.append(chr(o + 29))
        elif o == 0x03:
            out.append(" ")
        elif o == 0x0c:
            out.append("")  # drop form feed
        else:
            out.append(_GREEK.get(c, c))
    return "".join(out)


# --- value parsing ---------------------------------------------------------

_NUMBER_RE = re.compile(r"^\s*-?\d{1,3}(?:\.\d{3})*(?:,\d+)?\s*$|^\s*-?\d+(?:,\d+)?\s*$")
_SINGLE_DIGIT_RE = re.compile(r"^\s*\d{1,2}\s*$")


def _to_float(s: str) -> float | None:
    if s is None:
        return None
    s = s.strip().rstrip("%").strip()
    if not s:
        return None
    s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def _to_int(s: str) -> int | None:
    f = _to_float(s)
    if f is None:
        return None
    return int(round(f))


# --- known value phrases (decoded, uppercase) -----------------------------

_USOS = [
    "RESIDENCIAL VIVIENDA COLECTIVA",
    "RESIDENCIAL VIVIENDA UNIFAMILIAR",
    "RESIDENCIAL VIVIENDA",
    "PRODUCTIVO INDUSTRIAL/CIENTÍFICO TECNOLÓGICO",
    "PRODUCTIVO INDUSTRIAL / CIENTÍFICO TECNOLÓGICO",
    "PRODUCTIVO INDUSTRIAL",
    "PRODUCTIVO TERCIARIO COMERCIAL",
    "PRODUCTIVO TERCIARIO",
    "DOTACIONAL PRIVADO",
    "DOTACIONAL PÚBLICO",
    "DOTACIONAL SERVICIOS URBANOS",
    "DOTACIONAL",
    "TERCIARIO",
    "INDUSTRIAL",
]

_SISTEMAS = ["COMPENSACIÓN", "COOPERACIÓN", "EXPROPIACIÓN"]
_INICIATIVAS = ["PRIVADA", "PÚBLICA"]
_PLANEAMIENTOS = [
    "PLAN ESPECIAL DE REFORMA INTERIOR",
    "ESTUDIO DE DETALLE",
    "PLAN PARCIAL",
    "PLAN ESPECIAL",
    "NORMAS COMPLEMENTARIAS",
]
_CATEGORIAS_FULL = [
    "SUNC URBANO NO CONSOLIDADO",
    "SUC URBANO CONSOLIDADO",
    "SUR SUELO URBANIZABLE SECTORIZADO",
    "SUR SUELO URBANIZABLE",
    "URBANO NO CONSOLIDADO",
    "URBANO CONSOLIDADO",
    "URBANIZABLE SECTORIZADO",
    "URBANIZABLE",
]
_CLASES = ["SUELO URBANIZABLE", "SUELO URBANO", "SUELO NO URBANIZABLE"]


# Ancla cifrada -> field. Cada lista contiene variantes consideradas equivalentes.
_NUM_ANCHORS = [
    ("superficie_ambito_m2", [
        "\x11Zhd\x04\x03\x18\x1c>\x03\x06D\x11/dK\x03DϮ",
        "\x11whd\x04 \x18\x1c[ \x06a\x11Ldh a2",
        "\x11whd\x04 \x18\x1c[ \x06a\x11Ldh aϮ",
    ], "float"),
    ("superficie_neta_m2", [
        "^hW\x1cZ&/\x12/\x1c\x03E\x1cd\x04",
        "^ht\x1cwCL\x12L\x1c b\x1cd\x04",
    ], "float"),
    ("densidad_viv_ha", [
        "\x18\x1cE^/\x18\x04\x18", "\x18\x1cb^L\x18\x04\x18",
    ], "int"),
    ("viviendas_estimadas", [
        "s/s/\x1cE\x18\x04^\x03\x1c^d/D\x04\x18\x04^",
        "sLsL\x1cb\x18\x04^ \x1c^dLa\x04\x18\x04^",
    ], "int"),
    ("sup_residencial_m2", [
        "Z\x1c^/\x18\x1cE\x12/\x04>", "w\x1c^L\x18\x1cb\x12L\x04[",
    ], "float"),
    ("sup_industrial_m2", [
        "/E\x18h^dZ/\x04>",
        "/E\x18h^dZ/\x04>\x03ͬ\x03\x12/\x1cEd1&/\x12K\x03d\x1c\x12EK>M'/\x12K",
        "Lb\x18h^dwL\x04[",
        "Lb\x18h^dwL\x04[ / \x12L\x1cbdNCL\x12h d\x1c\x12bh[jDL\x12h",
    ], "float"),
    ("sup_terciario_m2", [
        "d\x1cZ\x12/\x04Z/K", "d\x1cw\x12L\x04wLh",
    ], "float"),
    ("sup_dotacional_privado_m2", [
        "\x18Kd\x04\x12/KE\x04>\x03WZ/s\x04\x18K",
        "\x18hd\x04\x12Lhb\x04[ twLs\x04\x18h",
    ], "float"),
    ("sup_viario_m2", [
        "s/\x04Z/K", "sL\x04wLh",
    ], "float"),
    ("sup_espacios_libres_m2", [
        "\x7fKE\x04^\x03s\x1cZ\x18\x1c^ͬ\x1c^W\x04\x12/K^\x03>/\x11Z\x1c^",
        "\x7fhb\x04^ s\x1cw\x18\x1c^/\x1c^t\x04\x12Lh^ [L\x11w\x1c^",
    ], "float"),
    ("sup_dotacional_publico_m2", [
        "\x18Kd\x04\x12/KE\x04>\x03Wj\x11>/\x12K",
        "\x18hd\x04\x12Lhb\x04[ tj\x11[L\x12h",
    ], "float"),
    ("viviendas_protegidas_pct", [
        "йs/s/\x1cE\x18\x04^\x03WZKd\x1c'/\x18\x04^",
        "%sLsL\x1cb\x18\x04^ twhd\x1cDL\x18\x04^",
    ], "float"),
]

_ORDENANZA_ANCHORS = ["KZ\x18\x1cE\x04E\x7f\x04", "hw\x18\x1cb\x04b\x7f\x04"]


def _norm(s: str) -> str:
    return s.replace("\x03", " ").replace("\x0c", "").strip()


def _extract_spans(pdf_path: Path) -> list[dict]:
    """Devuelve lista de spans: {page, x0, y0, x1, y1, raw, decoded}.
    Ordenados por (page, y0, x0)."""
    spans: list[dict] = []
    doc = fitz.open(pdf_path)
    try:
        for pi, page in enumerate(doc):
            d = page.get_text("dict")
            for block in d.get("blocks", []):
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        bb = span.get("bbox") or (0, 0, 0, 0)
                        raw = span.get("text", "")
                        if not raw or not raw.strip():
                            continue
                        spans.append({
                            "page": pi,
                            "x0": bb[0],
                            "y0": bb[1],
                            "x1": bb[2],
                            "y1": bb[3],
                            "raw": raw,
                            "decoded": decode_line(raw),
                        })
    finally:
        doc.close()
    spans.sort(key=lambda s: (s["page"], round(s["y0"], 1), s["x0"]))
    return spans


def _find_label_span(spans: list[dict], anchors: list[str]) -> dict | None:
    anchors_norm = {_norm(a) for a in anchors}
    for s in spans:
        if _norm(s["raw"]) in anchors_norm:
            return s
    return None


def _value_in_same_row(spans: list[dict], label: dict, *, y_tol: float = 4.0,
                       min_dx: float = 5.0) -> str | None:
    """Devuelve el primer span numérico (decoded) en la misma fila Y a la derecha del label."""
    page = label["page"]
    y_mid = (label["y0"] + label["y1"]) / 2
    candidates = []
    for s in spans:
        if s["page"] != page or s is label:
            continue
        s_mid = (s["y0"] + s["y1"]) / 2
        if abs(s_mid - y_mid) > y_tol:
            continue
        if s["x0"] < label["x1"] + min_dx:
            continue
        dec = s["decoded"].strip()
        if _NUMBER_RE.match(dec):
            candidates.append((s["x0"], dec))
    if not candidates:
        return None
    candidates.sort()
    return candidates[0][1]


def _value_below_same_column(spans: list[dict], label: dict, *, max_dy: float = 80.0,
                              x_overlap_extra: float = 30.0) -> str | None:
    """Devuelve el primer span numérico cuya x cae bajo la columna del label."""
    page = label["page"]
    lx0, lx1 = label["x0"], label["x1"]
    cands = []
    for s in spans:
        if s["page"] != page or s is label:
            continue
        if s["y0"] <= label["y1"]:
            continue
        if s["y0"] - label["y1"] > max_dy:
            continue
        # x-overlap test: value's center within label x range (with small extension)
        s_cx = (s["x0"] + s["x1"]) / 2
        if s_cx < lx0 - x_overlap_extra or s_cx > lx1 + x_overlap_extra:
            continue
        dec = s["decoded"].strip()
        if _NUMBER_RE.match(dec):
            cands.append((s["y0"], dec))
    if not cands:
        return None
    cands.sort()
    return cands[0][1]


def _decoded_full_text(spans: list[dict]) -> str:
    return "\n".join(s["decoded"] for s in spans)


def _decoded_by_column(spans: list[dict]) -> str:
    """Agrupa spans por columna X aproximada, leyéndolos por y; produce texto
    donde palabras consecutivas (en la misma columna) están juntas con un espacio."""
    # group by (page, ~x bucket) -> sorted by y -> string
    from collections import defaultdict
    by_col: dict[tuple, list[dict]] = defaultdict(list)
    for s in spans:
        bucket = (s["page"], round(s["x0"] / 20))
        by_col[bucket].append(s)
    parts = []
    for k in sorted(by_col.keys()):
        for s in sorted(by_col[k], key=lambda z: z["y0"]):
            parts.append(s["decoded"])
        parts.append("|||")
    return " ".join(parts)


def _find_phrase(decoded_text: str, phrases: list[str]) -> str | None:
    """Devuelve la primera frase de la lista que aparece, tolerando saltos de línea."""
    for ph in phrases:
        pat = re.compile(r"\b" + re.escape(ph).replace(r"\ ", r"\s+") + r"\b")
        if pat.search(decoded_text):
            return ph
    return None


def _extract_categoria_clase(decoded_text: str) -> tuple[str | None, str | None]:
    cat = _find_phrase(decoded_text, _CATEGORIAS_FULL)
    cla = _find_phrase(decoded_text, _CLASES)
    # Fallback robusto: en el PDF los tokens "SUNC" / "URBANO" / "NO CONSOLIDADO"
    # están en spans distintos (filas distintas), así que la búsqueda de frase
    # exacta falla. Detectamos cada token por separado.
    if not cat:
        has_sunc = bool(re.search(r"\bSUNC\b", decoded_text))
        has_suc = bool(re.search(r"\bSUC\b", decoded_text))
        has_sur = bool(re.search(r"\bSUR\b", decoded_text))
        has_no_consol = bool(re.search(r"NO\s+CONSOLIDADO", decoded_text))
        has_consol = bool(re.search(r"\bCONSOLIDADO\b", decoded_text))
        has_sector = bool(re.search(r"SECTORIZADO", decoded_text))
        if has_sunc or (has_no_consol and not has_consol):
            cat = "SUNC URBANO NO CONSOLIDADO"
        elif has_suc or (has_consol and not has_no_consol):
            cat = "SUC URBANO CONSOLIDADO"
        elif has_sur and has_sector:
            cat = "SUR SUELO URBANIZABLE SECTORIZADO"
        elif has_sur:
            cat = "SUR SUELO URBANIZABLE"
    elif "SUNC" in decoded_text and not cat.startswith("SUNC"):
        cat = "SUNC URBANO NO CONSOLIDADO"
    elif "SUC" in decoded_text and not cat.startswith("SUC") and "URBANO CONSOLIDADO" in cat:
        cat = "SUC URBANO CONSOLIDADO"
    return cat, cla


def _extract_instrumento(decoded_text: str) -> str | None:
    for full in [
        "PROYECTO DE COMPENSACIÓN",
        "PROYECTO DE URBANIZACIÓN",
        "PROYECTO DE ACTUACIÓN",
    ]:
        if re.search(re.escape(full).replace(r"\ ", r"\s+"), decoded_text):
            return full
    # fallback: PROYECTO DE \n X
    m = re.search(r"PROYECTO\s+DE\s+(ACTUACIÓN|COMPENSACIÓN|URBANIZACIÓN)", decoded_text)
    if m:
        return f"PROYECTO DE {m.group(1)}"
    return None


def _extract_ordenanza(spans: list[dict]) -> str | None:
    """La ordenanza aparece bajo el label "ORDENANZA" como pequeño texto/cifra
    (ej "3", "1", "O3"). Buscar span con anchor y devolver el siguiente span
    NO numérico O dígito 1-2 caracteres en la misma columna (x similar)."""
    label = _find_label_span(spans, _ORDENANZA_ANCHORS)
    if label is None:
        return None
    page = label["page"]
    # Buscar spans en la misma columna (x cercana) con y > label.y
    candidates = []
    for s in spans:
        if s["page"] != page or s is label:
            continue
        if s["y0"] <= label["y1"]:
            continue
        if abs(s["x0"] - label["x0"]) > 80:
            continue
        d = s["decoded"].strip()
        if not d:
            continue
        # ignorar texto largo
        if len(d) > 12:
            continue
        # ignorar líneas decorativas tipo "==", "+", etc
        if not re.search(r"[A-Za-z0-9]", d):
            continue
        # ignorar "Página X de Y"
        if "Página" in d or "página" in d.lower() or "ágina" in d:
            continue
        candidates.append((s["y0"], d))
    if not candidates:
        return None
    candidates.sort()
    return candidates[0][1]


def _extract_altura(spans: list[dict]) -> int | None:
    """ALTURA MÁXIMA viene como ancla "\\x04>dhZ\\x04 D\\x06y/D\\x04" en cabecera de columna.
    El valor (entero pequeño 1-12) está en una fila inferior en la misma X."""
    anchors = [
        "\x04>dhZ\x04\x03D\x06y/D\x04\x03",
        "\x04[dhw\x04 a\x06yLa\x04",
    ]
    label = _find_label_span(spans, anchors)
    if label is None:
        return None
    page = label["page"]
    cands = []
    for s in spans:
        if s["page"] != page or s is label:
            continue
        if s["y0"] <= label["y1"]:
            continue
        if abs(s["x0"] - label["x0"]) > 120:
            continue
        d = s["decoded"].strip()
        if _SINGLE_DIGIT_RE.match(d):
            v = _to_int(d)
            if v is not None and 1 <= v <= 20:
                cands.append((s["y0"], v))
    if not cands:
        return None
    cands.sort()
    return cands[0][1]


def _extract_edificabilidad(spans: list[dict]) -> float | None:
    """Edificabilidad m2/m2 está bajo el label "\\x11whd\\x04 ŵ2/ŵ2" o
    "\\x11whd\\x04\\x03ŵϮͬŵϮ". Valor 0.05-5.0 con formato N,NN."""
    anchors = [
        "\x11Zhd\x04\x03ŵϮͬŵϮ",
        "\x11whd\x04 ŵ2/ŵ2",
    ]
    label = _find_label_span(spans, anchors)
    if label is not None:
        page = label["page"]
        cands = []
        for s in spans:
            if s["page"] != page or s is label:
                continue
            if s["y0"] <= label["y1"]:
                continue
            if abs(s["x0"] - label["x0"]) > 80:
                continue
            d = s["decoded"].strip()
            if re.match(r"^\d{1,2},\d{2,3}$", d):
                v = _to_float(d)
                if v is not None and 0.05 <= v <= 5.0:
                    cands.append((s["y0"], v))
        if cands:
            cands.sort()
            return cands[0][1]
    # fallback: primer número en rango (0.1-5) con formato N,NN
    for s in spans:
        d = s["decoded"].strip()
        if re.match(r"^\d{1,2},\d{2,3}$", d):
            v = _to_float(d)
            if v is not None and 0.05 <= v <= 5.0:
                return v
    return None


def _extract_sup_edificable(spans: list[dict]) -> float | None:
    """Sup. edificable máxima: bajo el label "\\x18LCL\\x12\\x04\\x11[\\x1c ŵ2"."""
    anchors = [
        "\x18/&/\x12\x04\x11>\x1c\x03ŵϮ",
        "\x18LCL\x12\x04\x11[\x1c ŵ2",
    ]
    label = _find_label_span(spans, anchors)
    if label is None:
        return None
    page = label["page"]
    cands = []
    for s in spans:
        if s["page"] != page or s is label:
            continue
        if s["y0"] <= label["y1"]:
            continue
        if abs(s["x0"] - label["x0"]) > 80:
            continue
        d = s["decoded"].strip()
        if _NUMBER_RE.match(d):
            v = _to_float(d)
            if v is not None and v > 0:
                cands.append((s["y0"], v))
    if not cands:
        return None
    cands.sort()
    return cands[0][1]


def parse_ficha_pdf(pdf_path: Path) -> dict:
    """Parsea una ficha PDF de Gijón y devuelve un dict con campos extraídos."""
    pdf_path = Path(pdf_path)
    ambito_id = pdf_path.stem

    result: dict = {
        "ambito_id": ambito_id,
        "uso_predominante": None,
        "categoria_suelo": None,
        "clase_suelo": None,
        "planeamiento_desarrollo": None,
        "iniciativa": None,
        "sistema_actuacion": None,
        "instrumento_gestion": None,
        "ordenanza": None,
        "superficie_ambito_m2": None,
        "superficie_neta_m2": None,
        "edificabilidad_m2m2": None,
        "densidad_viv_ha": None,
        "viviendas_estimadas": None,
        "viviendas_protegidas_pct": None,
        "altura_max_plantas": None,
        "sup_residencial_m2": None,
        "sup_industrial_m2": None,
        "sup_terciario_m2": None,
        "sup_viario_m2": None,
        "sup_espacios_libres_m2": None,
        "sup_dotacional_publico_m2": None,
        "sup_dotacional_privado_m2": None,
        "sup_edificable_max_m2": None,
        "raw_text_decoded": "",
    }

    try:
        spans = _extract_spans(pdf_path)
    except Exception as e:
        result["error"] = f"open fail: {type(e).__name__}: {e}"
        return result

    if not spans:
        result["error"] = "no spans"
        return result

    decoded_text = _decoded_full_text(spans)
    result["raw_text_decoded"] = decoded_text

    # texto agrupado por columna: une "ESTUDIO DE" + "DETALLE" cuando están en columna
    decoded_by_col = _decoded_by_column(spans)
    combined = decoded_text + "\n" + decoded_by_col

    # --- Valores textuales por frase canónica ---
    result["uso_predominante"] = _find_phrase(combined, _USOS)
    cat, cla = _extract_categoria_clase(combined)
    result["categoria_suelo"] = cat
    result["clase_suelo"] = cla
    result["planeamiento_desarrollo"] = _find_phrase(combined, _PLANEAMIENTOS)
    result["sistema_actuacion"] = _find_phrase(combined, _SISTEMAS)
    result["iniciativa"] = _find_phrase(combined, _INICIATIVAS)
    result["instrumento_gestion"] = _extract_instrumento(combined)

    # --- Ordenanza, altura, edificabilidad, sup_edificable ---
    result["ordenanza"] = _extract_ordenanza(spans)
    result["altura_max_plantas"] = _extract_altura(spans)
    result["edificabilidad_m2m2"] = _extract_edificabilidad(spans)
    result["sup_edificable_max_m2"] = _extract_sup_edificable(spans)

    # --- Valores numéricos por ancla + alineación de fila ---
    for field, anchors, kind in _NUM_ANCHORS:
        label = _find_label_span(spans, anchors)
        if label is None:
            continue
        val = _value_in_same_row(spans, label)
        if val is None:
            # algunos labels son cabeceras de columna y el valor está debajo
            val = _value_below_same_column(spans, label, max_dy=80.0)
        if val is None:
            continue
        if kind == "int":
            result[field] = _to_int(val)
        else:
            result[field] = _to_float(val)

    # Si sup_edificable_max no se sacó del label específico, derivar de edif*ambito
    if result["sup_edificable_max_m2"] is None and result["edificabilidad_m2m2"] and result["superficie_ambito_m2"]:
        try:
            result["sup_edificable_max_m2"] = round(
                result["edificabilidad_m2m2"] * result["superficie_ambito_m2"], 2
            )
        except Exception:
            pass

    return result


if __name__ == "__main__":
    import json
    import sys

    for arg in sys.argv[1:]:
        out = parse_ficha_pdf(Path(arg))
        out.pop("raw_text_decoded", None)
        print(json.dumps(out, ensure_ascii=False, indent=2))
