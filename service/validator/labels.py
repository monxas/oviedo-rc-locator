"""Validator labels storage — JSON-on-disk, lock-serialised writes."""
import json
import threading
from pathlib import Path

ROOT = Path.home() / "oviedo-rc-locator"
LABELS_PATH = ROOT / "data" / "validator_labels.json"
FICHA_LABELS_PATH = ROOT / "data" / "validator_labels_fichas.json"
LABELS_PATH.parent.mkdir(exist_ok=True, parents=True)

_LABELS_LOCK = threading.Lock()


def load_labels() -> list:
    if not LABELS_PATH.exists():
        return []
    return json.loads(LABELS_PATH.read_text(encoding="utf-8"))


def save_labels(labels):
    LABELS_PATH.write_text(json.dumps(labels, indent=2, ensure_ascii=False))


def labeled_rcs() -> set:
    return {l["rc"] for l in load_labels()}


def load_ficha_labels() -> list:
    if not FICHA_LABELS_PATH.exists():
        return []
    try:
        return json.loads(FICHA_LABELS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []


def save_ficha_labels(labels):
    tmp = FICHA_LABELS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(labels, indent=2, ensure_ascii=False))
    tmp.replace(FICHA_LABELS_PATH)
