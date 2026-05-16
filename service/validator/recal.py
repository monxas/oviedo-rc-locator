"""Auto-recalibration trigger — counts accepts and writes a sentinel
when the threshold is reached. An out-of-band watcher consumes the
sentinel, runs scripts/recalibrate.py, rewrites
data/calibration_offsets.json, and removes the sentinel. The locator
and validator pick up new offsets via mtime-based lazy reload
(oviedo_rc.calibration._load) without restart.
"""
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path.home() / "oviedo-rc-locator"

RECAL_THRESHOLD = 30
_RECAL_COUNTER_FILE = ROOT / "data" / ".recal_counter"
_RECAL_PENDING_FILE = ROOT / "data" / ".recal_pending"


def _accept_counter() -> int:
    if _RECAL_COUNTER_FILE.exists():
        try:
            return int(_RECAL_COUNTER_FILE.read_text())
        except Exception:
            return 0
    return 0


def _bump_accept_counter() -> int:
    n = _accept_counter() + 1
    _RECAL_COUNTER_FILE.write_text(str(n))
    return n


def _reset_accept_counter():
    _RECAL_COUNTER_FILE.write_text("0")


def _trigger_recalibration():
    try:
        _RECAL_PENDING_FILE.write_text(datetime.now(timezone.utc).isoformat())
    except OSError:
        pass
