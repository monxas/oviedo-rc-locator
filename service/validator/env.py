"""Validator env loading — reads ~/.validator.env plus VALIDATOR_* env vars."""
import os
from pathlib import Path

ROOT = Path.home() / "oviedo-rc-locator"
ENV_FILE = ROOT / ".validator.env"


def _load_env() -> dict:
    env = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    for k, v in os.environ.items():
        if k.startswith("VALIDATOR_"):
            env[k] = v
    return env


ENV = _load_env()
TOKEN = ENV.get("VALIDATOR_TOKEN", "")
HOST = ENV.get("VALIDATOR_HOST", "127.0.0.1")
PORT = int(ENV.get("VALIDATOR_PORT", "9103"))

if not TOKEN:
    raise RuntimeError("VALIDATOR_TOKEN no definido en .validator.env")
