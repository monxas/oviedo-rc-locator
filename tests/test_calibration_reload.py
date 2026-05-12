"""Verifica que `calibration._load()` releea el JSON cuando cambia su mtime.

PR2 introdujo lazy-reload por `st_mtime` para que `recalibrate.py` pudiera
actualizar `data/calibration_offsets.json` sin restart de los servicios. Si
este test falla, los offsets nuevos NO se aplicarán en producción hasta un
reinicio.
"""
import json
import os
import shutil
import time
from pathlib import Path

import pytest

from oviedo_rc import calibration


def _cal_path() -> Path:
    return Path(__file__).resolve().parents[1] / "data" / "calibration_offsets.json"


def test_offset_reload_on_mtime_change(tmp_path):
    p = _cal_path()
    if not p.exists():
        pytest.skip("calibration_offsets.json no presente")

    original_bytes = p.read_bytes()
    backup = tmp_path / "calibration_offsets.backup.json"
    backup.write_bytes(original_bytes)

    cell, sub = "16-K", "I"
    try:
        # 1) Captura valor v1
        v1 = calibration.offset_for(cell, sub)

        # 2) Modificar el JSON con valor centinela [99, 99] y escribir
        #    atómicamente con mtime > anterior.
        cal = json.loads(original_bytes)
        cal.setdefault("csub_offsets_px", {})[f"{cell}-{sub}"] = [99, 99]
        tmp_write = p.with_suffix(".json.testtmp")
        tmp_write.write_text(json.dumps(cal))
        # Asegurar mtime estrictamente mayor (FS con resolución 1s o coarse mtime).
        old_mtime = p.stat().st_mtime
        new_mtime = old_mtime + 2.0
        os.utime(tmp_write, (new_mtime, new_mtime))
        os.replace(tmp_write, p)  # atomic on POSIX
        # Sanity: la nueva mtime quedó.
        assert p.stat().st_mtime > old_mtime

        # 3) Re-llamada: debe devolver [99, 99]
        v2 = calibration.offset_for(cell, sub)
        assert v2 == (99, 99), (
            f"calibration no recargó tras mtime change: v1={v1}, v2={v2}. "
            "El lazy-reload del PR2 está roto."
        )
    finally:
        # 4) Restaurar fichero original con mtime futura para forzar reload
        #    en el siguiente test que use la cal.
        p.write_bytes(original_bytes)
        future = time.time() + 3.0
        os.utime(p, (future, future))
        # Limpia un posible .testtmp residual
        leftover = p.with_suffix(".json.testtmp")
        if leftover.exists():
            leftover.unlink()
        # Verifica que la restauración cargó (la cal queda como estaba).
        v3 = calibration.offset_for(cell, sub)
        assert v3 != (99, 99), "fallo al restaurar la cal original"
