#!/usr/bin/env python3
"""Web app para validar predicciones del modelo de localización RC→PGOU.
Para cada candidato, el usuario marca:
  - "OK"      : la marca cae sobre el portal correcto
  - "MAL"     : la marca cae fuera del portal (y opcionalmente click en posición correcta)
  - "SKIP"    : no se puede determinar
"""
import json
from pathlib import Path
from flask import Flask, render_template, jsonify, request, send_from_directory

ROOT = Path(__file__).resolve().parent

# Training set (los 100 originales)
CANDIDATES = json.loads((ROOT / "static" / "candidates.json").read_text())
RESULTS_FILE = ROOT / "results" / "validations.json"
RESULTS_FILE.parent.mkdir(exist_ok=True)
results = json.loads(RESULTS_FILE.read_text()) if RESULTS_FILE.exists() else {}

# Test set (los 10 nuevos para evaluar el modelo refinado)
TEST_FILE = ROOT / "static" / "test_set.json"
# Hack: el gen_test_set guarda en validation_app/test_set.json, no en static/
ALT_TEST = ROOT / "test_set.json"
if not TEST_FILE.exists() and ALT_TEST.exists():
    TEST_FILE = ALT_TEST
TEST = json.loads(TEST_FILE.read_text()) if TEST_FILE.exists() else []
# Adaptar test entries con plan_img/cat_img relativos a /static
for t in TEST:
    t["plan_img"] = f"test_set/{t['idx']:02d}_plan.jpg"
    t["cat_img"]  = f"test_set/{t['idx']:02d}_cat.jpg"
    t.setdefault("plan_size", [900, 900])
    t.setdefault("warnings", [])
    t.setdefault("utm", [0, 0])

TEST_RESULTS_FILE = ROOT / "results" / "test_validations.json"
test_results = json.loads(TEST_RESULTS_FILE.read_text()) if TEST_RESULTS_FILE.exists() else {}

# Parcel set (capa de parcelas catastrales)
PARCEL_FILE = ROOT / "parcel_set.json"
PARCEL = json.loads(PARCEL_FILE.read_text()) if PARCEL_FILE.exists() else []
for t in PARCEL:
    t["plan_img"] = f"parcel_set/{t['idx']:02d}_plan.jpg"
    t["cat_img"]  = f"parcel_set/{t['idx']:02d}_cat.jpg"
    t.setdefault("plan_size", [1100, 1100])
    t.setdefault("warnings", [])
    t.setdefault("utm", [0, 0])
PARCEL_RES_FILE = ROOT / "results" / "parcel_validations.json"
parcel_results = json.loads(PARCEL_RES_FILE.read_text()) if PARCEL_RES_FILE.exists() else {}

# Quality set (5 RCs aleatorias offline-cache)
QUALITY_FILE = ROOT / "quality_set.json"
QUALITY = json.loads(QUALITY_FILE.read_text()) if QUALITY_FILE.exists() else []
for t in QUALITY:
    t["plan_img"] = f"quality_set/{t['idx']:02d}_plan.jpg"
    t["cat_img"]  = f"quality_set/{t['idx']:02d}_cat.jpg"
    t.setdefault("plan_size", [1100, 1100])
    t.setdefault("warnings", [])
    t.setdefault("utm", [0, 0])
QUALITY_RES_FILE = ROOT / "results" / "quality_validations.json"
quality_results = json.loads(QUALITY_RES_FILE.read_text()) if QUALITY_RES_FILE.exists() else {}

app = Flask(__name__, static_folder="static", template_folder="templates")


def stats(cands, res):
    total = len(cands); done = len(res)
    ok = sum(1 for r in res.values() if r["verdict"] == "ok")
    bad = sum(1 for r in res.values() if r["verdict"] == "bad")
    bad_with_click = sum(1 for r in res.values() if r["verdict"] == "bad" and r.get("click_xy"))
    skip = sum(1 for r in res.values() if r["verdict"] == "skip")
    return total, done, ok, bad, bad_with_click, skip


@app.route("/")
def index():
    total, done, ok, bad, bad_with_click, skip = stats(CANDIDATES, results)
    return render_template("index.html",
                            candidates=CANDIDATES, total=total,
                            done=done, ok=ok, bad=bad,
                            bad_with_click=bad_with_click, skip=skip,
                            results=results, page_title="Training set",
                            api_save="/api/save", api_reset_prefix="/api/reset",
                            other_url="/test", other_label="→ Test set")


@app.route("/test")
def test_page():
    total, done, ok, bad, bad_with_click, skip = stats(TEST, test_results)
    return render_template("index.html",
                            candidates=TEST, total=total,
                            done=done, ok=ok, bad=bad,
                            bad_with_click=bad_with_click, skip=skip,
                            results=test_results, page_title="TEST SET (10 nuevas)",
                            api_save="/api/test_save", api_reset_prefix="/api/test_reset",
                            other_url="/parcels", other_label="→ Parcel set")


