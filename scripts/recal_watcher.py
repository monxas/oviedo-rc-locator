"""Watcher for `data/.recal_pending` sentinel.

When the validator's accept-counter hits RECAL_THRESHOLD, the API writes
`data/.recal_pending` (instead of spawning `systemctl restart` on itself,
which used to RST in-flight requests). This script polls every 30s, and
when the sentinel appears it:

  1. runs `scripts/recalibrate.py` (which rewrites
     `data/calibration_offsets.json`),
  2. removes the sentinel,
  3. touches `data/.cal_reloaded` for diagnostics.

The locator and validator processes pick up the new offsets file on their
next request via `oviedo_rc.calibration._load`'s mtime-based lazy reload,
so no service restart is needed.

Run via the systemd user unit `recal-watcher.service`.
"""
from __future__ import annotations

import logging
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path.home() / "oviedo-rc-locator"
SENTINEL = ROOT / "data" / ".recal_pending"
RELOADED = ROOT / "data" / ".cal_reloaded"
RECAL_PY = ROOT / "scripts" / "recalibrate.py"
LOG_FILE = ROOT / "recal.log"
PYBIN = ROOT / ".venv" / "bin" / "python"
POLL_S = 30

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s recal-watcher %(levelname)s %(message)s",
)
log = logging.getLogger("recal-watcher")


def run_once() -> bool:
    if not SENTINEL.exists():
        return False
    log.info("sentinel present (%s) — running recalibrate.py", SENTINEL)
    try:
        with LOG_FILE.open("ab") as fh:
            fh.write(b"\n=== recal-watcher run @ " + time.strftime("%Y-%m-%dT%H:%M:%S").encode() + b" ===\n")
            rc = subprocess.run(
                [str(PYBIN), str(RECAL_PY)],
                cwd=str(ROOT),
                stdout=fh,
                stderr=subprocess.STDOUT,
                timeout=600,
            ).returncode
    except subprocess.TimeoutExpired:
        log.error("recalibrate.py timed out after 600s")
        return False
    except Exception:
        log.exception("recalibrate.py failed to launch")
        return False
    if rc != 0:
        log.error("recalibrate.py exited with rc=%s — leaving sentinel in place", rc)
        return False
    try:
        SENTINEL.unlink()
    except FileNotFoundError:
        pass
    try:
        RELOADED.write_text(time.strftime("%Y-%m-%dT%H:%M:%S"))
    except OSError:
        pass
    log.info("recalibrate.py OK — sentinel cleared, services pick up new cal via mtime")
    return True


def main():
    log.info("recal-watcher starting (poll=%ss, sentinel=%s)", POLL_S, SENTINEL)
    while True:
        try:
            run_once()
        except KeyboardInterrupt:
            return 0
        except Exception:
            log.exception("watcher loop error")
        time.sleep(POLL_S)


if __name__ == "__main__":
    sys.exit(main() or 0)