@app.route("/parcels")
def parcels_page():
    total, done, ok, bad, bad_with_click, skip = stats(PARCEL, parcel_results)
    return render_template("index.html",
                            candidates=PARCEL, total=total,
                            done=done, ok=ok, bad=bad,
                            bad_with_click=bad_with_click, skip=skip,
                            results=parcel_results, page_title="PARCEL SET (con polígonos)",
                            api_save="/api/parcel_save", api_reset_prefix="/api/parcel_reset",
                            other_url="/", other_label="← Training set")


@app.route("/api/parcel_save", methods=["POST"])
def parcel_save():
    data = request.get_json()
    idx = str(data["idx"])
    parcel_results[idx] = {"verdict": data["verdict"],
                            "click_xy": data.get("click_xy"), "ts": data.get("ts")}
    PARCEL_RES_FILE.write_text(json.dumps(parcel_results, indent=2, ensure_ascii=False))
    return jsonify({"ok": True, "saved": idx})


@app.route("/api/parcel_reset/<idx>", methods=["POST"])
def parcel_reset(idx):
    if idx in parcel_results:
        del parcel_results[idx]
        PARCEL_RES_FILE.write_text(json.dumps(parcel_results, indent=2, ensure_ascii=False))
    return jsonify({"ok": True})


@app.route("/quality")
def quality_page():
    total, done, ok, bad, bad_with_click, skip = stats(QUALITY, quality_results)
    return render_template("index.html",
                            candidates=QUALITY, total=total,
                            done=done, ok=ok, bad=bad,
                            bad_with_click=bad_with_click, skip=skip,
                            results=quality_results, page_title="QUALITY (offline)",
                            api_save="/api/quality_save", api_reset_prefix="/api/quality_reset",
                            other_url="/", other_label="← Training set")


@app.route("/api/quality_save", methods=["POST"])
def quality_save():
    data = request.get_json()
    idx = str(data["idx"])
    quality_results[idx] = {"verdict": data["verdict"],
                            "click_xy": data.get("click_xy"), "ts": data.get("ts")}
    QUALITY_RES_FILE.write_text(json.dumps(quality_results, indent=2, ensure_ascii=False))
    return jsonify({"ok": True, "saved": idx})


@app.route("/api/quality_reset/<idx>", methods=["POST"])
def quality_reset(idx):
    if idx in quality_results:
        del quality_results[idx]
        QUALITY_RES_FILE.write_text(json.dumps(quality_results, indent=2, ensure_ascii=False))
    return jsonify({"ok": True})


@app.route("/api/save", methods=["POST"])
def save():
    data = request.get_json()
    idx = str(data["idx"])
    results[idx] = {"verdict": data["verdict"],
                    "click_xy": data.get("click_xy"), "ts": data.get("ts")}
    RESULTS_FILE.write_text(json.dumps(results, indent=2, ensure_ascii=False))
    return jsonify({"ok": True, "saved": idx})


@app.route("/api/test_save", methods=["POST"])
def test_save():
    data = request.get_json()
    idx = str(data["idx"])
    test_results[idx] = {"verdict": data["verdict"],
                         "click_xy": data.get("click_xy"), "ts": data.get("ts")}
    TEST_RESULTS_FILE.write_text(json.dumps(test_results, indent=2, ensure_ascii=False))
    return jsonify({"ok": True, "saved": idx})


@app.route("/api/reset/<idx>", methods=["POST"])
def reset(idx):
    if idx in results:
        del results[idx]
        RESULTS_FILE.write_text(json.dumps(results, indent=2, ensure_ascii=False))
    return jsonify({"ok": True})


@app.route("/api/test_reset/<idx>", methods=["POST"])
def test_reset(idx):
    if idx in test_results:
        del test_results[idx]
        TEST_RESULTS_FILE.write_text(json.dumps(test_results, indent=2, ensure_ascii=False))
    return jsonify({"ok": True})


@app.route("/api/export")
def export():
    """Devuelve resultados con la información necesaria para re-calibrar el modelo."""
    out = []
    for idx, res in results.items():
        c = CANDIDATES[int(idx)]
        entry = {
            "rc": c["rc"], "utm": c["utm"], "address": c["address"],
            "sheet_name": c["sheet_name"], "cell": c["cell"],
            "sub_quadrant": c["sub_quadrant"],
            "body_rx_predicted": c["body_rx"], "body_ry_predicted": c["body_ry"],
            "verdict": res["verdict"],
        }
        if res["verdict"] == "bad" and res.get("click_xy"):
            # Click en pixel del crop. Convertir a body relative del plano completo
            # plan_rc_xy es la posición del RC en el crop (donde el modelo lo puso)
            # click_xy es donde el usuario lo movió
            cx, cy = res["click_xy"]
            pred_x, pred_y = c["plan_rc_xy"]
            m_per_px = c["plan_m_per_px"]
            # Diferencia en metros
            dx_m = (cx - pred_x) * m_per_px
            dy_m = (cy - pred_y) * m_per_px
            entry["correction_dx_m"] = dx_m
            entry["correction_dy_m"] = dy_m
        out.append(entry)
    return jsonify(out)


@app.route("/static/imgs/<path:filename>")
def serve_img(filename):
    return send_from_directory(ROOT / "static" / "imgs", filename)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5057, debug=False)
